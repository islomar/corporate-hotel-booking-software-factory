#!/usr/bin/env python3
"""Claude Code SessionStart hook: say when the rules health check is due (CHB-167).

The rules agents follow (CLAUDE.md, docs/process, ADRs, factory/) grow every day and nobody checks
them as a whole. The owner decided on 2026-11-10: no timer and no headless run; the first session
after 7 or more days since the last check runs it. This hook only informs. It adds one line to the
agent's context, and the agent runs the procedure in docs/process/rules-health.md after the owner's
current request.

Last run: the newest Jira ticket whose summary starts with "Rules health check" (any status). The
procedure creates that ticket first, so the date moves as soon as a run starts, even when its PR
waits for review. A repository file would change only when the PR merges, and the agent cannot
commit to main outside a PR.

Reads: one 1Password read and one Jira search as the agent account. It writes nothing.
Matcher in .claude/settings.json: `startup`, so resume, /clear and compaction do not repeat it.

It fails open: when Jira or 1Password is unreachable, or anything unexpected happens, it prints one
line to stderr and exits 0, with no context added. A reason is only the type name of the error, or
the message of a known error (they hold no secret).

Contract (https://code.claude.com/docs/en/hooks, SessionStart): the event arrives as JSON on stdin.
Printing {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}} adds
the text to Claude's context.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

HOOKS = Path(__file__).resolve().parent
FACTORY = HOOKS.parent
sys.path.insert(0, str(FACTORY))  # python3 -I does not add it
sys.path.insert(0, str(HOOKS))

import jira  # noqa: E402
import op_service_account  # noqa: E402
import validating_sweep  # noqa: E402

SUMMARY_PREFIX = "rules health check"
INTERVAL = timedelta(days=7)
PROCEDURE = "docs/process/rules-health.md"
JQL = 'summary ~ "\\"rules health check\\"" ORDER BY created DESC'


def last_run(issues: list[dict[str, Any]]) -> datetime | None:
    """The creation time of the newest ticket whose summary starts with "Rules health check". Raises ValueError on a bad date."""
    times = [
        datetime.strptime(found["fields"]["created"], "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(timezone.utc)
        for found in issues
        if str(found["fields"].get("summary", "")).lower().startswith(SUMMARY_PREFIX)
    ]
    return max(times) if times else None


def due_line(last: datetime | None, now: datetime) -> str | None:
    """The context line when 7 or more days have passed since the last run (or there was none)."""
    if last is None:
        return f"Rules health check is due: it never ran. After the owner's current request, follow {PROCEDURE}."
    days = (now - last).days
    if now - last < INTERVAL:
        return None
    return (
        f"Rules health check is due: last run {last.date().isoformat()}, {days} days ago (every 7 days). "
        f"After the owner's current request, follow {PROCEDURE}."
    )


def read_issues() -> list[dict[str, Any]]:
    """The tickets that may be a past run: one 1Password read, one Jira search."""
    return validating_sweep.jira_client().search(JQL, limit=20, fields=["summary", "created"])


def _reason(error: BaseException) -> str:
    known = (jira.JiraError, jira.CredentialError, op_service_account.ServiceAccountError)
    text = str(error) if isinstance(error, known) else type(error).__name__
    return " ".join(text.split())[:300] or type(error).__name__


def main(
    stdin: TextIO = sys.stdin,
    now: datetime | None = None,
    read: Callable[[], list[dict[str, Any]]] = read_issues,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    try:
        event = json.load(stdin)
    except ValueError:
        event = {}
    if isinstance(event, dict) and event.get("source", "startup") != "startup":
        return 0
    try:
        line = due_line(last_run(read()), now or datetime.now(timezone.utc))
    except BaseException as error:  # noqa: BLE001 - fail open on anything
        print(f"Rules health check skipped: {_reason(error)}", file=err)
        return 0
    if line:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": line}}), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
