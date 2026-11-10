#!/usr/bin/env python3
"""Claude Code SessionStart hook: what to finish first, read-only (CHB-140).

On 2026-11-10 the 05:15 UTC Dependabot run unblocked two Validating tickets. No session ran then,
and the next session did not sweep Validating until the owner asked.

The hook adds to the agent's context, before the first prompt:
1. Each Validating ticket with the "When:" lines of its "Acceptance criteria (Validating)" section.
   A line is "due" when its earliest absolute date (YYYY-MM-DD, optionally HH:MM and a zone: UTC, Z,
   +01:00 or an IANA name; Europe/Madrid when none) has passed. A date only is due from the start of
   that day. A line with a future date has no mark: the date wins over an event on the same line.
   A line with no absolute date names an event: "check". A ticket without the section: "no section".
2. The `make review-agreement` line (factory/review/agreement.py, same tally), and up to 5 merged
   PRs whose last review comment has no owner reaction, newest first, with links.

Reads: one Jira search as the agent account (factory/jira.py; credentials from 1Password through
the service account) and one GitHub GraphQL query with the owner's `gh` login. It writes nothing.

It fails open: each part that fails prints one line ("Validating sweep failed: <reason>",
"Review agreement failed: <reason>") and the hook exits 0. Every call has a timeout; both parts run
in parallel and stop waiting after DEADLINE_SECONDS. A reason is the message of a known error (they
hold no secret) or only the type name of an unexpected one.

Contract (https://code.claude.com/docs/en/hooks, SessionStart): the event arrives as JSON on stdin.
Printing {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}} adds
the text to Claude's context. The hook ignores the input.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FACTORY = Path(__file__).resolve().parents[1]
ROOT = FACTORY.parent
sys.path.insert(0, str(FACTORY))  # python3 -I does not add it
sys.path.insert(0, str(FACTORY / "review"))

import agreement  # noqa: E402
import jira  # noqa: E402
import op_service_account  # noqa: E402

SECTION = "Acceptance criteria (Validating)"
DEFAULT_ZONE = "Europe/Madrid"  # the owner's time zone; kanban.md examples use it
DEADLINE_SECONDS = 9.0
CALL_TIMEOUT_SECONDS = 6  # each subprocess and HTTP call
KEYRING_TIMEOUT_SECONDS = 3
MAX_UNANSWERED = 5
PR_PAGES = 3  # 100 PRs a page, newest updated first

WHEN = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)?(?:\*\*)?When:(?:\*\*)?\s*(?P<text>.*?)\s*$")
DATE = re.compile(
    r"(?<!\d)(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[T ](?P<hour>\d{1,2}):(?P<minute>\d{2})(?::\d{2})?"
    r"(?:\s*(?P<zone>Z\b|UTC\b|GMT\b|[+-]\d{2}:?\d{2}\b|[A-Z][A-Za-z_]+/[A-Za-z_]+))?)?"
)

QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    owner { login }
    pullRequests(states: MERGED, baseRefName: "main", first: 100, after: $after,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number url title mergedAt updatedAt
        comments(last: 30) {
          nodes {
            author { login __typename }
            databaseId body
            reactions(first: 20) { nodes { content user { login } } }
          }
        }
      }
    }
  }
}
"""
REACTIONS = {"THUMBS_UP": "+1", "THUMBS_DOWN": "-1"}


class SweepError(Exception):
    """A read failed. The message holds no secret."""


# Validating


def classify(line: str, now: datetime) -> str | None:
    """'due' when the earliest absolute date has passed, None when it is ahead, 'check' without one."""
    moments = [m for m in (_moment(match) for match in DATE.finditer(line)) if m is not None]
    if not moments:
        return "check"
    return "due" if min(moments) <= now else None


def _moment(match: re.Match[str]) -> datetime | None:
    try:
        day = date.fromisoformat(match["date"])
        clock = time(int(match["hour"]), int(match["minute"])) if match["hour"] else time(0, 0)
        return datetime.combine(day, clock, tzinfo=_zone(match["zone"]))
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _zone(name: str | None) -> Any:
    if name in {"Z", "UTC", "GMT"}:
        return timezone.utc
    if name and name[0] in "+-":
        digits = name[1:].replace(":", "")
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return timezone(offset if name[0] == "+" else -offset)
    return ZoneInfo(name or DEFAULT_ZONE)


