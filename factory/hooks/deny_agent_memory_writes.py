#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny file tools that write to personal agent memory (CHB-136).

ADR-eedb: learnings live in this repository, never in personal agent memory. On 2026-11-07 a
session saved a work-in-progress snapshot to memory; it went stale and a later session read it.
Work state lives in Jira, learnings in docs/knowledge/, a hook or a check.

Denied: Write, Edit, MultiEdit and NotebookEdit on any path under `.claude/projects/<project>/memory/`.
A leading `~` expands to the home directory; a relative path resolves against the event's `cwd`.
Not covered: writes through Bash (redirects, cp, sed -i). The hook stops accidents, not a deliberate bypass.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys

MEMORY = re.compile(r"/\.claude/projects/[^/]+/memory(?:/|$)")
PATH_KEYS = ("file_path", "notebook_path")

REASON = (
    "Blocked: personal agent memory is off for this repository (ADR-eedb, CHB-136). "
    "Work state goes in Jira with factory/bin/jira; learnings go in docs/knowledge/, a hook or a check."
)


def in_agent_memory(path: str, *, home: str, cwd: str) -> bool:
    """True when the path, made absolute and normalized, lies in a Claude Code project memory folder."""
    if path == "~" or path.startswith("~/"):
        path = home + path[1:]
    if not posixpath.isabs(path):
        path = posixpath.join(cwd, path)
    return bool(MEMORY.search(posixpath.normpath(path)))


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the agent memory hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the agent memory hook could not read its input.", file=sys.stderr)
        return 2
    if not isinstance(event, dict):
        return 0
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    paths = [tool_input[k] for k in PATH_KEYS if isinstance(tool_input.get(k), str)]
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    home = os.path.expanduser("~")
    if any(in_agent_memory(p, home=home, cwd=cwd) for p in paths):
        print(REASON, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
