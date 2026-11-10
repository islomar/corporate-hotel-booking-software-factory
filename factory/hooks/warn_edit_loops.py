#!/usr/bin/env python3
"""Claude Code PostToolUse hook: warn on the 5th and 8th edit of the same file in a session (CHB-54).

Editing one file again and again is a sign of a loop: guessing instead of reading the failing
test and the plan. After every Write, Edit and MultiEdit the hook counts the edits per file for
the session. On the 5th and 8th edit it adds context for the agent:
"5th edit of <file> this session: step back, re-read the failing test and the plan before editing again."

The file is named relative to the session's `cwd` when it lies under it. The counts live in
`<temp dir>/chb-edit-loops-<uid>/<session id>.json` (the env var CHB_EDIT_LOOP_STATE_DIR overrides the
folder). An unreadable state file starts the count again. A folder that is a symlink, belongs to
another user or is open to others is refused; the state file opens with O_NOFOLLOW.

Contract (https://code.claude.com/docs/en/hooks): the event arrives as JSON on stdin. The hook
prints `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ...}}` and
exits 0. It never blocks: the edit has already happened. Bad input or an error inside the hook
exits 0 with no output, unlike the PreToolUse hooks, which deny.
"""

from __future__ import annotations

import fcntl
import json
import os
import posixpath
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from private_folder import private_folder  # noqa: E402

STATE_DIR_ENV = "CHB_EDIT_LOOP_STATE_DIR"
WARN_AT = {5: "5th", 8: "8th"}
MESSAGE = "{nth} edit of {file} this session: step back, re-read the failing test and the plan before editing again."


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV) or posixpath.join(tempfile.gettempdir(), f"chb-edit-loops-{os.getuid()}")


def record_edit(folder: str, session_id: str, path: str) -> int:
    """Add one edit of the path to the session's counts and return the new count."""
    private_folder(folder)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    fd = os.open(posixpath.join(folder, safe + ".json"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as state:
        fcntl.flock(state, fcntl.LOCK_EX)
        state.seek(0)
        try:
            counts = json.loads(state.read() or "{}")
        except ValueError:
            counts = {}
        if not isinstance(counts, dict):
            counts = {}
        count = counts.get(path, 0) + 1 if isinstance(counts.get(path, 0), int) else 1
        counts[path] = count
        state.seek(0)
        state.truncate()
        state.write(json.dumps(counts))
    return count


def shown(path: str, cwd: str) -> str:
    """The path relative to cwd when it lies under it, else the absolute path."""
    if cwd and (path == cwd or path.startswith(cwd.rstrip("/") + "/")):
        return posixpath.relpath(path, cwd)
    return path


def main() -> int:
    try:
        context = _context(sys.stdin.read())
    except Exception:  # never block or disturb the session: the edit already happened
        return 0
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}))
    return 0


def _context(raw: str) -> str | None:
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    session_id, tool_input = event.get("session_id"), event.get("tool_input")
    path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(session_id, str) or not session_id or not isinstance(path, str) or not path:
        return None
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
    cwd = posixpath.normpath(cwd) if cwd else ""
    path = posixpath.normpath(posixpath.join(cwd, path)) if cwd else posixpath.normpath(path)
    nth = WARN_AT.get(record_edit(state_dir(), session_id, path))
    return MESSAGE.format(nth=nth, file=shown(path, cwd)) if nth else None


if __name__ == "__main__":
    sys.exit(main())