def when_lines(description: Any) -> list[str] | None:
    """The text after "When:" in each line of the Validating section; None when there is no section."""
    content = description.get("content", []) if isinstance(description, dict) else []
    for start, node in enumerate(content):
        if not (_is_heading(node) and _heading_text(node) == SECTION):
            continue
        level = _level(node)
        body = []
        for following in content[start + 1 :]:
            if _is_heading(following) and _level(following) <= level:
                break
            body.append(following)
        text = jira.adf_to_text({"type": "doc", "content": body})
        return [m["text"] for m in map(WHEN.match, text.splitlines()) if m]
    return None


def _is_heading(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "heading"


def _heading_text(node: dict[str, Any]) -> str:
    return "".join(jira.adf_to_text(child) for child in node.get("content", [])).strip()


def _level(node: dict[str, Any]) -> int:
    level = (node.get("attrs") or {}).get("level", 1)
    return level if isinstance(level, int) else 1


def validating_lines(issues: Sequence[dict[str, Any]], now: datetime) -> list[str]:
    lines = [f"Validating: {len(issues)} tickets."]
    for issue in sorted(issues, key=_key_number):
        fields = issue.get("fields") or {}
        title = f"- {issue.get('key')} {fields.get('summary', '')}".rstrip()
        whens = when_lines(fields.get("description"))
        if whens is None:
            lines.append(f"{title}: no section")
            continue
        if not whens:
            lines.append(f"{title}: no When: line")
            continue
        lines.append(title)
        for text in whens:
            mark = classify(text, now)
            lines.append(f"  - {mark + ': ' if mark else ''}When: {text}")
    return lines


def _key_number(issue: dict[str, Any]) -> int:
    match = re.search(r"\d+$", str(issue.get("key", "")))
    return int(match.group()) if match else 0


def _capped_run(limit: int) -> Callable[..., Any]:
    def run(*args: Any, **kwargs: Any) -> Any:
        kwargs["timeout"] = min(kwargs.get("timeout", limit), limit)
        return subprocess.run(*args, **kwargs)  # noqa: S603

    return run


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header never follows one to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def read_issues() -> list[dict[str, Any]]:
    """The Validating tickets with summary and description: one 1Password read, one Jira search."""
    token_reader = lambda: op_service_account.read_token(_capped_run(KEYRING_TIMEOUT_SECONDS))  # noqa: E731
    try:
        output = op_service_account.run_op(
            jira.CREDENTIALS_COMMAND, os.environ, token_reader, subprocess.run, timeout=CALL_TIMEOUT_SECONDS
        )
    except op_service_account.ServiceAccountError as error:
        raise jira.CredentialError(str(error)) from None
    email, token = jira.parse_credentials(output.decode())
    opener = urllib.request.build_opener(_NoRedirect)
    client = jira.Jira(email, token, lambda request: opener.open(request, timeout=CALL_TIMEOUT_SECONDS))
    return client.search('status = "Validating"', fields=["summary", "description"])


# Review agreement


def read_reviews() -> dict[str, Any]:
    """Merged PRs updated since agreement.START, as one GraphQL answer (pages merged)."""
    nodes: list[Any] = []
    after: str | None = None
    answer: dict[str, Any] = {}
    for _ in range(PR_PAGES):
        command = ["gh", "api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-f", f"query={QUERY}"]
        if after:
            command += ["-f", f"after={after}"]
        answer = _gh_json(command)
        connection = _pull_requests(answer)
        page = connection.get("nodes") or []
        nodes.extend(page)
        info = connection.get("pageInfo") or {}
        if not info.get("hasNextPage") or not page or str(page[-1].get("updatedAt", "")) < agreement.START:
            break
        after = info.get("endCursor")
    _pull_requests(answer)["nodes"] = nodes
    return answer


def _gh_json(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(  # noqa: S603
            command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CALL_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        raise SweepError("gh is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise SweepError(f"gh did not answer in {CALL_TIMEOUT_SECONDS} s") from None
    if result.returncode != 0:
        raise SweepError(f"gh api graphql exited {result.returncode} (run `gh auth status`)")
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        raise SweepError("gh returned output that is not JSON") from None
    if not isinstance(answer, dict):
        raise SweepError("gh returned JSON that is not an object")
    return answer


def _pull_requests(answer: dict[str, Any]) -> dict[str, Any]:
    if answer.get("errors"):
        raise SweepError("GitHub GraphQL answered with errors")
    repository = (answer.get("data") or {}).get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("pullRequests"), dict):
        raise SweepError("GitHub GraphQL answer has no repository")
    return repository["pullRequests"]


def _rest_comment(node: dict[str, Any]) -> dict[str, Any]:
    """A GraphQL comment in the REST shape agreement.py reads. A GitHub App's login gets "[bot]"."""
    author = node.get("author") or {}
    login = author.get("login")
    if author.get("__typename") == "Bot":
        login = f"{login}[bot]"
    reactions = [
        {"content": REACTIONS.get(r.get("content"), r.get("content")), "user": {"login": (r.get("user") or {}).get("login")}}
        for r in (node.get("reactions") or {}).get("nodes") or []
    ]
    return {"id": node.get("databaseId"), "user": {"login": login}, "body": node.get("body", ""), "reactions": reactions}


def review_lines(answer: dict[str, Any]) -> list[str]:
    connection = _pull_requests(answer)
    owner = str(answer["data"]["repository"].get("owner", {}).get("login", ""))
    merged = [
        pr for pr in connection.get("nodes") or [] if isinstance(pr.get("mergedAt"), str) and pr["mergedAt"] >= agreement.START
    ]
    merged.sort(key=lambda pr: pr["mergedAt"], reverse=True)
    prs = [
        {**pr, "number": int(pr["number"]), "comments": [_rest_comment(c) for c in (pr.get("comments") or {}).get("nodes") or []]}
        for pr in merged
    ]
    lines = [f"Review agreement: {agreement.report(agreement.tally(prs, owner))}"]
    unanswered = [pr for pr in prs if agreement.answer(pr, owner) == "no answer"][:MAX_UNANSWERED]
    if not unanswered:
        lines.append("Every reviewed PR has an owner reaction.")
        return lines
    lines.append(f"Merged PRs whose last review comment has no owner reaction (newest first, at most {MAX_UNANSWERED}):")
    lines += [f"- #{pr['number']} {pr.get('title', '')} {pr.get('url', '')}" for pr in unanswered]
    return lines


# Hook


def _reason(error: BaseException) -> str:
    known = (jira.JiraError, jira.CredentialError, op_service_account.ServiceAccountError, SweepError)
    text = str(error) if isinstance(error, known) else type(error).__name__
    return " ".join(text.split())[:300] or type(error).__name__


def _in_background(work: Callable[[], list[str]]) -> tuple[threading.Thread, dict[str, Any]]:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["lines"] = work()
        except BaseException as error:  # noqa: BLE001 - fail open on anything
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def summary(
    now: datetime,
    read_issues: Callable[[], list[dict[str, Any]]] = read_issues,
    read_reviews: Callable[[], dict[str, Any]] = read_reviews,
    deadline: float = DEADLINE_SECONDS,
) -> str:
    parts = [
        ("Validating sweep failed", _in_background(lambda: validating_lines(read_issues(), now))),
        ("Review agreement failed", _in_background(lambda: review_lines(read_reviews()))),
    ]
    end = datetime.now(timezone.utc) + timedelta(seconds=deadline)
    lines = ["Session start sweep (CHB-140): finish these first (docs/process/kanban.md, Validating)."]
    for failure, (thread, box) in parts:
        thread.join(max(0.0, (end - datetime.now(timezone.utc)).total_seconds()))
        if "lines" in box:
            lines += box["lines"]
        elif "error" in box:
            lines.append(f"{failure}: {_reason(box['error'])}")
        else:
            lines.append(f"{failure}: no answer within {deadline:g} s")
    return "\n".join(lines) + "\n"


def main(
    stdin: TextIO = sys.stdin,
    now: datetime | None = None,
    read_issues: Callable[[], list[dict[str, Any]]] = read_issues,
    read_reviews: Callable[[], dict[str, Any]] = read_reviews,
    deadline: float = DEADLINE_SECONDS,
) -> int:
    try:
        stdin.read()  # the event; the summary does not depend on it
    except (OSError, ValueError):
        pass
    try:
        context = summary(now or datetime.now(timezone.utc), read_issues, read_reviews, deadline)
    except Exception as error:  # noqa: BLE001 - fail open
        context = f"Validating sweep failed: {_reason(error)}\n"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
