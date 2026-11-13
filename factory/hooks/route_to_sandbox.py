#!/usr/bin/env python3
"""Claude Code PreToolUse hook: third-party code runs in the sandbox container, not on the host (CHB-31).

Package installs, linters and test runners run code from npm and PyPI. On the host that code
could read the owner's keyring, 1Password and home folder. `.devcontainer/run <command>` runs it
in a container with no secret and an egress allow list (README.md, "Sandbox container").

Denied on the host, in any simple command of the Bash command line:
- `uv`, `uvx`, `npm`, `npx`, `pnpm`, `yarn`, `pip`, `pip3`, `pytest`, `py.test`, `vitest`;
- `python -m pytest` and `python -m pip`;
- `make` with a target in SANDBOX_TARGETS (`make -C <dir> check` too).
Behind a wrapper (`timeout`, `nice`, `env`, `xargs`, `sudo`, `exec` and others) every later word is
checked, so an option the hook does not know cannot hide the program. `env -S` is denied: its
command is one string the hook does not split.

Allowed: a simple command whose program is the launcher of a worktree of this repository
(its arguments are not checked). The word is resolved against the folder the shell is in,
following literal `cd` commands; after a `cd` the hook cannot follow, only an absolute path counts.
The trusted launchers come from `git worktree list` of the hook's own checkout: that list lives
in the git folder, which the container mounts read-only, so the container cannot add one.
Also allowed: `make` without a target or with another target (`dev`, `e2e`, `up` start Docker
themselves), stdlib Python (`python3 -I -m unittest`), and everything else.

It guards against mistakes, not against an agent that hides a call: `bash -c "npm ci"`, `eval`
and scripts that call these tools are not seen. The git pre-commit hook
(scripts/git-hooks/pre-commit) uses the container too.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_words import ASSIGNMENT, PYTHON, is_operator, name, redirection, tokens  # noqa: E402

PROGRAMS = {"uv", "uvx", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "pytest", "py.test", "vitest"}
PYTHON_MODULES = {"pytest", "pip"}
SANDBOX_TARGETS = {"check", "test", "check-backend", "check-frontend", "check-deps", "format", "mutate"}
WRAPPERS = {"rtk", "env", "time", "nice", "nohup", "command", "exec", "setsid", "ionice", "stdbuf", "timeout",
            "xargs", "sudo", "doas", "chrt", "taskset", "flock", "unbuffer", "watch", "strace"}
MAKE_VALUE_OPTIONS = {"-C", "--directory", "-f", "--file", "--makefile", "-j", "--jobs", "-I", "-o", "-W"}
LAUNCHER = ".devcontainer/run"
HOOK_CHECKOUT = Path(__file__).resolve().parents[2]


def segments(command: str) -> list[list[str]]:
    """Simple commands as written: words between operators, a redirection's target left out."""
    found: list[list[str]] = []
    current: list[str] = []
    words = tokens(command)
    i = 0
    while i < len(words):
        word = words[i]
        if is_operator(word):
            if redirection(word) and not any(c in word for c in ";|()\n") and "&&" not in word:
                i += 2
                continue
            if current:
                found.append(current)
            current = []
        else:
            current.append(word)
        i += 1
    if current:
        found.append(current)
    return found


def _strip_assignments(words: list[str]) -> list[str]:
    i = 0
    while i < len(words) and ASSIGNMENT.match(words[i]):
        i += 1
    return words[i:]


def _make_targets(args: list[str]) -> list[str]:
    targets, i = [], 0
    while i < len(args):
        word = args[i]
        if word in MAKE_VALUE_OPTIONS:
            i += 2
            continue
        if not word.startswith("-") and "=" not in word:
            targets.append(word)
        i += 1
    return targets


def _runs_third_party_code(words: list[str]) -> bool:
    prog, args = name(words[0]), words[1:]
    if prog in PROGRAMS:
        return True
    if PYTHON.match(prog):
        for i, word in enumerate(args):
            if word == "-m":
                return i + 1 < len(args) and args[i + 1] in PYTHON_MODULES
            if word.startswith("-m"):
                return word[2:] in PYTHON_MODULES
            if not word.startswith("-"):
                return False  # a script runs, not a module
        return False
    if prog == "make":
        return any(target in SANDBOX_TARGETS for target in _make_targets(args))
    return False


def _resolve(word: str, cwd: str | None) -> str | None:
    if os.path.isabs(word):
        return os.path.realpath(word)
    return None if cwd is None else os.path.realpath(os.path.join(cwd, word))


def _after_cd(words: list[str], cwd: str | None) -> str | None:
    """The folder after `cd <literal dir>`; None when the hook cannot tell."""
    args = [w for w in words[1:] if w != "--"]
    if len(args) != 1 or args[0].startswith("-") or any(c in args[0] for c in "$`~*?"):
        return None
    return _resolve(args[0], cwd)


def _verdict(words: list[str], cwd: str | None, launchers: set[str]) -> str | None:
    """The program words that need the sandbox, or None."""
    words = _strip_assignments(words)
    if not words:
        return None
    first = words[0]
    if first.endswith(LAUNCHER):
        if _resolve(first, cwd) in launchers:
            return None
        return " ".join(words[1:]) or first
    if name(first) in WRAPPERS:
        if name(first) == "env" and any(w in ("-S", "--split-string") or w.startswith("--split-string=") for w in words):
            return " ".join(words)
        for i in range(1, len(words)):
            if words[i].endswith(LAUNCHER) or name(words[i]) in PROGRAMS | {"make"} or PYTHON.match(name(words[i])):
                return _verdict(words[i:], cwd, launchers)
        return None
    return " ".join(words) if _runs_third_party_code(words) else None


def reason(command: str, cwd: str | None, launchers: set[str]) -> str | None:
    """Why the command must run in the sandbox, or None.

    `cwd` is the folder the shell starts in; `launchers` are the real paths of trusted launchers.
    """
    for words in segments(command):
        if name(words[0]) == "cd":
            cwd = _after_cd(words, cwd)
            continue
        shown = _verdict(words, cwd, launchers)
        if shown is not None:
            return (
                f"Blocked: `{shown}` runs third-party code on the host. Run it in the sandbox container "
                f"of this checkout: `{LAUNCHER} {shown}` (no secrets, egress allow list; "
                f"README.md, \"Sandbox container\"; CHB-31)."
            )
    return None


def launchers_from_worktree_list(porcelain: str) -> set[str]:
    """Launcher paths of the worktrees in `git worktree list --porcelain` output."""
    return {
        os.path.join(line[len("worktree "):], LAUNCHER)
        for line in porcelain.splitlines()
        if line.startswith("worktree ")
    }


def _trusted_launchers() -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(HOOK_CHECKOUT), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {os.path.realpath(p) for p in launchers_from_worktree_list(result.stdout)}


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the sandbox routing hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the sandbox routing hook could not read its input.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    why = reason(command, cwd, _trusted_launchers())
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
