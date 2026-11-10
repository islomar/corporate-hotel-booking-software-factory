#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny Bash commands that create or push a branch not named CHB-<n>-<slug> (CHB-54).

CLAUDE.md: one Jira ticket per branch, named `CHB-<n>-<slug>`. A branch named otherwise loses
the link between the PR, the ticket and the board.

Denied, when the new branch name does not match ^CHB-[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$:
- `git switch -c|-C|--create|--force-create|--orphan <name>`,
- `git checkout -b|-B|--orphan <name>` and `git worktree add -b|-B <name>`,
- `git branch <name>`, and the new name of `git branch -m|-M|-c|-C`,
- `git push ... <name>`, `<src>:<name>` or `refs/heads/<name>`.

Allowed names: `main`, `dependabot/...`, and `worktree-agent-...` (Claude Code's own worktrees).
Allowed commands: `git branch` listing and deleting, `git push` with no refspec, of `HEAD`, of a
tag or a deletion, and any name built from a shell variable or `$(...)`, which the hook cannot read.
Launchers are stripped first: `rtk`, `rtk proxy`, `factory/bin/as-agent`, `env`.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_words import name, positionals, simple_commands  # noqa: E402

BRANCH = re.compile(r"^CHB-[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$")
ALLOWED_PREFIXES = ("dependabot/", "worktree-agent-")

GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
SWITCH_CREATE = {"-c", "-C", "--create", "--force-create", "--orphan"}
CHECKOUT_CREATE = {"-b", "-B", "--orphan"}
BRANCH_RENAME = {"-m", "-M", "--move", "-c", "-C", "--copy"}
BRANCH_READ_OR_DELETE = {
    "-d", "-D", "--delete", "-l", "--list", "--show-current", "-a", "--all", "-r", "--remotes",
    "-v", "-vv", "--verbose", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--edit-description", "--unset-upstream", "-u", "--set-upstream-to", "--column", "--sort", "--format",
}
BRANCH_READ_OR_DELETE_SHORT = set("dDlarv")
PUSH_VALUE_OPTIONS = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
PUSH_NO_BRANCH = {"-d", "--delete", "--tags", "--all", "--mirror", "--branches", "--prune"}

FORMAT = "CHB-<n>-<slug> (^CHB-[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$), for example CHB-54-guardrail-hooks"


def allowed(branch: str) -> bool:
    if "$" in branch or "`" in branch:
        return True  # built at run time; the hook cannot read it
    return branch == "main" or branch.startswith(ALLOWED_PREFIXES) or bool(BRANCH.match(branch))


def new_branches(words: list[str]) -> list[str]:
    """Branch names that one simple git command creates or pushes."""
    if not words or name(words[0]) != "git":
        return []
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in GIT_VALUE_OPTIONS else 1
    if i >= len(words):
        return []
    sub, args = words[i], words[i + 1 :]
    if sub == "switch":
        return _option_values(args, SWITCH_CREATE)
    if sub == "checkout":
        return _option_values(args, CHECKOUT_CREATE)
    if sub == "worktree" and args[:1] == ["add"]:
        return _option_values(args[1:], {"-b", "-B"})
    if sub == "branch":
        return _branch(args)
    if sub == "push":
        return _push(args)
    return []


def _option_values(args: list[str], options: set[str]) -> list[str]:
    """Values of the options: `-b x`, `-bx` for a short option, `--create=x` for a long one."""
    found = []
    for k, arg in enumerate(args):
        if arg == "--":
            break
        if arg in options and k + 1 < len(args):
            found.append(args[k + 1])
        for option in options:
            if option.startswith("--") and arg.startswith(option + "="):
                found.append(arg[len(option) + 1 :])
            elif len(option) == 2 and arg.startswith(option) and len(arg) > 2 and not arg.startswith("--"):
                found.append(arg[2:])
    return found


def _branch(args: list[str]) -> list[str]:
    flags = [a for a in args if a.startswith("-")]
    for flag in flags:
        if flag.split("=", 1)[0] in BRANCH_READ_OR_DELETE:
            return []
        if re.fullmatch(r"-[A-Za-z]+", flag) and BRANCH_READ_OR_DELETE_SHORT & set(flag[1:]):
            return []
    names = positionals(args, set())
    if not names:
        return []
    if any(f in BRANCH_RENAME for f in flags):
        return [names[-1]]
    return [names[0]]


def _push(args: list[str]) -> list[str]:
    if any(a in PUSH_NO_BRANCH for a in args):
        return []
    found = []
    for refspec in positionals(args, PUSH_VALUE_OPTIONS)[1:]:
        refspec = refspec.lstrip("+")
        if ":" in refspec:
            src, refspec = refspec.split(":", 1)
            if not src or not refspec:
                continue  # `:name` deletes the branch
        if refspec in {"HEAD", "@"} or refspec.startswith("refs/tags/"):
            continue
        found.append(refspec.removeprefix("refs/heads/"))
    return found


def reason(command: str) -> str | None:
    """Why the command is denied, or None when it may run."""
    for words in simple_commands(command):
        for branch in new_branches(words):
            if not allowed(branch):
                return (
                    f"Blocked: branch `{branch}` does not match {FORMAT}. "
                    "One Jira ticket per branch (CLAUDE.md). main, dependabot/* and worktree-agent-* are allowed."
                )
    return None


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the branch name hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the branch name hook could not read its input.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    why = reason(command)
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
