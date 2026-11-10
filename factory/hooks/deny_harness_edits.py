#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny edits of harness paths on a product ticket's branch (CHB-30).

CLAUDE.md: do not edit `.claude/`, `factory/` or `.github/` as part of a feature ticket. A harness
change made inside a feature PR changes the rules the agent works under without a ticket of its own.

Denied: Write, Edit and MultiEdit of a path whose first folder, relative to the top of its own git
worktree, is `.claude`, `factory` or `.github`, when that worktree's branch is `CHB-<n>-...` and
CHB-<n> is a product epic or its Jira parent is one (PRODUCT_EPICS).
Allowed: other paths, paths outside a git repository, other branches (`main`, `worktree-agent-...`:
the branch name hook guards names), tickets of other epics and tickets without a parent.
A file under `<repo>/.claude/worktrees/agent-x/` belongs to that worktree, not to `.claude/`.

The parent comes from `factory/bin/jira view CHB-<n>`, called only for a harness path, and is cached
per branch in `<temp dir>/chb-harness-epics-<uid>/<branch>.json` (the env var CHB_HARNESS_EPIC_CACHE_DIR
overrides the folder). A folder that is a symlink, belongs to another user or is open to others is
not used; cache files open with O_NOFOLLOW. A failed Jira read is not cached and denies the edit.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from warn_edit_loops import _private_folder  # noqa: E402

# The product epics (owner, CHB-30): Walking skeleton, Observability with OpenTelemetry,
# Security and authentication, Kata specification. Every other epic is factory work.
PRODUCT_EPICS = frozenset({"CHB-4", "CHB-14", "CHB-19", "CHB-50"})
HARNESS = {".claude", "factory", ".github"}
TICKET_BRANCH = re.compile(r"^(CHB-[0-9]+)-")
PARENT_LINE = re.compile(r"^Parent: (CHB-[0-9]+)\s*$", re.MULTILINE)

CACHE_DIR_ENV = "CHB_HARNESS_EPIC_CACHE_DIR"
JIRA = posixpath.join(posixpath.dirname(posixpath.dirname(posixpath.abspath(__file__))), "bin", "jira")
JIRA_TIMEOUT = 20
GIT_TIMEOUT = 10


class JiraUnreadable(Exception):
    """factory/bin/jira could not give the ticket's parent."""


def may_be_harness(path: str) -> bool:
    """Cheap filter before any git call: some folder of the path is named like a harness folder."""
    return bool(HARNESS & set(path.split("/")))


def harness_path(path: str, toplevel: str) -> bool:
    """True when the path's first folder below the worktree top is a harness folder."""
    rel = posixpath.relpath(path, toplevel)
    return not rel.startswith("..") and rel.split("/")[0] in HARNESS


def ticket_of(branch: str) -> str | None:
    match = TICKET_BRANCH.match(branch)
    return match.group(1) if match else None


def cache_dir() -> str:
    return os.environ.get(CACHE_DIR_ENV) or posixpath.join(tempfile.gettempdir(), f"chb-harness-epics-{os.getuid()}")


def jira_parent(ticket: str) -> str | None:
    """The ticket's parent key from `factory/bin/jira view`, or None when it has none."""
    try:
        run = subprocess.run([JIRA, "view", ticket], capture_output=True, text=True, timeout=JIRA_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise JiraUnreadable(f"`factory/bin/jira view {ticket}` timed out after {JIRA_TIMEOUT}s") from None
    except OSError as error:
        raise JiraUnreadable(f"`factory/bin/jira` did not start ({type(error).__name__})") from None
    if run.returncode != 0:
        detail = (run.stderr.strip().splitlines() or [f"exit {run.returncode}"])[-1]
        raise JiraUnreadable(detail)
    if not run.stdout.startswith(ticket + " "):
        raise JiraUnreadable(f"unexpected output from `factory/bin/jira view {ticket}`")
    match = PARENT_LINE.search(run.stdout)
    return match.group(1) if match else None


def _cache_file(branch: str) -> str | None:
    try:
        folder = cache_dir()
        _private_folder(folder)
    except OSError:
        return None
    return posixpath.join(folder, re.sub(r"[^A-Za-z0-9_-]", "_", branch) + ".json")


def _cached(path: str, ticket: str) -> tuple[bool, str | None]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as cached:
            entry = json.loads(cached.read())
    except (OSError, ValueError):
        return False, None
    if isinstance(entry, dict) and entry.get("ticket") == ticket and isinstance(entry.get("parent"), (str, type(None))):
        return True, entry["parent"]
    return False, None


def _store(path: str, ticket: str, parent: str | None) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as cached:
            cached.write(json.dumps({"ticket": ticket, "parent": parent}))
    except OSError:
        pass  # a cache that cannot be written only costs another Jira read


def parent_epic(branch: str, ticket: str) -> str | None:
    """The ticket's parent, from the per-branch cache or from Jira. Raises JiraUnreadable."""
    path = _cache_file(branch)
    if path:
        hit, parent = _cached(path, ticket)
        if hit:
            return parent
    parent = jira_parent(ticket)
    if path:
        _store(path, ticket, parent)
    return parent


def _git(folder: str, *args: str) -> str | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # a git hook's GIT_DIR must not win
    run = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env)
    return run.stdout.strip() if run.returncode == 0 else None


def worktree_of(path: str) -> tuple[str, str, str] | None:
    """(real path, worktree top, branch) for the path's own git worktree, or None outside git."""
    existing, rest = path, ""
    while not posixpath.isdir(existing):
        existing, tail = posixpath.split(existing)
        rest = posixpath.join(tail, rest) if rest else tail
    real = posixpath.realpath(existing)
    toplevel = _git(real, "rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    branch = _git(real, "branch", "--show-current") or ""
    return posixpath.join(real, rest) if rest else real, posixpath.realpath(toplevel), branch


def reason(path: str, *, cwd: str) -> str | None:
    """Why the edit is denied, or None when it may go ahead."""
    path = posixpath.normpath(posixpath.join(cwd, path))
    if not may_be_harness(path):
        return None
    found = worktree_of(path)
    if not found:
        return None
    real, toplevel, branch = found
    ticket = ticket_of(branch)
    if not ticket or not harness_path(real, toplevel):
        return None
    folder = posixpath.relpath(real, toplevel).split("/")[0] + "/"
    if ticket in PRODUCT_EPICS:
        epic = ticket
    else:
        try:
            epic = parent_epic(branch, ticket)
        except JiraUnreadable as error:
            return (
                f"Blocked: could not read the parent epic of {ticket} from Jira ({error}). "
                f"Edits of `{folder}` on a ticket branch need it (CHB-30). "
                f"Check `factory/bin/jira view {ticket}` and retry."
            )
    if epic in PRODUCT_EPICS:
        return (
            f"Blocked: {ticket} belongs to product epic {epic}. A feature ticket does not edit "
            f"`.claude/`, `factory/` or `.github/` (CLAUDE.md); this edit is in `{folder}`. "
            "Make the harness change in a factory ticket of its own."
        )
    return None


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the harness edit hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the harness edit hook could not read its input.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(path, str) or not path:
        return 0
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else os.getcwd()
    why = reason(path, cwd=cwd)
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
