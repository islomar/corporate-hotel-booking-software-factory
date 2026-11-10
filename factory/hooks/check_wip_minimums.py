#!/usr/bin/env python3
"""Claude Code Stop hook: act on the WIP minimums before the turn ends (CHB-162).

On 2026-11-10 the wrapper printed "Ready is below its minimum (0/3)" and "In Progress is below its
minimum (0/1)" four times. The agent ended its turn each time, and the owner had to push it.

The hook reads the board through factory/jira.py (the column minimums from the board configuration,
the open tickets from one search) and refuses to end the turn, exit 2 with the reason on stderr, when:
- In Progress is below its minimum and Ready has a ticket that can start. The reason names the top one
  (todo_order, the same order a start checks) and the command. Nothing is demanded while In Progress,
  In Review or Validating is at its max: a start would exit 5.
- Ready is below its minimum and the reply does not report the Backlog trigger scan: the words
  "Ready is low" with the result (docs/process/kanban.md, Ready).
Epics count toward no column (keys of the board, CHB-157). A column without a minimum is not checked.

It fails open: when Jira or 1Password is unreachable, or anything unexpected happens, it prints one line
to stderr and exits 0. A reason is the message of a known error (they hold no secret) or only the type
name of an unexpected one.

It never loops. It remembers the findings it refused for, per session, in
`<temp dir>/chb-wip-minimums-<uid>/<session id>.json` (CHB_WIP_MINIMUMS_STATE_DIR overrides the folder).
A second refusal for the same findings is allowed with one line. A board with no findings forgets the
refusal. A state folder it cannot use means it allows the stop.

Contract (https://code.claude.com/docs/en/hooks, Stop): the event arrives as JSON on stdin with
`last_assistant_message` and `session_id`. Exit code 2 blocks the stop and feeds stderr to the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

HOOKS = Path(__file__).resolve().parent
FACTORY = HOOKS.parent
sys.path.insert(0, str(FACTORY))  # python3 -I does not add it
sys.path.insert(0, str(HOOKS))

import jira  # noqa: E402
import op_service_account  # noqa: E402
import validating_sweep  # noqa: E402
from private_folder import private_folder  # noqa: E402

STATE_DIR_ENV = "CHB_WIP_MINIMUMS_STATE_DIR"
SCAN_REPORT = re.compile(r"\bready is low\b", re.IGNORECASE)

Read = Callable[[], "tuple[Sequence[jira.Column], list[dict[str, Any]]]"]


def read_board() -> tuple[Sequence[jira.Column], list[dict[str, Any]]]:
    """The board columns and the open tickets: one 1Password read, two Jira reads."""
    client = validating_sweep.jira_client()
    return client.board_columns(), client.open_issues()


def findings(columns: Sequence[jira.Column], issues: list[dict[str, Any]], message: str) -> list[str]:
    """What the agent must still do, one line each. Empty when the board needs nothing."""
    by_name = {column.name: column for column in columns}
    counts = {
        column.name: sum(
            1
            for issue in issues
            if issue["fields"]["issuetype"]["name"] != "Epic" and str(issue["fields"]["status"]["id"]) in column.status_ids
        )
        for column in columns
    }
    lines: list[str] = []
    in_progress = by_name.get(jira.IN_PROGRESS)
    pull = jira.pull_list(columns)
    if in_progress is not None and in_progress.min is not None and counts[in_progress.name] < in_progress.min:
        full = any(
            (column := by_name.get(name)) is not None and column.max is not None and counts[name] >= column.max
            for name in jira.STARTED_WORK
        )
        startable = jira.todo_order(issues, columns).ready
        if startable and not full:
            top = startable[0]
            lines.append(
                f"{in_progress.name} is below its minimum ({counts[in_progress.name]}/{in_progress.min}) and "
                f"{pull.name if pull else jira.TO_DO} has a ticket that can start: {top}. "
                f'Start it now: factory/bin/jira transition {top} "{jira.IN_PROGRESS}".'
            )
    if pull is not None and pull.min is not None and counts[pull.name] < pull.min and not SCAN_REPORT.search(message):
        lines.append(
            f"{pull.name} is below its minimum ({counts[pull.name]}/{pull.min}) and your reply does not report the "
            "Backlog scan. Look for Backlog tickets with a trigger (docs/process/kanban.md, Backlog) and say "
            '"Ready is low" with the result: which ticket you moved with `ready`, or that nothing in Backlog has a trigger.'
        )
    return lines


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV) or posixpath.join(tempfile.gettempdir(), f"chb-wip-minimums-{os.getuid()}")


def _state_path(folder: str, session_id: str) -> str:
    return posixpath.join(folder, re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "none") + ".json")


def already_refused(folder: str, session_id: str, fingerprint: str | None) -> bool:
    """Record the findings of this stop. True when this session was already refused for the same findings.

    None (no findings) forgets the refusal. Raises OSError when the folder cannot be used.
    """
    path = _state_path(folder, session_id)
    if fingerprint is None:
        if os.path.lexists(folder):
            private_folder(folder)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return False
    private_folder(folder)
    try:
        with open(path, encoding="utf-8") as state:
            if json.load(state).get("refused") == fingerprint:
                return True
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as state:
        json.dump({"refused": fingerprint}, state)
    return False


def _reason(error: BaseException) -> str:
    known = (jira.JiraError, jira.CredentialError, op_service_account.ServiceAccountError)
    text = str(error) if isinstance(error, known) else type(error).__name__
    return " ".join(text.split())[:300] or type(error).__name__


def main(stdin: TextIO = sys.stdin, read: Read | None = None, state_dir: str | None = None) -> int:
    try:
        event = json.load(stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    message = event.get("last_assistant_message")
    message = message if isinstance(message, str) else ""
    session_id = event.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    try:
        columns, issues = (read or read_board)()
        lines = findings(columns, issues, message)
    except BaseException as error:  # noqa: BLE001 - fail open on anything
        print(f"WIP minimums check skipped: {_reason(error)}", file=sys.stderr)
        return 0
    fingerprint = hashlib.sha256("\n".join(lines).encode()).hexdigest() if lines else None
    try:
        repeated = already_refused(state_dir if state_dir is not None else globals()["state_dir"](), session_id, fingerprint)
    except OSError as error:
        print(f"WIP minimums check skipped: state folder unusable ({type(error).__name__})", file=sys.stderr)
        return 0
    if not lines:
        return 0
    if repeated:
        print("WIP minimums: same board as the last refusal, allowing the stop.", file=sys.stderr)
        return 0
    print(
        "The board needs you before this turn ends (CHB-162, docs/process/kanban.md):\n"
        + "\n".join(f"- {line}" for line in lines),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
