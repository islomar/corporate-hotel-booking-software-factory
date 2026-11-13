#!/usr/bin/env python3
"""Act in Jira as the agent account "Claude (CHB agent)" (CHB-107).

Usage:
  factory/bin/jira view CHB-12 [--json]
  factory/bin/jira search 'status = "In Review"' [--json]   (sent as project = CHB AND (<jql>))
  factory/bin/jira create --parent CHB-27 --summary TEXT --description-file FILE --priority High|Medium|Low
                                                          (in Backlog, with the Definition of Ready)
  factory/bin/jira create --parent CHB-27 --summary TEXT --description-file FILE --expedite "production bug: <evidence>"
                                                          (priority Highest, straight in In Progress; CHB-157)
  factory/bin/jira transition CHB-12 "In Review"
  factory/bin/jira tick CHB-12 why|trigger|decision|small   (ticks one box of the Definition of Ready)
  factory/bin/jira tick CHB-12 2|TEXT --evidence-file FILE  (ticks one acceptance criterion and posts the evidence; Done needs all ticked)
  factory/bin/jira ready CHB-12 --trigger TEXT            (Backlog to Ready: checks the Definition of Ready)
  factory/bin/jira checklist CHB-12 [CHB-13 ...] | --open  (adds the Definition of Ready where it is missing)
  factory/bin/jira comment CHB-12 --body-file FILE      (FILE "-" reads stdin)
  factory/bin/jira assign CHB-12 --me | --account-id ID
  factory/bin/jira rank CHB-12 [CHB-13 ...] --before CHB-9 | --after CHB-9
  factory/bin/jira section CHB-12 --heading TEXT --body-file FILE [--first]
  factory/bin/jira sort-todo                              (Ready: ready High, Medium, Low, then blocked, then epics)
  factory/bin/jira priority CHB-12 High|Medium|Low
  factory/bin/jira description CHB-12 --file FILE        (replaces the whole description)
  factory/bin/jira edit-comment CHB-12 10071 --body-file FILE
  factory/bin/jira link CHB-88 --blocked-by CHB-146         (a "Blocks" link: CHB-146 blocks CHB-88)

The account email and API token are read from the 1Password item chb-jira-claude.api-token
(fields username and credential, vault chb-factory) on every call, with one `op` call that
uses the 1Password service account token from the GNOME keyring (op_service_account.py).
The Jira token stays in this process: it is never printed, logged, written to a file or
passed to another process.

The account is a CHB project Administrator (Jira Free has no Member role), so this
wrapper is the guardrail. It only touches issues whose key is CHB-<n>, creates only
Tasks under a CHB Epic, and has no delete, no project, board, workflow or permission
writes, and no raw request command. Do not add one. `rank` changes only the rank of
issues. `section` writes only the description field, and changes only one section of it.
In "Acceptance criteria (Validating)", `section` refuses a "When:" line without a "Speed up:" line.
In "Needs human: owner steps", `section` refuses a body without an "Answer:" line (CHB-171).
`priority` writes only the priority field, and only High, Medium or Low (docs/process/priorities.md).
Highest marks an expedite ticket: only `create --expedite` sets it, one open at a time, for a production bug,
a broken pipeline or a leaking secret. It skips Backlog, Ready and every column limit (CHB-157).
A move into the pull list is refused while it holds its board max; epics count toward no column limit.
The pull list is the board column "Ready", or "To Do" on a board without it (pull_list, CHB-153).
"Backlog" holds work not needed yet: it is never sorted, never next and never started.
`sort-todo` changes only the rank of pull-list issues. `create` runs it after creating a ticket in the
pull list, and a start from the pull list runs it before the top-ticket check (CHB-148).
`create` puts a new ticket in Backlog when the board has that column (CHB-153).
Every ticket carries a "Definition of Ready" task list right after "Why it matters" (CHB-154): `create`
adds it, and `checklist` adds it to a ticket without it. `tick` and `checklist` write only the
description field: `tick` changes the state of one task item, `checklist` inserts the section and
changes no other node. `ready` is the only way from Backlog to the pull list: it re-checks every
box (readiness_problems), ticks the boxes it verifies, moves the ticket and posts the trigger as the
comment "Moved to Ready. Trigger: <text>". `transition` refuses that move and any --trigger.
`create` refuses a description that does not open with a non-empty "Why it matters" section (CHB-148).
`description` writes only the description field. `edit-comment` changes one comment body.
`link` creates only "Blocks" links between two different CHB issues, and never removes one.

Descriptions, comments and section bodies are Markdown: headings, bullet and numbered lists,
task lists ("- [ ]" and "- [x]" become Jira checkboxes), `code`, **bold** and links render in Jira (text_to_adf). CHB-<n> links to the ticket, and
"PR #<n>" or "(#<n>" links to the pull request.

Exit codes:
  0  done
  1  Jira refused or failed (HTTP error, unknown status, parent is not an epic,
     an issue that could not be ranked, a search answer with an issue outside CHB)
  2  usage error (also a bad section heading, an empty body or summary, a file that is not
     UTF-8, a duplicate rank key, JQL whose parentheses or quotes do not balance, a ticket
     linked to itself, a move from Backlog to Ready with `transition`, --trigger on `transition`,
     --ready or --trigger on `create`, `ready` without --trigger or on a ticket outside Backlog,
     `tick` of a box that `ready` verifies, priority Highest outside `create --expedite`, an expedite
     reason that does not start with one of the three cases)
  3  no credential: no service account token in the keyring (keyring locked, item absent)
     or `op` failed. The agent stops and reports.
  4  a board rule blocks the transition: the description lacks the section the target status requires, a move from Backlog other than to Ready, Done or Discarded, or a start
     from the pull list that is not its top ticket, or has no "Why it matters" first section or
     no acceptance criteria (docs/process/kanban.md); `ready` on a ticket whose Definition of Ready
     has a box unticked or a check that fails (no "Why it matters", no acceptance criteria, an open
     blocker). The message names each open box. The ticket stays where it is. Also a move into the
     pull list while it holds its max (CHB-157), and `create --expedite` while an expedite ticket is open.
     Also `ready` with trigger 5 ("5. ...") while Ready holds its minimum or has none (CHB-164).
  5  the target column is at its WIP limit (In Progress, In Review, Validating or Needs human). Waiting clears it (CHB-162).
  6  the move is done and Ready is below its minimum (CHB-164). The output lists the Backlog tickets
     to refill it with (key, priority, missing sections). Refill with `ready` and trigger 5 until it exits 0.

`transition` reads the column limits from the board configuration (board 2) on every call.
After a move it prints the free In Progress slots and the next tickets of the pull list (CHB-125).
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python3 -I does not add it

import op_service_account  # noqa: E402

SITE = "https://islomar.atlassian.net"
PROJECT = "CHB"
ITEM = "chb-jira-claude.api-token"
FIELDS = ("username", "credential")
CREDENTIALS_COMMAND = [
    "op", "item", "get", ITEM, "--vault", op_service_account.VAULT,
    "--fields", ",".join(f"label={field}" for field in FIELDS), "--reveal", "--format", "json",
]  # fmt: skip
KEY_PATTERN = re.compile(rf"^{PROJECT}-[1-9][0-9]*$")
URL_PATTERN = re.compile(r"https?://[^\s<>()]*[^\s<>().,;:!?'\"]")
SEARCH_FIELDS = ["summary", "status", "assignee", "issuetype", "parent", "priority"]
PRIORITIES = ("High", "Medium", "Low")  # Lowest exists in Jira but CHB does not use it
# Highest marks an expedite ticket and nothing else (CHB-157): only `create --expedite` sets it.
EXPEDITE_PRIORITY = "Highest"
# The three cases that justify Expedite (CHB-157, docs/process/kanban.md, Expedite). The reason starts with one.
EXPEDITE_CASES = ("production bug", "pipeline broken", "secret leak")
EXPEDITE_PATTERN = re.compile(rf"^\s*(?:{'|'.join(EXPEDITE_CASES)})\s*:\s*\S", re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^(#{1,6}) +(\S.*?)\s*$")
BULLET_PATTERN = re.compile(r"^- (?P<text>.*)$")
ORDERED_PATTERN = re.compile(r"^([1-9][0-9]*)\. (?P<text>.*)$")
TASK_PATTERN = re.compile(r"^- \[(?P<mark>[ xX])\] (?P<text>.*)$")  # a task list item (CHB-154)
ORDER_BY_PATTERN = re.compile(r"\border\s+by\b", re.IGNORECASE)
PULL_URL = "https://github.com/islomar/corporate-hotel-booking-software-factory/pull/"
REFERENCE_PATTERN = re.compile(
    URL_PATTERN.pattern
    + rf"|(?<![\w-])(?P<key>{PROJECT}-[1-9][0-9]*)(?![\w-])"
    + r"|(?:\bPR |(?<=\())#(?P<pr>[1-9][0-9]*)\b"
)
INLINE_PATTERN = re.compile(r"`(?P<code>[^`]+)`|\*\*(?P<bold>[^*]+)\*\*")
RANK_LIMIT = 50  # issues per rank request, the Jira Software limit
BOARD_ID = 2  # the CHB board; its column configuration holds the WIP limits
# The pull list (CHB-153): the column agents pull work from. It is "Ready", or "To Do" until the owner
# renames that column. "Backlog" holds work not needed yet: never sorted, never next, never started.
READY = "Ready"
TO_DO = "To Do"
BACKLOG = "Backlog"
IN_PROGRESS = "In Progress"
NEEDS_HUMAN = "Needs human"
# Columns of started work. Starting a ticket (a move into In Progress from a column that is not
# started work) is blocked while one of them is at or over its max (CHB-127). Moves into In Review
# and Validating record a PR or a merge and are never blocked.
STARTED_WORK = (IN_PROGRESS, "In Review", "Validating")
# Board columns of closed tickets: a ticket moved into one no longer blocks its dependents (CHB-143).
CLOSED = ("Done", "Discarded")
# Target status (casefolded) -> description section it requires (docs/process/kanban.md).
REQUIRED_SECTIONS = {
    "validating": "Acceptance criteria (Validating)",
    "needs human": "Needs human: owner steps",
}
# The top ticket of the pull list (CHB-125, docs/process/kanban.md, Ready) is the highest-ranked non-epic
# ticket that no open ticket blocks (open_blockers). It starts once it has acceptance criteria: a
# heading that starts with "Acceptance criteria" (not "Acceptance criteria (Validating)"), or a line
# that is only that label, with text under it.
# A heading that starts with the label (not the Validating section), or a line that is only the label.
ACCEPTANCE_PATTERN = re.compile(
    r"^#{1,6} +(?:\*\*)?acceptance criteria\b(?!\s*\(validating\))|^\s*(?:\*\*)?acceptance criteria\s*:?\s*(?:\*\*)?\s*:?\s*$",
    re.IGNORECASE,
)
# Open tickets in rank order, for the top of the pull list and for the dependencies. Discarded is in the
# Done status category, so it is not open.
OPEN_JQL = "statusCategory != Done ORDER BY Rank ASC"
OPEN_FIELDS = ("status", "issuetype", "description", "issuelinks", "priority")
# The fields before_transition and ready read of the moving ticket.
ISSUE_FIELDS = "status,description,issuelinks,issuetype,assignee"
# Dependencies are Jira "Blocks" links (CHB-151): inward "is blocked by", outward "blocks". In an issue's
# issuelinks, an entry of this type with an inwardIssue means "this issue is blocked by inwardIssue".
BLOCKS = "Blocks"
OPEN_LIMIT = 1000  # a search answer this long may be cut off; the rank and dependency checks refuse it

# Every ticket opens with this section: the pain, who feels it, the value of solving it (CHB-148).
WHY_HEADING = "Why it matters"
# Pull-list order (CHB-148): ready tickets by priority, then blocked tickets, then epics. A missing or
# unknown priority sorts as Medium, the default of docs/process/priorities.md.
PRIORITY_ORDER = {"Highest": 0, "High": 0, "Medium": 1, "Low": 2, "Lowest": 2}

EXIT_JIRA = 1
EXIT_USAGE = 2
EXIT_CREDENTIAL = 3
EXIT_BLOCKED = 4
EXIT_WIP_LIMIT = 5
EXIT_REFILL = 6  # the move is done and Ready is below its minimum: refill it (CHB-164)


class JiraError(Exception):
    """Jira refused the request or the request makes no sense. Exit code 1."""


class UsageError(Exception):
    """A local input problem, such as an unreadable file. Exit code 2."""


class CredentialError(Exception):
    """The token could not be read. Exit code 3: the agent stops."""


class BlockedError(Exception):
    """A board rule blocks the transition: a missing section, a start out of order. Exit code 4."""


class WipLimitError(BlockedError):
    """A full In Progress, In Review, Validating or Needs human column blocks the move. Exit code 5 (CHB-162).

    Only this refusal clears by itself when a ticket leaves a column, so only it is worth waiting for.
    """


@dataclass(frozen=True)
class Column:
    """One column of the board, with its status ids and its WIP limits (None when unset)."""

    name: str
    status_ids: frozenset[str]
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True)
class Move:
    """What before_transition learned, for after_transition."""

    source: Column | None
    target: Column | None
    columns: tuple[Column, ...] = ()
    comment: str | None = None  # the trigger comment to post after the move (check_trigger, CHB-153)
    epic: bool = False  # an epic counts toward no column limit (CHB-157)
    starts: bool = False  # a start from the pull list that passed every check: the agent becomes the assignee (CHB-187)
    assignee: str | None = None  # the account id the ticket had when the start was checked


@dataclass(frozen=True)
class Box:
    """One box of the Definition of Ready checklist (CHB-154)."""

    name: str  # the name `tick` takes
    prefix: str  # the start of the box text: it finds the box in a description, so keep it when editing the text
    text: str


# The Definition of Ready (CHB-154, docs/process/kanban.md, Backlog): a task list in every ticket, right after
# "Why it matters". `ready` moves a ticket from Backlog to Ready only when every box is ticked and every check holds.
DOR_HEADING = "Definition of Ready"
DOR_BOXES = (
    Box(
        "why",
        "Why it matters",
        "Why it matters states the need and when it becomes real, never the solution. (agent ticks; `ready` checks the section)",
    ),
    Box(
        "trigger",
        "Trigger named",
        "Trigger named with evidence: broken, owner asked (or in the agreed product slice), unblocks work in progress, "
        "or the same pain twice. (agent ticks; `ready` requires the trigger text and posts it)",
    ),
    Box("acceptance", "Acceptance criteria", "Acceptance criteria that can be checked. (`ready` verifies the section and ticks)"),
    Box("unblocked", "Not blocked", 'Not blocked: every "is blocked by" ticket is Done or Discarded. (`ready` verifies the links and ticks)'),
    Box(
        "decision",
        "No owner decision",
        "No owner decision pending (money, lock-in, public exposure, deletion), or its decision ticket is Done. "
        "(agent ticks; a pending decision is a Blocks link that `ready` verifies)",
    ),
    Box("small", "Small", "Small: one PR, about a day of agent work; otherwise split first. (agent ticks)"),
)
VERIFIED_BOXES = ("acceptance", "unblocked")  # `ready` checks and ticks them; `tick` refuses them
DOR_BOX_NAMES = tuple(box.name for box in DOR_BOXES)
JUDGMENT_BOXES = tuple(box.name for box in DOR_BOXES if box.name not in VERIFIED_BOXES)


def read_secret(
    command: list[str],
    token_reader: Callable[[], str] = op_service_account.read_token,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    """Output of `op` run with the service account token in its environment only."""
    try:
        return op_service_account.run_op(command, os.environ, token_reader, run).decode()
    except op_service_account.ServiceAccountError as error:
        raise CredentialError(str(error)) from None


def parse_credentials(output: str) -> tuple[str, str]:
    """Email and token from `op item get --format json` output. No value ever reaches a message."""
    try:
        fields = json.loads(output)
    except ValueError:
        raise CredentialError("op returned output that is not JSON") from None
    if isinstance(fields, dict):
        fields = [fields]
    if not isinstance(fields, list):
        raise CredentialError("op returned JSON that is not a list of fields")
    values = {f.get("label"): f.get("value") for f in fields if isinstance(f, dict)}
    result = []
    for label in FIELDS:
        value = values.get(label)
        if not isinstance(value, str) or not value.strip():
            raise CredentialError(f"1Password field label={label} is missing or empty")
        result.append(value.strip())
    return result[0], result[1]


def read_credentials(reader: Callable[[list[str]], str] = read_secret) -> tuple[str, str]:
    """Email and API token of the agent account, from one read of the 1Password item, on every call."""
    return parse_credentials(reader(CREDENTIALS_COMMAND))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header never follows one to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


Opener = Callable[[urllib.request.Request], Any]


def default_opener() -> Opener:
    opener = urllib.request.build_opener(_NoRedirect)
    return lambda request: opener.open(request, timeout=30)


def text_to_adf(text: str) -> dict[str, Any]:
    """Markdown text to an Atlassian Document (CHB-129).

    Blank lines split blocks. A line "## Title" (1 to 6 #) is a heading. Lines that start with
    "- " form a bullet list, lines that start with "1. " an ordered list; an indented line after
    a list item continues that item. Inline: `code`, **bold**, and URLs become links.
    Other lines are paragraphs, one hard break per line.
    """
    blocks: list[dict[str, Any]] = []
    for lines in _blocks(text):
        heading = HEADING_PATTERN.fullmatch(lines[0])
        if heading:
            level, title = heading.groups()
            blocks.append({"type": "heading", "attrs": {"level": len(level)}, "content": _inline(title)})
            continue
        blocks.extend(_block_nodes(lines))
    return {"type": "doc", "version": 1, "content": blocks}


def _block_nodes(lines: list[str]) -> list[dict[str, Any]]:
    """Split one block into paragraphs and lists, in order."""
    nodes: list[dict[str, Any]] = []
    run: list[str] = []
    kind = ""  # "bullet", "ordered" or "task" while a list is open
    first_number = 1
    items: list[list[str]] = []
    states: list[str] = []  # TODO or DONE per item of a task list

    def close() -> None:
        nonlocal run, items, states, kind
        if run:
            nodes.append(_paragraph(run))
            run = []
        if items:
            nodes.append(_list(kind, items, first_number, states))
            items, states = [], []
        kind = ""

    for line in lines:
        task = TASK_PATTERN.match(line)
        bullet = None if task else BULLET_PATTERN.match(line)
        numbered = ORDERED_PATTERN.match(line)
        match = task or bullet or numbered
        if match:
            new_kind = "task" if task else "bullet" if bullet else "ordered"
            if run or kind != new_kind:
                close()
            if not items and numbered:
                first_number = int(numbered.group(1))
            kind = new_kind
            items.append([match.group("text")])
            states.append("DONE" if task and task.group("mark") in "xX" else "TODO")
        elif items and line[:1].isspace():
            items[-1].append(line.strip())
        else:
            if items:
                close()
            run.append(line)
    close()
    return nodes


def _list(kind: str, items: list[list[str]], first_number: int, states: list[str]) -> dict[str, Any]:
    if kind == "task":  # a Jira task list: its items hold inline nodes, not paragraphs (CHB-154)
        tasks = [
            {"type": "taskItem", "attrs": {"localId": str(uuid.uuid4()), "state": state}, "content": _paragraph(item)["content"]}
            for item, state in zip(items, states)
        ]
        return {"type": "taskList", "attrs": {"localId": str(uuid.uuid4())}, "content": tasks}
    content = [{"type": "listItem", "content": [_paragraph(item)]} for item in items]
    if kind == "ordered":
        return {"type": "orderedList", "attrs": {"order": first_number}, "content": content}
    return {"type": "bulletList", "content": content}


def _blocks(text: str) -> list[list[str]]:
    """Blank lines split blocks, and a heading line is a block of its own."""
    blocks: list[list[str]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        current: list[str] = []
        for line in block.split("\n"):
            if HEADING_PATTERN.fullmatch(line):
                if current:
                    blocks.append(current)
                blocks.append([line])
                current = []
            else:
                current.append(line)
        if current:
            blocks.append(current)
    return blocks


def scope_jql(jql: str) -> str:
    """The JQL limited to the CHB project: project = CHB AND (<jql>) [ORDER BY ...].

    Refuses (UsageError, exit 2) JQL that could leave the parentheses: a ")" without its "(",
    a "(" never closed, an unterminated quote, a backslash outside quotes (Jira may read it
    as an escape the scanner does not), or a parenthesis after ORDER BY (an ORDER BY inside
    parentheses would close them). A trailing ORDER BY stays outside them. The check of the
    answer's issue keys in Jira.search is the final guard.
    """
    masked = []  # the JQL with the text inside quotes blanked, so only the syntax is left
    quote = ""
    escaped = False
    depth = 0
    for char in jql:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
                masked.append(char)
                continue
            masked.append(" ")
            continue
        if char in "\"'":
            quote = char
        elif char == "\\":
            raise UsageError("search JQL has a backslash outside quotes")
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise UsageError("search JQL has a ')' without its '('")
        masked.append(char)
    if quote:
        raise UsageError("search JQL has a quote that is never closed")
    if depth:
        raise UsageError("search JQL has a '(' that is never closed")
    syntax = "".join(masked)
    order = ORDER_BY_PATTERN.search(syntax)
    if order and any(char in "()" for char in syntax[order.start() :]):
        raise UsageError("search JQL has a parenthesis after ORDER BY")
    where, order_by = (jql[: order.start()], " " + jql[order.start() :].strip()) if order else (jql, "")
    where = where.strip()
    return f"project = {PROJECT}{f' AND ({where})' if where else ''}{order_by}"


def check_summary(summary: str) -> None:
    if not summary.strip():
        raise UsageError("summary is empty")


def check_comment_body(body: str) -> None:
    if not body.strip():
        raise UsageError("comment body is empty")


def check_comment_id(comment_id: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", comment_id):
        raise UsageError(f"comment id must be a number, got {comment_id!r}")
    return comment_id


def check_priority(priority: str) -> str:
    if priority == EXPEDITE_PRIORITY:
        raise UsageError(
            f"priority {EXPEDITE_PRIORITY} is only for an expedite ticket: factory/bin/jira create --expedite "
            f'"<case>: <evidence>" (cases: {", ".join(EXPEDITE_CASES)}; docs/process/kanban.md, Expedite)'
        )
    if priority not in PRIORITIES:
        raise UsageError(f"priority must be one of {', '.join(PRIORITIES)}, got {priority!r}")
    return priority


def check_expedite(reason: str) -> str:
    """Refuse (UsageError, exit 2) an expedite reason that does not start with one of the three cases (CHB-157)."""
    if not EXPEDITE_PATTERN.match(reason):
        cases = ", ".join(f'"{case}: <evidence>"' for case in EXPEDITE_CASES)
        raise UsageError(
            f"--expedite needs a reason that starts with one of the three cases and its evidence: {cases}. "
            "Any other work goes to Backlog with create --priority (docs/process/kanban.md, Expedite)"
        )
    return reason.strip()


def _paragraph(lines: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "paragraph", "content": content}


def _inline(line: str) -> list[dict[str, Any]]:
    """`code` and **bold** spans, then links in the plain parts."""
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in INLINE_PATTERN.finditer(line):
        if match.start() > position:
            nodes.extend(_links(line[position : match.start()]))
        code, bold = match.group("code"), match.group("bold")
        if code is not None:
            nodes.append({"type": "text", "text": code, "marks": [{"type": "code"}]})
        else:
            nodes.append({"type": "text", "text": bold, "marks": [{"type": "strong"}]})
        position = match.end()
    if position < len(line):
        nodes.extend(_links(line[position:]))
    return nodes


def _links(line: str) -> list[dict[str, Any]]:
    """URLs, CHB keys and PR references ("PR #44", "(#44") become links."""
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in REFERENCE_PATTERN.finditer(line):
        if match.start() > position:
            nodes.append({"type": "text", "text": line[position : match.start()]})
        text = match.group(0)
        if match.group("key"):
            href = f"{SITE}/browse/{text}"
        elif match.group("pr"):
            href = f"{PULL_URL}{match.group('pr')}"
        else:
            href = text
        nodes.append({"type": "text", "text": text, "marks": [{"type": "link", "attrs": {"href": href}}]})
        position = match.end()
    if position < len(line):
        nodes.append({"type": "text", "text": line[position:]})
    return nodes


def _indent(item: str, width: int) -> str:
    """Indent the continuation lines of a list item, so text_to_adf reads them back into the item."""
    return item.replace("\n", "\n" + " " * width)


def adf_to_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        text = str(node.get("text", ""))
        marks = {mark.get("type") for mark in node.get("marks", []) if isinstance(mark, dict)}
        if "code" in marks:
            return f"`{text}`"
        return f"**{text}**" if "strong" in marks else text
    if kind == "hardBreak":
        return "\n"
    if kind == "bulletList":
        items = (adf_to_text(item).strip("\n") for item in node.get("content", []))
        return "".join(f"- {_indent(item, 2)}\n" for item in items) + "\n"
    if kind == "orderedList":
        start = node.get("attrs", {}).get("order", 1)
        items = (adf_to_text(item).strip("\n") for item in node.get("content", []))
        return "".join(f"{n}. {_indent(item, len(str(n)) + 2)}\n" for n, item in enumerate(items, start)) + "\n"
    if kind == "taskList":
        lines = []
        for item in node.get("content", []):
            if isinstance(item, dict) and item.get("type") == "taskItem":
                mark = "x" if (item.get("attrs") or {}).get("state") == "DONE" else " "
                text = "".join(adf_to_text(child) for child in item.get("content", []))
                lines.append(f"- [{mark}] {_indent(text, 6)}\n")
            else:  # a nested task list
                lines.append(adf_to_text(item).strip("\n") + "\n")
        return "".join(lines) + "\n"
    inner = "".join(adf_to_text(child) for child in node.get("content", []))
    if kind == "doc":
        return inner.strip("\n")
    if kind == "heading":
        return "#" * _level(node) + " " + inner.strip("\n") + "\n\n"
    if kind in {"paragraph", "codeBlock", "listItem", "blockquote", "rule"}:
        return inner.rstrip("\n") + "\n\n"
    return inner


def check_key(key: str) -> str:
    if not KEY_PATTERN.fullmatch(key):
        raise JiraError(f"{key!r} is not a {PROJECT} issue key ({PROJECT}-<n>)")
    return key


def replace_section(description: dict[str, Any] | None, heading: str, body: str, *, first: bool = False) -> dict[str, Any]:
    """Return a copy of the description with the section under `heading` set to `body`.

    The section starts at the top-level heading whose text is `heading` and ends before the next
    heading of the same or a higher level, or at the end. It is replaced by a level-2 heading and
    the body. Without such a heading the section is appended. Other nodes are kept as they are.
    With `first`, the new section goes at the top instead, and the old one is removed wherever it was.
    """
    heading = check_section_input(heading, body)
    doc = description if isinstance(description, dict) and description.get("type") == "doc" else None
    content: list[Any] = list(doc.get("content", [])) if doc else []
    new = [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": heading}]},
        *text_to_adf(acceptance_checkboxes(heading, body))["content"],
    ]
    starts = [i for i, node in enumerate(content) if _is_heading(node) and _heading_text(node) == heading]
    if len(starts) > 1:
        raise JiraError(f"the description has {len(starts)} sections headed {heading!r}; fix it by hand")
    if starts:
        start = starts[0]
        content[start : _section_end(content, start)] = [] if first else new
    if first:
        content[0:0] = new
    elif not starts:
        content.extend(new)
    result = dict(doc) if doc else {"type": "doc", "version": 1}
    result["content"] = content
    return result


def _section_end(content: list[Any], start: int) -> int:
    """Index after the section headed at `start`: the next heading of the same or a higher level, or the end."""
    level = _level(content[start])
    return next((i for i in range(start + 1, len(content)) if _is_heading(content[i]) and _level(content[i]) <= level), len(content))


def with_checklist(description: dict[str, Any]) -> dict[str, Any]:
    """The description with the Definition of Ready added unless it has one (`create`, CHB-154)."""
    return add_checklist(description) or description


def checklist_markdown(done: Collection[str] = ()) -> str:
    """The Definition of Ready as Markdown task lines, with the boxes named in `done` ticked (CHB-154)."""
    return "\n".join(f"- [{'x' if box.name in done else ' '}] {box.text}" for box in DOR_BOXES)


def _content(description: Any) -> list[Any]:
    return list(description.get("content", [])) if isinstance(description, dict) else []


def _checklist_starts(content: list[Any]) -> list[int]:
    return [i for i, node in enumerate(content) if _is_heading(node) and _heading_text(node) == DOR_HEADING]


def checklist_count(description: Any) -> int:
    """How many top-level "Definition of Ready" sections the description has."""
    return len(_checklist_starts(_content(description)))


def add_checklist(description: Any) -> dict[str, Any] | None:
    """A copy of the description with an unticked Definition of Ready, or None when it has one already.

    The section goes right after the "Why it matters" section, with a heading of the same level, so an
    empty Why section stays empty (why_problem). Without a Why section it goes first. No other node changes.
    """
    content = _content(description)
    if _checklist_starts(content):
        return None
    why = next((i for i, node in enumerate(content) if _is_heading(node) and _heading_text(node) == WHY_HEADING), None)
    level = 2 if why is None else _level(content[why])
    at = 0 if why is None else _section_end(content, why)
    heading = {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": DOR_HEADING}]}
    content[at:at] = [heading, *text_to_adf(checklist_markdown())["content"]]
    doc = description if isinstance(description, dict) and description.get("type") == "doc" else None
    result = dict(doc) if doc else {"type": "doc", "version": 1}
    result["content"] = content
    return result


def _checklist_items(description: Any) -> list[dict[str, Any]] | None:
    """The task items of the first Definition of Ready section (the nodes themselves), or None without one."""
    content = _content(description)
    starts = _checklist_starts(content)
    if not starts:
        return None
    section = content[starts[0] + 1 : _section_end(content, starts[0])]
    lists = [node for node in section if isinstance(node, dict) and node.get("type") == "taskList"]
    return [item for node in lists for item in node.get("content", []) if isinstance(item, dict) and item.get("type") == "taskItem"]


def _box_of(item: dict[str, Any]) -> Box | None:
    text = "".join(adf_to_text(child) for child in item.get("content", [])).strip()
    return next((box for box in DOR_BOXES if text.startswith(box.prefix)), None)


def box_states(description: Any) -> dict[str, str] | None:
    """Box name -> TODO or DONE for the boxes of the Definition of Ready, or None without that section."""
    items = _checklist_items(description)
    if items is None:
        return None
    states: dict[str, str] = {}
    for item in items:
        box = _box_of(item)
        if box is not None and box.name not in states:
            states[box.name] = str((item.get("attrs") or {}).get("state", "TODO"))
    return states


def tick_boxes(description: dict[str, Any], names: Collection[str]) -> dict[str, Any]:
    """A copy of the description with the named boxes ticked. Only their state changes; ids and text stay."""
    result = copy.deepcopy(description)
    for item in _checklist_items(result) or []:
        box = _box_of(item)
        if box is not None and box.name in names:
            item["attrs"] = {**(item.get("attrs") or {}), "state": "DONE"}
    return result


def check_box(name: str) -> None:
    """Refuse (UsageError, exit 2) a box that `tick` does not tick."""
    if name in VERIFIED_BOXES:
        raise UsageError(f"box {name!r} is verified, not ticked: factory/bin/jira ready verifies it and ticks it")
    if name not in JUDGMENT_BOXES:
        raise UsageError(f"unknown box {name!r}; tick one of: {', '.join(JUDGMENT_BOXES)}")


def readiness_problems(key: str, description: Any, blocking: list[str]) -> list[str]:
    """Why the ticket may not move to Ready (CHB-154): each open box, by its text. Empty when it may.

    A judgment box must be ticked. A verified box must pass its check, ticked or not: ticking cannot
    replace checking. `blocking` are the open tickets that block it (open_blockers).
    """
    count = checklist_count(description)
    if count == 0:
        return [f"its description has no {DOR_HEADING} section; add it: factory/bin/jira checklist {key}"]
    if count > 1:
        return [f"its description has {count} {DOR_HEADING} sections; fix it by hand"]
    states = box_states(description) or {}
    problems = []
    for box in DOR_BOXES:
        if states.get(box.name) is None:
            problems.append(f"{box.prefix}: the box is missing from the checklist; fix it by hand")
        elif box.name in JUDGMENT_BOXES and states[box.name] != "DONE":
            problems.append(f"{box.prefix}: not ticked (factory/bin/jira tick {key} {box.name})")
        why = why_problem(description) if box.name == "why" else None
        if why:
            problems.append(f'{box.prefix}: the description {why} (factory/bin/jira section {key} --heading "{WHY_HEADING}" --first --body-file FILE)')
        if box.name == "acceptance" and not has_acceptance_criteria(description):
            problems.append(f'{box.prefix}: no acceptance criteria (factory/bin/jira section {key} --heading "Acceptance criteria" --body-file FILE)')
        if box.name == "unblocked" and blocking:
            problems.append(f"{box.prefix}: blocked by open tickets: {', '.join(blocking)}")
    return problems


def check_section_input(heading: str, body: str) -> str:
    """Refuse a bad heading or an empty body (UsageError, exit 2). Return the stripped heading."""
    heading = heading.strip()
    if not heading or "\n" in heading:
        raise UsageError("heading must be one non-empty line")
    if not body.strip():
        raise UsageError("section body is empty")
    return heading


# A "When:" or "Speed up:" line of an acceptance section, with an optional bullet and bold (CHB-133).
_LABEL = r"^\s*(?:[-*]|\d+\.)?\s*(?:\*\*)?{}(?:\*\*)?\s*:"
WHEN_PATTERN = re.compile(_LABEL.format("when"), re.IGNORECASE)
SPEED_UP_PATTERN = re.compile(_LABEL.format(r"speed[ -]up"), re.IGNORECASE)


def check_speed_up(heading: str, body: str) -> None:
    """Refuse an acceptance section where a "When:" line has no "Speed up:" line (exit 2, CHB-133).

    Applies only to the Validating section. Each "When:" line opens a part that runs to the next
    "When:" line or the end of the body, and that part needs a "Speed up:" line.
    """
    if heading.strip() != REQUIRED_SECTIONS["validating"]:
        return
    part: list[str] = []
    for line in [*body.splitlines(), None]:
        if line is None or WHEN_PATTERN.match(line):
            if part and not any(SPEED_UP_PATTERN.match(other) for other in part):
                raise UsageError(
                    f"{part[0].strip()!r} has no Speed up line. After each When line add "
                    "'- Speed up: <exact step the owner can take>' or '- Speed up: cannot be sped up (<reason>)'"
                )
            part = [line] if line is not None else []
        elif part:
            part.append(line)


ANSWER_PATTERN = re.compile(_LABEL.format("answer"), re.IGNORECASE)


def check_answer_line(heading: str, body: str) -> None:
    """Refuse a "Needs human: owner steps" section without an "Answer:" line (exit 2, CHB-171).

    The line says where the owner answers: in the Claude Code chat, or in a ticket comment that the
    next session start picks up (factory/hooks/validating_sweep.py).
    """
    if heading.strip() != REQUIRED_SECTIONS["needs human"]:
        return
    if not any(ANSWER_PATTERN.match(line) for line in body.splitlines()):
        raise UsageError(
            "the section has no Answer line. Add "
            "'- Answer: <where the owner answers: in the Claude Code chat, or in a comment on this ticket, "
            "which the next session start picks up>'"
        )


def check_link_input(blocked: str, blocker: str) -> None:
    """Both keys are CHB keys and differ. A wrong key is a usage error (exit 2), checked before any request."""
    for key in (blocked, blocker):
        if not KEY_PATTERN.fullmatch(key):
            raise UsageError(f"{key!r} is not a {PROJECT} issue key ({PROJECT}-<n>); only CHB tickets are linked")
    if blocked == blocker:
        raise UsageError(f"{blocked} cannot be blocked by itself")
def why_problem(description: Any) -> str | None:
    """What is wrong with the description's opening, or None when it opens with a non-empty
    "Why it matters" section (CHB-148).

    The first node must be a heading (any level) with exactly that text, and a node with text must
    follow before the next heading of the same or a higher level. Nothing may come before it.
    """
    content = description.get("content", []) if isinstance(description, dict) else []
    opening = content[0] if content else None
    if not (_is_heading(opening) and _heading_text(opening) == WHY_HEADING):
        return f"does not start with a section headed {WHY_HEADING!r}"
    level = _level(opening)
    for following in content[1:]:
        if _is_heading(following):
            if _level(following) <= level:
                break
            continue
        if adf_to_text(following).strip():
            return None
    return f"has a {WHY_HEADING!r} section that is empty"


def check_why_text(text: str) -> None:
    """Refuse (UsageError, exit 2) a new description that does not open with "Why it matters"."""
    problem = why_problem(text_to_adf(text))
    if problem:
        raise UsageError(
            f"the description {problem}. Start it with '## {WHY_HEADING}' and 2 to 5 plain sentences: "
            "the pain, who feels it (the owner, users of the app, or agents) and the value of solving it "
            "(docs/process/kanban.md, Ready)"
        )


def check_rank_input(keys: list[str], anchor: str) -> None:
    """Refuse a rank request that makes no sense (UsageError, exit 2). Keys are checked apart."""
    if not keys or len(keys) > RANK_LIMIT:
        raise UsageError(f"rank takes 1 to {RANK_LIMIT} issues, got {len(keys)}")
    if len(set(keys)) != len(keys) or anchor in keys:
        raise UsageError("each issue may appear once, and not as its own anchor")


def _heading_text(node: dict[str, Any]) -> str:
    """The text of a heading, without the "## " that adf_to_text puts in front."""
    return "".join(adf_to_text(child) for child in node.get("content", [])).strip()


def _is_heading(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "heading"


def _level(node: dict[str, Any]) -> int:
    level = node.get("attrs", {}).get("level", 1)
    return level if isinstance(level, int) else 1


class Jira:
    def __init__(self, email: str, token: str, opener: Opener, site: str = SITE) -> None:
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._auth = f"Basic {credentials}"
        self._opener = opener
        self._site = site

    def send(self, method: str, path: str, body: object | None = None) -> tuple[int, bytes]:
        """Send one request. Return the HTTP status and the raw answer. HTTP errors raise JiraError."""
        request = urllib.request.Request(  # noqa: S310 - fixed https site
            self._site + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": self._auth, "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request) as response:
                return getattr(response, "status", 200), response.read()
        except urllib.error.HTTPError as error:
            raise JiraError(f"{method} {path}: HTTP {error.code} {_jira_messages(error)}") from None
        except urllib.error.URLError as error:
            raise JiraError(f"{method} {path}: {error.reason}") from None
        except OSError as error:  # timeouts and resets while reading the response
            raise JiraError(f"{method} {path}: {type(error).__name__}") from None

    def request(self, method: str, path: str, body: object | None = None, *, empty_ok: bool = False) -> dict[str, Any]:
        """Send one request. The answer must be a JSON object, or empty where empty_ok (HTTP 204)."""
        _, raw = self.send(method, path, body)
        return _json_object(method, path, raw, empty_ok=empty_ok)

    def myself(self) -> dict[str, Any]:
        return self.request("GET", "/rest/api/3/myself")

    def view(self, key: str) -> dict[str, Any]:
        fields = "summary,status,priority,assignee,issuetype,parent,description,comment,issuelinks"
        return self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields={fields}")

    def search(self, jql: str, limit: int = 500, fields: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Issues of the CHB project that match the JQL (scope_jql). An issue outside CHB raises JiraError."""
        jql = scope_jql(jql)
        names = list(SEARCH_FIELDS if fields is None else fields)
        issues: list[dict[str, Any]] = []
        page_token = None
        while len(issues) < limit:
            body: dict[str, Any] = {"jql": jql, "fields": names, "maxResults": min(100, limit - len(issues))}
            if page_token:
                body["nextPageToken"] = page_token
            page = self.request("POST", "/rest/api/3/search/jql", body)
            for issue in page.get("issues", []):
                if not KEY_PATTERN.fullmatch(str(issue.get("key", ""))):
                    raise JiraError(f"search answered with an issue outside {PROJECT}; nothing printed")
                issues.append(issue)
            page_token = page.get("nextPageToken")
            if page.get("isLast", True) or not page_token:
                break
        return issues

    def create(self, parent: str, summary: str, description: str, priority: str) -> str:
        check_priority(priority)
        return self._create(parent, summary, description, priority)

    def create_expedite(self, parent: str, summary: str, description: str, reason: str) -> str:
        """Create an expedite ticket, priority Highest (CHB-157). Refuses (BlockedError) while one is open.

        start_expedite moves it to In Progress. Nothing else sets Highest, so an open Highest ticket is an open expedite ticket.
        """
        check_expedite(reason)  # the guard that makes Highest legitimate; _create checks the summary and description
        open_ones = self.open_expedite()
        if open_ones:
            raise BlockedError(
                f"{', '.join(open_ones)} is an open expedite ticket; only one may be open at a time. "
                f"Finish it first, or tell the owner (docs/process/kanban.md, Expedite)."
            )
        return self._create(parent, summary, description, EXPEDITE_PRIORITY)

    def open_expedite(self) -> list[str]:
        """Keys of the open tickets with priority Highest: the open expedite tickets (CHB-157)."""
        return [str(issue["key"]) for issue in self.open_issues() if _priority_name(issue) == EXPEDITE_PRIORITY]

    def move_expedite(self, key: str) -> None:
        """Move a new expedite ticket straight to In Progress, past Backlog, Ready and every column limit (CHB-157).

        The move skips before_transition: it refuses a start from Backlog, and a full column. One open
        expedite ticket at a time keeps the excess at one.
        """
        in_progress = next((column for column in self.board_columns() if column.name == IN_PROGRESS), None)
        if in_progress is None:
            raise JiraError(f"the board has no column {IN_PROGRESS!r}")
        transitions = self.request("GET", f"/rest/api/3/issue/{check_key(key)}/transitions")["transitions"]
        match = next((t for t in transitions if str(t["to"]["id"]) in in_progress.status_ids), None)
        if match is None:
            raise JiraError(f"{key} has no transition into {IN_PROGRESS}")
        self.request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}}, empty_ok=True)

    def finish_expedite(self, key: str, reason: str) -> list[str]:
        """After the move: comment the reason, assign the agent, and check that no other expedite ticket
        opened meanwhile (two agents can both pass the one-at-a-time check). Return a notice for each
        step that failed or found a problem; the ticket is in In Progress whatever happens here.
        """
        notices = []
        body = (
            f"Expedite: {reason.strip()}\n\nCreated straight in {IN_PROGRESS}, past {BACKLOG}, {READY} and the column limits "
            "(docs/process/kanban.md, Expedite)."
        )
        steps: list[tuple[Callable[[], object], str]] = [
            (lambda: self.comment(key, body), f"post it: factory/bin/jira comment {key} --body-file FILE with 'Expedite: <reason>'"),
            (lambda: self.assign(key, self.myself()["accountId"]), f"run factory/bin/jira assign {key} --me"),
        ]
        for step, fix in steps:
            try:
                step()
            except JiraError as error:
                notices.append(f"{key} is in {IN_PROGRESS}, but a step failed: {error}; {fix}")
        try:
            open_ones = self.open_expedite()
        except JiraError as error:
            notices.append(f"could not check for other open expedite tickets: {error}")
        else:
            if len(open_ones) > 1:
                notices.append(f"more than one open expedite ticket: {', '.join(open_ones)}. Tell the owner")
        return notices

    def _create(self, parent: str, summary: str, description: str, priority: str) -> str:
        check_summary(summary)
        check_why_text(description)
        epic = self.request("GET", f"/rest/api/3/issue/{check_key(parent)}?fields=issuetype")
        if epic["fields"]["issuetype"]["name"] != "Epic":
            raise JiraError(f"parent {parent} is a {epic['fields']['issuetype']['name']}, not an Epic")
        created = self.request(
            "POST",
            "/rest/api/3/issue",
            {
                "fields": {
                    "project": {"key": PROJECT},
                    "issuetype": {"name": "Task"},
                    "parent": {"key": parent},
                    "summary": summary.strip(),
                    "description": with_checklist(text_to_adf(acceptance_checkboxes_in(description))),
                    "priority": {"name": priority},
                }
            },
        )
        return str(created["key"])

    def transition(
        self, key: str, status: str, trigger: str | None = None, *, prepare: Callable[[], None] | None = None
    ) -> tuple[str, list[str]]:
        """Move the issue. Return the status name and notices to print (a minimum, the free slots).

        `status` is a status name or, when no status has that name, a board column name (CHB-153).
        A move from Backlog into the pull list needs `trigger`, posted as a comment after the move
        (check_trigger). Only `ready` passes a trigger (CHB-154): the CLI `transition` refuses --trigger,
        so it refuses that move too. `prepare` runs once every check has passed, just before the move.
        """
        check_key(key)
        transitions = self.request("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
        matches = [t for t in transitions if t["to"]["name"].casefold() == status.casefold()]
        if not matches:
            column = next((c for c in self.board_columns() if c.name.casefold() == status.casefold()), None)
            matches = [t for t in transitions if column is not None and str(t["to"]["id"]) in column.status_ids]
        if not matches:
            names = sorted({t["to"]["name"] for t in transitions})
            raise JiraError(f"{key} cannot move to {status!r}. Allowed: {', '.join(names) or 'none'}")
        target = matches[0]["to"]["name"]
        move = before_transition(self, key, target, status_id=str(matches[0]["to"]["id"]), trigger=trigger)
        comment = move.comment
        if prepare is not None:
            prepare()
        if move.starts:
            self.assign_agent_for_start(key, move)
        self.request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": matches[0]["id"]}}, empty_ok=True)
        if comment:
            try:
                self.comment(key, comment)
            except JiraError as error:
                raise JiraError(
                    f"{key} moved to {move.target.name if move.target else target}, but the trigger comment failed: {error}. "
                    f"Post it: factory/bin/jira comment {key} --body-file FILE with the line {comment!r}"
                ) from None
        return target, after_transition(self, key, move)

    def assign_agent_for_start(self, key: str, move: Move) -> None:
        """Assign the agent account to a ticket the start has accepted, unless it is assigned to it already (CHB-187).

        Runs after every start check and before the move, so a refused start assigns nobody. A failure
        here is a JiraError before the move: the ticket stays in the pull list.
        """
        me = str(self.myself()["accountId"])
        if move.assignee != me:
            self.assign(key, me)

    def place_created(self, key: str) -> Column | None:
        """Put a ticket just created in Backlog (CHB-153). Return its column. `ready` moves it on (CHB-154).

        Without a Backlog column (before the owner adds it), the ticket stays where Jira put it, the
        pull list, and nothing is read. Jira may put a new ticket in either column, so the status is
        read first and the ticket moves only when it is elsewhere. The move skips the board rules:
        neither column has any.
        """
        columns = self.board_columns()
        backlog = backlog_column(columns)
        if backlog is None:
            return pull_list(columns)
        status = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields=status")["fields"]["status"]
        if str(status["id"]) in backlog.status_ids:
            return backlog
        transitions = self.request("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
        match = next((t for t in transitions if str(t["to"]["id"]) in backlog.status_ids), None)
        if match is None:
            raise JiraError(f"{key} has no transition into {backlog.name}")
        self.request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}}, empty_ok=True)
        return backlog

    def ready(self, key: str, trigger: str) -> tuple[str, list[str]]:
        """Move a Backlog ticket to the pull list once its Definition of Ready holds (CHB-154). The only way into Ready.

        Refuses (UsageError) a ticket outside Backlog. Refuses (BlockedError) while readiness_problems names a
        box. Then moves the ticket (transition), ticking the verified boxes just before the move, and posts
        "Moved to Ready. Trigger: <text>". A move that a check refuses writes nothing. A move request that
        fails after the tick leaves the verified boxes ticked in Backlog: harmless, `ready` checks them again.
        """
        fields = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields={ISSUE_FIELDS}")["fields"]
        columns = self.board_columns()
        backlog, pull = backlog_column(columns), pull_list(columns)
        if backlog is None or pull is None or str(fields["status"]["id"]) not in backlog.status_ids:
            raise UsageError(f"ready moves a ticket from {BACKLOG} to {pull.name if pull else READY}; {key} is in {fields['status']['name']}")
        if is_refill_trigger(trigger):
            check_refill_trigger(self, key, pull)
        description = fields.get("description")
        blocking = open_blockers(fields, {str(issue["key"]) for issue in self.open_issues()}) if blockers(fields) else []
        problems = readiness_problems(key, description, blocking)
        if problems:
            raise BlockedError(
                f"{key} is not ready: {'; '.join(problems)}. It stays in {BACKLOG} (docs/process/kanban.md, Backlog, {DOR_HEADING})."
            )

        def tick_verified() -> None:
            """Tick the verified boxes on a fresh read, once every check of the move has passed.

            An edit made during the checks is kept; one made after this read is lost.
            """
            current = self.request("GET", f"/rest/api/3/issue/{key}?fields=description")["fields"].get("description")
            states = box_states(current) or {}
            if isinstance(current, dict) and any(states.get(name) == "TODO" for name in VERIFIED_BOXES):
                ticked = tick_boxes(current, VERIFIED_BOXES)
                self.request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"description": ticked}}, empty_ok=True)

        return self.transition(key, pull.name, trigger, prepare=tick_verified)

    def tick(self, key: str, box: str) -> bool:
        """Tick one judgment box of the Definition of Ready. Return False when it was ticked already."""
        check_box(box)
        current = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields=description")["fields"].get("description")
        count = checklist_count(current)
        if count != 1:
            fix = f"add it: factory/bin/jira checklist {key}" if count == 0 else "fix it by hand"
            raise JiraError(f"{key} has {count} {DOR_HEADING} sections; {fix}")
        state = (box_states(current) or {}).get(box)
        if state is None:
            prefix = next(b.prefix for b in DOR_BOXES if b.name == box)
            raise JiraError(f"{key}'s {DOR_HEADING} has no box that starts with {prefix!r}; fix it by hand")
        if state == "DONE":
            return False
        self.request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"description": tick_boxes(current, {box})}}, empty_ok=True)
        return True

    def tick_criterion(self, key: str, target: str, evidence: str) -> tuple[int, str, bool]:
        """Tick one acceptance criterion and post its evidence (CHB-185). Return (number, text, changed).

        The evidence comment goes first: a failed comment leaves the box open, and a second run is safe.
        A criterion that is ticked already writes nothing.
        """
        current = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields=description")["fields"].get("description")
        number, text, done = find_criterion(current, target)
        if done:
            return number, text, False
        self.comment(key, f"Criterion {number} checked: {text}\n\nEvidence: {evidence.strip()}")
        fresh = self.request("GET", f"/rest/api/3/issue/{key}?fields=description")["fields"].get("description")
        number, _, _ = find_criterion(fresh, str(number))
        self.request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"description": tick_criterion(fresh, number)}}, empty_ok=True)
        return number, text, True

    def checklist(self, key: str) -> str:
        """Add the Definition of Ready to one ticket, from a fresh read. Return "added", "already there" or
        "skipped (epic)". Two or more sections raise JiraError: nothing is written then."""
        fields = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields=description,issuetype")["fields"]
        if fields["issuetype"]["name"] == "Epic":
            return "skipped (epic)"
        current = fields.get("description")
        count = checklist_count(current)
        if count > 1:
            raise JiraError(f"{count} {DOR_HEADING} sections; fix it by hand")
        updated = add_checklist(current)
        if updated is None:
            return "already there"
        self.request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"description": updated}}, empty_ok=True)
        return "added"

    def board_columns(self) -> list[Column]:
        """The columns of the CHB board with their WIP limits. The board is the only source of limits."""
        config = self.request("GET", f"/rest/agile/1.0/board/{BOARD_ID}/configuration")
        return [
            Column(
                name=str(column["name"]),
                status_ids=frozenset(str(status["id"]) for status in column.get("statuses", [])),
                min=_limit(column.get("min")),
                max=_limit(column.get("max")),
            )
            for column in config["columnConfig"]["columns"]
        ]

    def open_issues(self) -> list[dict[str, Any]]:
        """The CHB issues not in a Done-category status, in rank order, with status, type and description.

        A cut-off answer would hide open dependencies and ranks, so it raises JiraError instead.
        """
        issues = self.search(OPEN_JQL, limit=OPEN_LIMIT, fields=OPEN_FIELDS)
        if len(issues) >= OPEN_LIMIT:
            raise JiraError(f"{OPEN_LIMIT} or more open tickets: the search may be cut off, so ranks and dependencies are unknown")
        return issues

    def sort_todo(self, created: tuple[str, str, str] | None = None) -> tuple[TodoOrder, int]:
        """Sort the pull list (apply_todo_order). Return the order and the number of rank requests sent.

        `created` is (key, Markdown description, priority) of a Task just created. The search can miss
        a ticket created a moment ago. Jira ranks a new ticket last, so it is then sorted as the last
        ticket of the pull list.
        """
        columns = self.board_columns()
        issues = self.open_issues()
        to_do = pull_list(columns)
        if created is not None and to_do is not None and to_do.status_ids:
            key, description, priority = created
            if key not in {issue["key"] for issue in issues}:
                fields = {
                    "status": {"id": min(to_do.status_ids)},
                    "issuetype": {"name": "Task"},
                    "description": text_to_adf(description),
                    "priority": {"name": priority},
                }
                issues.append({"key": key, "fields": fields})
        return apply_todo_order(self, issues, columns)

    def keys_in(self, column: Column, *, excluding: str) -> list[str]:
        """Keys of the CHB issues in the column's statuses, without `excluding` and without epics (CHB-157)."""
        if not column.status_ids:
            return []
        ids = ", ".join(sorted(column.status_ids))
        jql = f"status in ({ids}) AND key != {check_key(excluding)} AND issuetype != Epic ORDER BY key ASC"
        return [str(issue["key"]) for issue in self.search(jql, limit=100)]

    def comment(self, key: str, body: str) -> str:
        check_comment_body(body)
        created = self.request("POST", f"/rest/api/3/issue/{check_key(key)}/comment", {"body": text_to_adf(body)})
        return str(created["id"])

    def agent_comment_times(self, key: str) -> list[datetime]:
        """When the agent account (this client's login) commented on the ticket, newest first, from the last 100 comments (CHB-163)."""
        me = self.myself()["accountId"]
        answer = self.request("GET", f"/rest/api/3/issue/{check_key(key)}/comment?orderBy=-created&maxResults=100")
        times: list[datetime] = []
        for comment in answer.get("comments", []):
            if (comment.get("author") or {}).get("accountId") != me:
                continue
            try:
                times.append(datetime.fromisoformat(str(comment["created"])))
            except (KeyError, ValueError):
                raise JiraError(f"{key}: a comment has no readable creation time") from None
        return times

    def assign(self, key: str, account_id: str) -> None:
        self.request("PUT", f"/rest/api/3/issue/{check_key(key)}/assignee", {"accountId": account_id}, empty_ok=True)

    def rank(self, keys: list[str], *, before: str | None = None, after: str | None = None) -> None:
        """Move the issues, in the given order, just before or just after another issue."""
        if (before is None) == (after is None):
            raise UsageError("rank needs exactly one of before or after")
        anchor = check_key(before or after or "")
        for key in keys:
            check_key(key)
        check_rank_input(keys, anchor)
        side = "rankBeforeIssue" if before else "rankAfterIssue"
        path = "/rest/agile/1.0/issue/rank"
        status, raw = self.send("PUT", path, {"issues": keys, side: anchor})
        if status in (200, 204) and not raw.strip():
            return
        data = _json_object("PUT", path, raw, empty_ok=True)
        entries = data.get("entries")
        entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        failed = [e for e in entries if e.get("errors") or e.get("status") not in (None, 200, 204)]
        if status not in (200, 204) or failed:
            details = "; ".join(f"{e.get('issueKey', e.get('issueId', '?'))}: {_errors(e.get('errors'))}" for e in failed)
            raise JiraError(f"rank failed for some issues (HTTP {status}): {details or 'no details'}")

    def set_priority(self, key: str, priority: str) -> None:
        body = {"fields": {"priority": {"name": check_priority(priority)}}}
        self.request("PUT", f"/rest/api/3/issue/{check_key(key)}", body, empty_ok=True)

    def edit_comment(self, key: str, comment_id: str, body: str) -> None:
        """Replace the body of one of the agent's own comments on this issue.

        The account is a project Administrator, so Jira would let it edit anyone's comment.
        The wrapper refuses a comment by another author (exit 1) before any write.
        """
        check_comment_body(body)
        path = f"/rest/api/3/issue/{check_key(key)}/comment/{check_comment_id(comment_id)}"
        author = self.request("GET", path).get("author", {}).get("accountId")
        if author != self.myself()["accountId"]:
            raise JiraError(f"comment {comment_id} on {key} was not written by the agent; only the agent's own comments can be edited")
        self.request("PUT", path, {"body": text_to_adf(body)})

    def set_description(self, key: str, text: str) -> None:
        """Replace the whole description with the Markdown text."""
        if not text.strip():
            raise UsageError("description is empty")
        new = text_to_adf(text)
        status = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields=status")["fields"]["status"]["name"]
        heading = REQUIRED_SECTIONS.get(str(status).casefold())
        if heading and not has_section(new, heading):
            raise BlockedError(
                f"{key} is in {status}, which requires a section headed {heading!r}; the new description has none. "
                f"Keep that section in the file (## {heading})."
            )
        body = {"fields": {"description": new}}
        self.request("PUT", f"/rest/api/3/issue/{check_key(key)}", body, empty_ok=True)

    def blocked_by(self, key: str) -> list[str]:
        """Keys of the issues that block this one, from a fresh read of its links."""
        fields = self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields=issuelinks")["fields"]
        return blockers(fields)

    def link(self, blocked: str, blocker: str) -> bool:
        """Make `blocker` block `blocked` with a "Blocks" link. Return False when that link exists already.

        The POST names the sides the other way round from a read: in the POST body the blocker is
        inwardIssue and the blocked issue outwardIssue; a read of the blocked issue then lists the blocker
        as inwardIssue ("is blocked by"). Checked live on 2026-11-10 (CHB-151): the opposite body made
        CHB-114 block CHB-17. The links are read again after the POST: a link stored the other way round
        would block the wrong ticket, so it raises JiraError.
        """
        check_link_input(blocked, blocker)
        if blocker in self.blocked_by(blocked):
            return False
        body = {"type": {"name": BLOCKS}, "inwardIssue": {"key": blocker}, "outwardIssue": {"key": blocked}}
        self.request("POST", "/rest/api/3/issueLink", body, empty_ok=True)
        if blocker not in self.blocked_by(blocked):
            raise JiraError(
                f"{blocked} does not show {blocker} as a blocker after the link. "
                "Check both tickets in Jira; the owner removes a wrong link in the Jira UI."
            )
        return True

    def set_section(self, key: str, heading: str, body: str, *, first: bool = False) -> bool:
        """Set one section of the description. Return False when it already had that content.

        With `first`, or for the "Why it matters" heading, the section goes at the top (CHB-148).
        """
        check_key(key)
        current = self.request("GET", f"/rest/api/3/issue/{key}?fields=description")["fields"].get("description")
        updated = replace_section(current, heading, body, first=first or heading.strip() == WHY_HEADING)
        if updated == current:
            return False
        self.request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"description": updated}}, empty_ok=True)
        return True


def _errors(errors: Any) -> str:
    return ", ".join(map(str, errors)) if isinstance(errors, list) else str(errors or "")


def _json_object(method: str, path: str, raw: bytes, *, empty_ok: bool) -> dict[str, Any]:
    if not raw.strip():
        if empty_ok:
            return {}
        raise JiraError(f"{method} {path}: empty response")
    try:
        data = json.loads(raw)
    except ValueError:
        raise JiraError(f"{method} {path}: response is not JSON") from None
    if not isinstance(data, dict):
        raise JiraError(f"{method} {path}: response is not a JSON object")
    return data


def pull_list(columns: Sequence[Column]) -> Column | None:
    """The column agents pull work from (CHB-153): "Ready", else "To Do". Never Backlog."""
    by_name = {column.name: column for column in columns}
    return by_name.get(READY) or by_name.get(TO_DO)


def backlog_column(columns: Sequence[Column]) -> Column | None:
    return next((column for column in columns if column.name == BACKLOG), None)


REFILL_TRIGGER = re.compile(r"\s*5(?!\d)")


class RefillNotice(str):
    """The first line of the Ready refill notice. A move that prints it exits 6 (CHB-164)."""


def is_refill_trigger(trigger: str | None) -> bool:
    """True when the trigger text names trigger 5: "5. Ready is below its minimum (0/3)" (docs/process/kanban.md, Backlog)."""
    return bool(trigger and REFILL_TRIGGER.match(trigger))


def check_refill_trigger(jira: Jira, key: str, pull: Column | None) -> None:
    """Trigger 5 holds only while Ready is below its minimum. Refuses (BlockedError, exit 4) otherwise (CHB-164)."""
    if pull is None or pull.min is None:
        raise BlockedError(f"trigger 5 needs a {READY} minimum on the board, and there is none")
    count = len(jira.keys_in(pull, excluding=key))
    if count >= pull.min:
        raise BlockedError(
            f"trigger 5 (Ready is below its minimum) does not hold: {pull.name} holds {count} of its minimum {pull.min}. "
            "Name trigger 1, 2, 3 or 4 with its evidence, or leave the ticket in Backlog (docs/process/kanban.md, Backlog)."
        )


def _missing_for_ready(fields: dict[str, Any]) -> list[str]:
    """The sections a Backlog ticket still needs before `ready` accepts it, by name."""
    description = fields.get("description")
    missing = []
    if why_problem(description):
        missing.append(WHY_HEADING)
    if not has_acceptance_criteria(description):
        missing.append("Acceptance criteria")
    if checklist_count(description) != 1:
        missing.append(DOR_HEADING)
    return missing


def refill_lines(
    pull: Column, count: int, issues: list[dict[str, Any]], columns: Sequence[Column], *, exclude: str | None = None
) -> list[str]:
    """The refill notice (CHB-164): Ready holds `count` of its minimum. Its first line is a RefillNotice.

    The candidates are the Backlog tickets that can meet the Definition of Ready: no epic, no open blocker.
    Highest priority first, then rank order (open_issues is in rank order), as many as Ready lacks.
    A candidate lists the sections it still lacks: the agent writes the acceptance criteria, or splits the ticket, first.
    """
    minimum = pull.min or 0
    lines = [
        RefillNotice(
            f"{pull.name} is below its minimum ({count}/{minimum}): refill it from {BACKLOG} now, "
            f'factory/bin/jira ready CHB-<n> --trigger "5. {pull.name} is below its minimum ({count}/{minimum})" '
            "(docs/process/kanban.md, Backlog). Candidates, highest priority and rank first:"
        )
    ]
    backlog = backlog_column(columns)
    open_keys = {str(issue["key"]) for issue in issues}
    found = [
        issue
        for issue in issues
        if backlog is not None
        and str(issue["fields"]["status"]["id"]) in backlog.status_ids
        and issue["key"] != exclude
        and issue["fields"]["issuetype"]["name"] != "Epic"
        and not open_blockers(issue["fields"], open_keys)
    ]
    found.sort(key=_priority_order)
    for issue in found[: max(minimum - count, 1)]:
        missing = _missing_for_ready(issue["fields"])
        state = f"missing {', '.join(missing)}" if missing else "complete"
        lines.append(f"  {issue['key']} ({_priority_name(issue) or 'Medium'}): {state}")
    if len(lines) == 1:
        lines.append(f"  none: no {BACKLOG} ticket can meet the {DOR_HEADING}; tell the owner")
    return lines


def check_trigger(key: str, move: Move, trigger: str | None) -> str | None:
    """The trigger comment for a move from Backlog into the pull list, or None for any other move (CHB-153).

    Refuses (UsageError, exit 2) that move without a trigger. Only `ready` passes a trigger, after it saw the
    ticket in Backlog; a trigger on another move means the ticket moved since, so nothing moves (JiraError).
    """
    columns = list(move.columns)
    pull = pull_list(columns)
    backlog = backlog_column(columns)
    promotes = backlog is not None and move.source == backlog and move.target is not None and move.target == pull
    if promotes and not trigger:
        name = pull.name if pull else READY
        raise UsageError(
            f'{key} moves from {BACKLOG} to {name} only with factory/bin/jira ready {key} --trigger "<which trigger and its evidence>", '
            f"once every box of its {DOR_HEADING} is ticked. Triggers: 1. something is broken; 2. the owner asked for it; "
            "3. it unblocks work in progress; 4. the same pain appeared twice; 5. Ready is below its minimum (docs/process/kanban.md, Backlog)."
        )
    if trigger and not promotes:
        raise JiraError(f"{key} left {BACKLOG} while ready checked it; nothing moved. Check its status and run ready again")
    if not promotes or move.target is None:
        return None
    return f"Moved to {move.target.name}. Trigger: {trigger}"


def before_transition(jira: Jira, key: str, status: str, *, status_id: str, trigger: str | None = None) -> Move:
    """Called with the target status just before every transition POST. Raise to block the move.

    Raises BlockedError (exit 4) when the target status needs a description section that is
    missing, and WipLimitError (exit 5) when a WIP limit blocks the move (docs/process/kanban.md, CHB-127, CHB-162):
    - starting work (a move into In Progress from a column that is not started work) is blocked
      while In Progress, In Review or Validating holds at least its board maximum of other tickets;
    - a move into Needs human is blocked while Needs human is full.
    No other move is blocked by a limit: a column can go over its max.
    A start from the pull list must also be its top ticket, with acceptance criteria (CHB-125, check_top_of_to_do).
    A ticket leaves Backlog only for the pull list (with `trigger`), Done or Discarded (CHB-153). The trigger
    is checked (check_trigger, UsageError) before any check that writes, such as the sort of a start.
    A failed read of the board or of the open tickets raises JiraError and blocks too.
    """
    issue = jira.request("GET", f"/rest/api/3/issue/{key}?fields={ISSUE_FIELDS}")["fields"]
    epic = (issue.get("issuetype") or {}).get("name") == "Epic"
    current_id = str(issue["status"]["id"])
    if current_id == status_id:
        check_trigger(key, Move(None, None), trigger)  # `ready` on a ticket that moved meanwhile
        return Move(None, None)
    heading = REQUIRED_SECTIONS.get(status.casefold())
    if heading and not has_section(issue.get("description"), heading):
        raise BlockedError(
            f"{key} cannot move to {status}: its description has no section headed {heading!r}. "
            f'Write it first: factory/bin/jira section {key} --heading "{heading}" --body-file FILE '
            "(docs/process/kanban.md)."
        )
    open_criteria = open_criteria_of(issue.get("description")) if status.casefold() == "done" else []
    if open_criteria:
        raise BlockedError(
            f"{key} cannot move to {status}: {len(open_criteria)} acceptance criteria are not ticked: "
            + "; ".join(f"{n}. {text}" for n, text in open_criteria)
            + f". Check each one, then: factory/bin/jira tick {key} <number> --evidence-file FILE (docs/process/kanban.md, Validating)."
        )
    columns = jira.board_columns()
    source = _column_of(columns, current_id)
    target = _column_of(columns, status_id)
    move = Move(source, target, tuple(columns), epic=epic)
    move = Move(source, target, tuple(columns), check_trigger(key, move, trigger), epic=epic)
    if target is None or target == source:
        return move
    pull = pull_list(columns)
    if source is not None and source == backlog_column(columns) and target != pull and target.name not in CLOSED:
        name = pull.name if pull else READY
        raise BlockedError(
            f"{key} is in {BACKLOG}: move it to {name} first, once its {DOR_HEADING} is ticked: "
            f'factory/bin/jira ready {key} --trigger "<which trigger and its evidence>". '
            f"From {BACKLOG} a ticket goes only to {name}, Done or Discarded (docs/process/kanban.md, Backlog)."
        )
    if target == pull and not epic:
        check_ready_maximum(jira, key, target)
    if target.name == IN_PROGRESS and (source is None or source.name not in (*STARTED_WORK, NEEDS_HUMAN)):
        limited = [column for column in columns if column.name in STARTED_WORK]
    elif target.name == NEEDS_HUMAN:
        limited = [target]
    else:
        return move
    full = []
    for column in limited:
        if column.max is None:
            continue
        others = jira.keys_in(column, excluding=key)
        if len(others) >= column.max:
            full.append(f"column {column.name!r} is at its WIP limit ({len(others)}/{column.max}): {', '.join(others)}")
    if full:
        where = source.name if source else issue["status"]["name"]
        raise WipLimitError(
            f"{key} cannot move to {status}: {'; '.join(full)}. "
            f"Leave {key} in {where}, comment on it and start nothing else. "
            f"Run this in the background (run_in_background) and carry on; it moves {key} when a slot frees, "
            f"ends at once on any other refusal, and gives up after {WAIT_LIMIT_SECONDS // 60} minutes with one line, "
            f"then tell the owner: {wait_command(key, status)} "
            "Only the owner overrides a limit, in the Jira UI, with a comment on the ticket (docs/process/kanban.md)."
        )
    if target.name == IN_PROGRESS and source is not None and source == pull:
        check_top_of_to_do(jira, key, issue, columns)
        assignee = issue.get("assignee")
        return replace(move, starts=True, assignee=str(assignee["accountId"]) if isinstance(assignee, dict) and assignee.get("accountId") else None)
    return move


WAIT_SECONDS = 120  # between two tries of the wait command (CHB-162)
WAIT_LIMIT_SECONDS = 3600  # the wait command gives up after an hour: the owner hears of a longer wait


def wait_command(key: str, status: str) -> str:
    """The shell loop that retries a move blocked by a WIP limit until a slot is free (CHB-162).

    It retries only on exit 5 (WIP limit). Any other exit ends it at once with that exit code: 0 (moved), or
    a refusal that waiting does not clear (exit 4: not the top ticket, a missing section, a blocked ticket),
    which the transition already printed. After WAIT_LIMIT_SECONDS it prints one line and exits 1.
    """
    move = f"factory/bin/jira transition {key} {shlex.quote(status)}"
    # A subshell: its `exit` ends the loop, never the shell that runs it.
    return (
        f"( end=$(( $(date +%s) + {WAIT_LIMIT_SECONDS} )); "
        f"while {move}; rc=$?; [ $rc -eq {EXIT_WIP_LIMIT} ]; do "
        "[ $(date +%s) -lt $end ] || "
        f"{{ echo \"{key}: the slot is still full after {WAIT_LIMIT_SECONDS // 60} minutes; tell the owner\"; exit 1; }}; "
        f"sleep {WAIT_SECONDS}; done; exit $rc )"
    )


def check_ready_maximum(jira: Jira, key: str, pull: Column) -> None:
    """Refuse (BlockedError) a move into the pull list while it holds its board max of other tickets (CHB-157).

    Epics are not counted (keys_in). Every way into the pull list passes here: `ready`, and a move back.
    """
    if pull.max is None:
        return
    others = jira.keys_in(pull, excluding=key)
    if len(others) >= pull.max:
        raise BlockedError(
            f"{key} cannot move to {pull.name}: column {pull.name!r} is at its maximum ({len(others)}/{pull.max}): {', '.join(others)}. "
            f"To make room, move the least needed of them back: factory/bin/jira transition CHB-<n> {BACKLOG}, "
            f"then try again (docs/process/kanban.md, Ready)."
        )


def check_top_of_to_do(jira: Jira, key: str, fields: dict[str, Any], columns: list[Column]) -> None:
    """Refuse (BlockedError) a start from the pull list of a ticket that is not its top ticket,
    or of the top ticket while it has no "Why it matters" first section or no acceptance criteria
    (docs/process/kanban.md, Ready).

    The ticket's own description and links (`fields`) come from the read just made, so a stale
    search answer cannot change its blockers or criteria. Its rank comes from the search.
    After the refusals that do not depend on the order (blocked, epic, missing from the search), it
    sorts the pull list (apply_todo_order, CHB-148) and takes the top ticket from the sorted order.
    """
    description = fields.get("description")
    pull = pull_list(columns)
    name = pull.name if pull else TO_DO
    issues = jira.open_issues()
    blocking = open_blockers(fields, {str(issue["key"]) for issue in issues})
    mine = next((issue for issue in issues if issue["key"] == key), None)
    reason = f"it is blocked by open tickets: {', '.join(blocking)}" if blocking else None
    if reason is None and mine is not None and mine["fields"]["issuetype"]["name"] == "Epic":
        reason = "it is an epic; start one of its tickets instead"
    if reason:
        raise BlockedError(
            f"{key} cannot start: {reason}. Ranking it does not help; pull the top ticket of {name} (docs/process/kanban.md, Ready)."
        )
    if mine is None:
        raise BlockedError(f"{key} is missing from the search of open tickets, so its rank is unknown. Try again in a minute.")
    # Sort only once the refusals that do not depend on the order have passed, so they write nothing.
    order, _ = apply_todo_order(jira, issues, columns, fresh={key: fields})
    queue = order.ready  # top_queue's answer, in the sorted order
    if key not in queue:  # the search still shows it outside the pull list
        raise BlockedError(f"{key} is not in {name} in the search of open tickets yet, so its rank is unknown. Try again in a minute.")
    if queue[0] != key:
        top = queue[0]
        raise BlockedError(
            f"{key} is not the top ticket of {name}: {top} is. Pull {top}. To start {key} first, with the owner's go, "
            f"raise its priority (factory/bin/jira priority {key} High) or, when both have the same priority, "
            f"rank it above {top} (factory/bin/jira rank {key} --before {top}), and try again. "
            f"Every start sorts {name} by priority, so a rank across priorities does not hold (docs/process/kanban.md, Ready)."
        )
    gaps = start_gaps(key, description)
    if gaps:
        raise BlockedError(gaps[0].message)


@dataclass(frozen=True)
class StartGap:
    """Something a ticket in the pull list lacks before a start accepts it (CHB-187)."""

    label: str  # what it needs, in a few words, for a notice
    message: str  # the refusal text of the start


def dor_gap(description: Any) -> tuple[str, list[str]] | None:
    """What the Definition of Ready of a ticket in the pull list still lacks, or None when it holds (CHB-187).

    The judgment boxes must be ticked. `ready` ticks the two verified boxes (acceptance criteria, not blocked) and
    the start checks them itself, so their ticks do not count: ticking cannot replace checking (CHB-154).
    Returns a short detail and the `tick` box names to run; the names are empty when the section is missing or doubled.
    """
    count = checklist_count(description)
    if count == 0:
        return "no section", []
    if count > 1:
        return f"{count} sections", []
    states = box_states(description) or {}
    open_boxes = [box.name for box in DOR_BOXES if box.name in JUDGMENT_BOXES and states.get(box.name) != "DONE"]
    return (f"unticked: {', '.join(open_boxes)}", open_boxes) if open_boxes else None


def start_gaps(key: str, description: Any) -> list[StartGap]:
    """Every gap in the ticket's own description that makes a start refuse it, in the order the start reports them (CHB-187).

    The start (check_top_of_to_do) and the Stop hook (check_wip_minimums) both read this, so the hook
    names only tickets the start accepts. Order and blockers are the callers' business.
    """
    gaps = []
    problem = why_problem(description)
    if problem:
        gaps.append(
            StartGap(
                WHY_HEADING,
                f"{key} cannot start: its description {problem}. Write it first: "
                f'factory/bin/jira section {key} --heading "{WHY_HEADING}" --first --body-file FILE '
                "(2 to 5 plain sentences: the pain, who feels it and the value of solving it; docs/process/kanban.md, Ready).",
            )
        )
    if not has_acceptance_criteria(description):
        gaps.append(
            StartGap(
                "acceptance criteria",
                f"{key} has no acceptance criteria. Write them first: "
                f'factory/bin/jira section {key} --heading "Acceptance criteria" --body-file FILE '
                "(derive them from the ticket, its epic and the ADRs; ask the owner only for decisions that are the owner's).",
            )
        )
    dor = dor_gap(description)
    if dor is not None:
        detail, boxes = dor
        fix = (
            f"tick the boxes you have checked, {'; '.join(f'factory/bin/jira tick {key} {name}' for name in boxes)}"
            if boxes
            else f"add or fix it: factory/bin/jira checklist {key}"
        )
        gaps.append(
            StartGap(
                f"{DOR_HEADING} ({detail})",
                f"{key} cannot start: its {DOR_HEADING} is not complete ({detail}). Fix it: {fix}. "
                f"A start needs every judgment box ticked (docs/process/kanban.md, Backlog).",
            )
        )
    return gaps


def top_queue(
    issues: list[dict[str, Any]],
    columns: Sequence[Column],
    fresh: dict[str, dict[str, Any]] | None = None,
    *,
    moved: tuple[str, Column] | None = None,
) -> list[str]:
    """Keys of the pull-list tickets that can be pulled, in rank order. `issues` is the whole of open_issues().

    The start check and the free-slot notice both take the top tickets from here, so they cannot disagree (CHB-143).
    A ticket is left out when it is an epic or an open ticket blocks it (open_blockers).
    Missing acceptance criteria do not leave it out: the agent writes them (CHB-125).
    `fresh` maps keys to issue fields (description, issuelinks) from a read newer than the search; those win.
    `moved` is a ticket that just moved and its new column. The search can still show its old status,
    so the new column wins: the ticket is open unless that column is in CLOSED, and it is never in the queue.
    """
    to_do = pull_list(columns)
    if to_do is None:
        return []
    open_keys = {str(issue["key"]) for issue in issues}
    if moved is not None:
        moved_key, target = moved
        if target.name in CLOSED:
            open_keys.discard(moved_key)
        else:
            open_keys.add(moved_key)
    queue = []
    for issue in issues:
        fields = issue["fields"]
        if str(fields["status"]["id"]) not in to_do.status_ids or fields["issuetype"]["name"] == "Epic":
            continue
        if moved is not None and issue["key"] == moved[0]:
            continue
        if not open_blockers((fresh or {}).get(issue["key"], fields), open_keys):
            queue.append(str(issue["key"]))
    return queue


def open_blockers(fields: dict[str, Any], open_keys: set[str]) -> list[str]:
    """The issues that block this one (blockers) and are still open. The one readiness rule (CHB-151).

    `fields` are the issue's fields with issuelinks. `open_keys` decides what is open: the keys of the
    open-tickets search, corrected for a ticket that just moved (top_queue). The status a link carries is
    as old as the search, so it is not used. A blocker outside CHB is never in `open_keys`.
    """
    return [key for key in blockers(fields) if key in open_keys]


def blockers(fields: dict[str, Any]) -> list[str]:
    """Keys of the issues this one "is blocked by": its "Blocks" links with the other issue as inwardIssue.

    Text in the description, such as "Depends on CHB-<n>", is not read (CHB-151).
    """
    keys = [
        str(link["inwardIssue"].get("key"))
        for link in fields.get("issuelinks") or []
        if isinstance(link, dict) and (link.get("type") or {}).get("name") == BLOCKS and isinstance(link.get("inwardIssue"), dict)
    ]
    return list(dict.fromkeys(keys))


@dataclass(frozen=True)
class TodoOrder:
    """The pull-list keys in their current rank order and in the order sort-todo wants (CHB-148)."""

    current: list[str]
    desired: list[str]
    ready: list[str]  # the top queue (top_queue), in the desired order
    column: str = TO_DO  # the name of the pull list (pull_list): "Ready", or "To Do" before the rename

    def steps(self) -> list[tuple[list[str], str | None, str | None]]:
        """Rank requests (keys, before, after) that turn the current order into the desired one.

        Nothing when the order is right. Tickets above the first misplaced one stay. When the top
        ticket changes, it goes before the old top ticket. Then the tickets from the first one still
        misplaced follow, in chunks of RANK_LIMIT, each after the last key ranked.
        """
        steps: list[tuple[list[str], str | None, str | None]] = []
        current = self.current
        if current and current[0] != self.desired[0]:
            steps.append(([self.desired[0]], current[0], None))
            current = [self.desired[0], *(key for key in current if key != self.desired[0])]
        start = next((i for i, (have, want) in enumerate(zip(current, self.desired)) if have != want), None)
        if start is None:
            return steps
        anchor = self.desired[start - 1]
        for chunk_start in range(start, len(self.desired), RANK_LIMIT):
            chunk = self.desired[chunk_start : chunk_start + RANK_LIMIT]
            steps.append((chunk, None, anchor))
            anchor = chunk[-1]
        return steps


def todo_order(
    issues: list[dict[str, Any]],
    columns: Sequence[Column],
    fresh: dict[str, dict[str, Any]] | None = None,
    *,
    moved: tuple[str, Column] | None = None,
) -> TodoOrder:
    """The pull-list order of docs/process/kanban.md, Ready (CHB-148): tickets that can start (top_queue)
    first, High, then Medium, then Low; then blocked tickets; then epics. Each group keeps its rank order.

    `issues` is the whole of open_issues(). Readiness comes from top_queue (open_blockers) only, so
    the dependency rule lives in one place. A missing or unknown priority sorts as Medium. `fresh` and
    `moved` go to top_queue.
    """
    to_do = pull_list(columns)
    if to_do is None:
        return TodoOrder([], [], [])
    in_to_do = {str(issue["key"]): issue for issue in issues if str(issue["fields"]["status"]["id"]) in to_do.status_ids}
    current = list(in_to_do)
    queue = set(top_queue(issues, columns, fresh, moved=moved))
    ready = sorted((key for key in current if key in queue), key=lambda key: _priority_order(in_to_do[key]))
    rest = [key for key in current if key not in queue]
    blocked = [key for key in rest if in_to_do[key]["fields"]["issuetype"]["name"] != "Epic"]
    epics = [key for key in rest if key not in blocked]
    return TodoOrder(current, ready + blocked + epics, ready, to_do.name)


def _priority_name(issue: dict[str, Any]) -> str | None:
    priority = issue["fields"].get("priority")
    name = priority.get("name") if isinstance(priority, dict) else None
    return None if name is None else str(name)


def _priority_order(issue: dict[str, Any]) -> int:
    return PRIORITY_ORDER.get(str(_priority_name(issue)), PRIORITY_ORDER["Medium"])


def apply_todo_order(
    jira: Jira, issues: list[dict[str, Any]], columns: Sequence[Column], fresh: dict[str, dict[str, Any]] | None = None
) -> tuple[TodoOrder, int]:
    """Rank the pull list into todo_order, with the rank requests of TodoOrder.steps. No request when sorted.

    Return the order and the number of rank requests sent. The steps re-rank every ticket from the
    first misplaced one down, not only the moved ones: simpler, at the cost of more rank history.
    """
    order = todo_order(issues, columns, fresh)
    steps = order.steps()
    for keys, before, after in steps:
        jira.rank(keys, before=before, after=after)
    return order, len(steps)


CRITERIA_HEADING = "acceptance criteria"  # a section heading that starts with it holds criteria, with or without "(Validating)"
# A top-level bullet of a Validating section that labels part of a criterion; it stays a plain line (CHB-185).
CRITERION_LABEL = re.compile(r"^- (?:\*\*)?(?:when|speed[ -]up|where|pass|who|if it fails)\b", re.IGNORECASE)


def acceptance_checkboxes(heading: str, body: str) -> str:
    """The body of an "Acceptance criteria" section with each top-level bullet as an unticked checkbox (CHB-185).

    Other headings, numbered lists, indented lines, ticked boxes and the label lines of the Validating section
    (When, Speed up, Where, Pass, Who, If it fails) stay as they are.
    """
    if not heading.strip().casefold().startswith(CRITERIA_HEADING):
        return body
    return "\n".join(
        f"- [ ] {line[2:]}" if BULLET_PATTERN.match(line) and not TASK_PATTERN.match(line) and not CRITERION_LABEL.match(line) else line
        for line in body.split("\n")
    )


def acceptance_checkboxes_in(markdown: str) -> str:
    """`acceptance_checkboxes` for every section of a whole description (`create`, CHB-185)."""
    heading, out = "", []
    for line in markdown.split("\n"):
        found = HEADING_PATTERN.fullmatch(line)
        if found:
            heading = found.group(2)
        out.append(acceptance_checkboxes(heading, line))
    return "\n".join(out)


def _criteria_items(description: Any) -> list[dict[str, Any]]:
    """The task items of every top-level "Acceptance criteria" section, in order (the nodes themselves)."""
    content = _content(description)
    items: list[dict[str, Any]] = []
    for start, node in enumerate(content):
        if not (_is_heading(node) and _heading_text(node).casefold().startswith(CRITERIA_HEADING)):
            continue
        for inner in content[start + 1 : _section_end(content, start)]:
            if isinstance(inner, dict) and inner.get("type") == "taskList":
                items.extend(i for i in inner.get("content", []) if isinstance(i, dict) and i.get("type") == "taskItem")
    return items


def _item_text(item: dict[str, Any]) -> str:
    return "".join(adf_to_text(child) for child in item.get("content", [])).strip()


def open_criteria_of(description: Any) -> list[tuple[int, str]]:
    """(number, text) of each unticked criterion checkbox. A ticket whose criteria are plain text has none (CHB-185)."""
    return [(n, _item_text(i)) for n, i in enumerate(_criteria_items(description), 1) if (i.get("attrs") or {}).get("state") != "DONE"]


def find_criterion(description: Any, target: str) -> tuple[int, str, bool]:
    """(number, text, already ticked) of the criterion `target` names: its number, or a text that only one
    criterion contains (case ignored). Raises JiraError when none or several match (CHB-185)."""
    items = _criteria_items(description)
    if not items:
        raise JiraError('the description has no acceptance criteria checkboxes; write them: factory/bin/jira section CHB-<n> --heading "Acceptance criteria" --body-file FILE')
    wanted = target.strip()
    if wanted.isdigit():
        hits = [n for n in range(1, len(items) + 1) if n == int(wanted)]
    else:
        hits = [n for n, i in enumerate(items, 1) if wanted.casefold() in _item_text(i).casefold()]
    if len(hits) != 1:
        listing = "; ".join(f"{n}. {_item_text(i)}" for n, i in enumerate(items, 1))
        why = "matches no criterion" if not hits else f"matches {len(hits)} criteria"
        raise JiraError(f"{target!r} {why}. Criteria: {listing}")
    item = items[hits[0] - 1]
    return hits[0], _item_text(item), (item.get("attrs") or {}).get("state") == "DONE"


def tick_criterion(description: dict[str, Any], number: int) -> dict[str, Any]:
    """A copy of the description with criterion `number` ticked. Only its state changes."""
    result = copy.deepcopy(description)
    item = _criteria_items(result)[number - 1]
    item["attrs"] = {**(item.get("attrs") or {}), "state": "DONE"}
    return result


def has_acceptance_criteria(description: Any) -> bool:
    """True when an "Acceptance criteria" heading or label line has a non-blank line under it.

    ACCEPTANCE_PATTERN defines the heading and the label line, in any case. Its section ends at the next
    heading (of the same or a higher level, when the line is a heading itself).
    """
    lines = adf_to_text(description).splitlines()
    for start, line in enumerate(lines):
        if not ACCEPTANCE_PATTERN.match(line):
            continue
        heading = HEADING_PATTERN.fullmatch(line)
        level = len(heading.group(1)) if heading else 6
        for following in lines[start + 1 :]:
            other = HEADING_PATTERN.fullmatch(following)
            if other and len(other.group(1)) <= level:
                break
            if following.strip() and not other:
                return True
    return False


def after_transition(jira: Jira, key: str, move: Move) -> list[str]:
    """Notices after a move: a column left below its minimum, then the free In Progress slots."""
    return _minimum_notice(jira, key, move) + _refill_notice(jira, key, move) + _free_slot_notice(jira, key, move)


def _minimum_notice(jira: Jira, key: str, move: Move) -> list[str]:
    """After a move between columns, say when the column it left is below its board minimum (not Ready: _refill_notice).

    The minimum is a signal, never a block: a failed count becomes a notice, not an error. Epics are not
    counted (keys_in). The moved key is left out of the count because Jira search can still list it in its
    old status for a moment.
    """
    source, target = move.source, move.target
    if source == target:
        return []
    checks = [(source, source.min, 0)] if source is not None and source.min is not None and source != pull_list(move.columns) else []
    notices = []
    for column, minimum, added in checks:
        try:
            count = len(jira.keys_in(column, excluding=key)) + added
        except JiraError as error:  # the move is done: report, do not fail it
            notices.append(f"could not check the {column.name} minimum: {error}")
            continue
        if count < minimum:
            notices.append(f"{column.name} is below its minimum ({count}/{minimum})")
    return notices


def _refill_notice(jira: Jira, key: str, move: Move) -> list[str]:
    """After a move between columns, the refill notice while Ready is below its board minimum (CHB-164).

    It follows every such move, not only a move out of Ready: the minimum is a quota. The first line is a
    RefillNotice, which makes the command exit 6. Epics are not counted (keys_in). The moved key is left out of
    the count because Jira search can still list it in its old status; in Ready it entered it is counted by hand.
    A failed read becomes a notice, not an error: the move is done.
    """
    pull = pull_list(move.columns)
    if pull is None or pull.min is None or move.source == move.target:
        return []
    try:
        count = len(jira.keys_in(pull, excluding=key)) + (1 if move.target == pull and not move.epic else 0)
        if count >= pull.min:
            return []
        return refill_lines(pull, count, jira.open_issues(), move.columns, exclude=key)
    except JiraError as error:
        return [f"could not check the {pull.name} minimum: {error}"]


def _free_slot_notice(jira: Jira, key: str, move: Move) -> list[str]:
    """`In Progress 2/3: 1 slot free; next: CHB-123` after a move between columns (CHB-125).

    Nothing after a move within one column or to a status on no column, when In Progress has no
    max, or when In Progress, In Review or Validating is at its max (starting is blocked then).
    Search can still list the moved key in its old status, so it is left out of the counts and
    counted in its new column by hand. top_queue gets the whole search and the move: the moved
    key is never "next", and it blocks its dependents unless it moved to Done or Discarded (CHB-143).
    A failed search becomes a notice: the move is done.
    """
    if move.target is None or move.target == move.source:
        return []
    columns = {column.name: column for column in move.columns}
    in_progress = columns.get(IN_PROGRESS)
    if in_progress is None or in_progress.max is None:
        return []
    try:
        issues = jira.open_issues()
    except JiraError as error:  # the move is done: report, do not fail it
        return [f"could not check free {IN_PROGRESS} slots: {error}"]
    others = [issue for issue in issues if issue["key"] != key and issue["fields"]["issuetype"]["name"] != "Epic"]
    counts: dict[str, int] = {}
    for name in STARTED_WORK:
        column = columns.get(name)
        if column is None:
            continue
        here = sum(1 for issue in others if str(issue["fields"]["status"]["id"]) in column.status_ids)
        counts[name] = here + (1 if move.target == column and not move.epic else 0)
        if column.max is not None and counts[name] >= column.max:
            return []
    free = in_progress.max - counts[IN_PROGRESS]
    descriptions = {issue["key"]: issue["fields"].get("description") for issue in issues}
    queue = [_with_missing_sections(top, descriptions[top]) for top in todo_order(issues, move.columns, moved=(key, move.target)).ready[:free]]
    pull = pull_list(move.columns)
    following = ", ".join(queue) or f"nothing to start in {pull.name if pull else TO_DO}"
    return [f"{IN_PROGRESS} {counts[IN_PROGRESS]}/{in_progress.max}: {free} slot{'s' if free > 1 else ''} free; next: {following}"]


def _with_missing_sections(key: str, description: Any) -> str:
    """`CHB-7`, or `CHB-7 (needs Why it matters and acceptance criteria)` for what a start would refuse (start_gaps)."""
    missing = [gap.label for gap in start_gaps(key, description)]
    if not missing:
        return key
    return f"{key} (needs {', '.join(missing[:-1])} and {missing[-1]})" if len(missing) > 1 else f"{key} (needs {missing[0]})"


def startable(issues: list[dict[str, Any]], columns: Sequence[Column]) -> tuple[str | None, list[tuple[str, list[StartGap]]]]:
    """The ticket a start would accept now, and the pull-list tickets it would refuse for a gap (CHB-187).

    A start takes only the top ticket (todo_order(...).ready, the order check_top_of_to_do uses), so that is the
    only candidate. It is None when the top ticket has a gap (start_gaps). The second item lists every ticket
    of the queue with its gaps, in queue order: what the agent must write or tick, starting with the top ticket.
    `issues` is the whole of open_issues().
    """
    by_key = {str(issue["key"]): issue for issue in issues}
    queue = todo_order(issues, columns).ready
    refused = [(key, gaps) for key in queue if (gaps := start_gaps(key, by_key[key]["fields"].get("description")))]
    top = queue[0] if queue and not any(key == queue[0] for key, _ in refused) else None
    return top, refused


def has_section(description: Any, heading: str) -> bool:
    """True when the description has a top-level heading with exactly this text and text under it."""
    content = description.get("content", []) if isinstance(description, dict) else []
    for start, node in enumerate(content):
        if not (_is_heading(node) and _heading_text(node) == heading):
            continue
        level = _level(node)
        for following in content[start + 1 :]:
            if _is_heading(following) and _level(following) <= level:
                break
            if adf_to_text(following).strip():
                return True
    return False


def _column_of(columns: list[Column], status_id: str) -> Column | None:
    return next((column for column in columns if status_id in column.status_ids), None)


def _limit(value: Any) -> int | None:
    """A board min or max. Jira leaves the field out when the limit is unset."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _jira_messages(error: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""
    messages = list(data.get("errorMessages", [])) + [f"{k}: {v}" for k, v in data.get("errors", {}).items()]
    return "; ".join(str(m) for m in messages)


def _name(user: dict[str, Any] | None) -> str:
    return user["displayName"] if user else "Unassigned"


def _print_issue(issue: dict[str, Any]) -> None:
    fields = issue["fields"]
    parent = fields.get("parent")
    print(f"{issue['key']} [{fields['issuetype']['name']}] {fields['summary']}")
    print(f"Status: {fields['status']['name']}")
    if fields.get("priority"):
        print(f"Priority: {fields['priority']['name']}")
    print(f"Assignee: {_name(fields.get('assignee'))}")
    if parent:
        print(f"Parent: {parent['key']}")
    print()
    for line in _link_lines(fields.get("issuelinks") or []):
        print(line)
    print()
    print(adf_to_text(fields.get("description")) or "(no description)")
    for item in fields.get("comment", {}).get("comments", []):
        print(f"\n--- {_name(item.get('author'))}, {item.get('created', '')}")
        print(adf_to_text(item.get("body")))


def _link_lines(links: list[dict[str, Any]]) -> list[str]:
    """`Is blocked by: CHB-146 (To Do)` for each link, labelled as Jira labels it on this issue."""
    lines = []
    for link in links:
        kind = link.get("type") or {}
        side = "inward" if "inwardIssue" in link else "outward"
        other = link.get(f"{side}Issue") or {}
        status = ((other.get("fields") or {}).get("status") or {}).get("name", "?")
        label = str(kind.get(side) or kind.get("name") or "linked to")
        lines.append(f"{label[:1].upper()}{label[1:]}: {other.get('key', '?')} ({status})")
    return lines


def _print_move(key: str, target: str, notices: list[str]) -> bool:
    """Print the move and its notices. True when a notice asks for a Ready refill (exit 6, CHB-164)."""
    print(f"{key} -> {target}")
    for notice in notices:
        print(notice)
    return any(isinstance(notice, RefillNotice) for notice in notices)


def _checklist(jira: Jira, keys: list[str]) -> None:
    """Add the Definition of Ready to each ticket and print one line per key, then a count (CHB-154).

    A ticket that fails is reported and skipped; the others still get the checklist. Any failure raises
    JiraError (exit 1) at the end.
    """
    results: dict[str, int] = {"added": 0, "already there": 0, "skipped (epic)": 0}
    failed = []
    for key in keys:
        try:
            result = jira.checklist(key)
        except JiraError as error:
            failed.append(key)
            print(f"{key}: {error}")
            continue
        results[result] += 1
        print(f"{key}: {result}")
    epics = results["skipped (epic)"]
    summary = f"{len(keys)} tickets: {results['added']} added, {results['already there']} already there, {epics} epic{'' if epics == 1 else 's'} skipped"
    print(summary + (f", {len(failed)} failed" if failed else ""))
    if failed:
        raise JiraError(f"checklist failed for {', '.join(failed)}")


def _read_text(path: str) -> str:
    """The file, or stdin for "-", as UTF-8 text. An unreadable or non-UTF-8 file is a UsageError."""
    name = "stdin" if path == "-" else path
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError as error:
        raise UsageError(f"{name} is not valid UTF-8 (byte {error.start})") from None
    except OSError as error:
        raise UsageError(f"cannot read {name}: {error.strerror}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory/bin/jira", description="Jira as the agent account.")
    commands = parser.add_subparsers(dest="command", required=True)

    view = commands.add_parser("view", help="show an issue with its comments")
    view.add_argument("key")
    view.add_argument("--json", action="store_true")

    search = commands.add_parser("search", help="list issues matching a JQL query")
    search.add_argument("jql")
    search.add_argument("--limit", type=int, default=500)
    search.add_argument("--json", action="store_true")

    create = commands.add_parser("create", help="create a Task under a CHB epic")
    create.add_argument("--parent", required=True)
    create.add_argument("--summary", required=True)
    create.add_argument("--description-file", required=True)
    create.add_argument("--priority", help="High, Medium or Low; required unless --expedite")
    create.add_argument("--expedite", metavar="REASON", help=f"a production incident: '<case>: <evidence>', case one of: {', '.join(EXPEDITE_CASES)}")
    # Refused (CHB-154): kept so the message names the ready command instead of argparse's.
    create.add_argument("--ready", action="store_true", help=argparse.SUPPRESS)
    create.add_argument("--trigger", help=argparse.SUPPRESS)

    transition = commands.add_parser("transition", help="move an issue to a status, by status name")
    transition.add_argument("key")
    transition.add_argument("status")
    transition.add_argument("--trigger", help=argparse.SUPPRESS)  # refused: `ready` moves a ticket to Ready (CHB-154)

    ready = commands.add_parser("ready", help="move a Backlog ticket to Ready once its Definition of Ready holds")
    ready.add_argument("key")
    ready.add_argument("--trigger", required=True, help="which trigger makes it needed now, and its evidence")

    tick = commands.add_parser("tick", help="tick one judgment box of the Definition of Ready")
    tick.add_argument("key")
    tick.add_argument(
        "box",
        help=f"a Definition of Ready box ({', '.join(JUDGMENT_BOXES)}); with --evidence-file an acceptance criterion: its number, or a text only it contains",
    )
    tick.add_argument("--evidence-file", help='criterion only: what you checked and what it showed, posted as a comment (FILE "-" reads stdin)')

    checklist = commands.add_parser("checklist", help="add the Definition of Ready to tickets that lack it")
    checklist.add_argument("keys", nargs="*", metavar="key")
    checklist.add_argument("--open", action="store_true", help="every open ticket that is not an epic")

    comment = commands.add_parser("comment", help="add a plain-text comment")
    comment.add_argument("key")
    comment.add_argument("--body-file", required=True)

    assign = commands.add_parser("assign", help="assign an issue")
    assign.add_argument("key")
    who = assign.add_mutually_exclusive_group(required=True)
    who.add_argument("--me", action="store_true")
    who.add_argument("--account-id")

    rank = commands.add_parser("rank", help="move issues, in order, just before or after another issue")
    rank.add_argument("keys", nargs="+", metavar="key")
    where = rank.add_mutually_exclusive_group(required=True)
    where.add_argument("--before")
    where.add_argument("--after")

    section = commands.add_parser("section", help="set one headed section of the description")
    section.add_argument("key")
    section.add_argument("--heading", required=True)
    section.add_argument("--body-file", required=True)
    section.add_argument("--first", action="store_true", help="put the section at the top (always for 'Why it matters')")

    commands.add_parser("sort-todo", help="rank Ready (To Do before the rename): ready High, Medium, Low, then blocked, then epics")

    edit_comment = commands.add_parser("edit-comment", help="replace the body of one of the agent's comments")
    edit_comment.add_argument("key")
    edit_comment.add_argument("comment_id")
    edit_comment.add_argument("--body-file", required=True)

    description = commands.add_parser("description", help="replace the whole description with a Markdown file")
    description.add_argument("key")
    description.add_argument("--file", required=True)

    link = commands.add_parser("link", help='make another ticket block this one (a "Blocks" link)')
    link.add_argument("key")
    link.add_argument("--blocked-by", required=True)

    priority = commands.add_parser("priority", help="set the priority: High, Medium or Low")
    priority.add_argument("key")
    priority.add_argument("priority", help="High, Medium or Low")
    return parser


def run(args: argparse.Namespace, jira: Jira) -> int:
    """Run the command. main() reads the input files into args first (body, description, text).

    Return EXIT_REFILL when a move is done and left Ready below its minimum (CHB-164), else 0.
    """
    if args.command == "view":
        issue = jira.view(args.key)
        if args.json:
            print(json.dumps(issue, indent=2))
        else:
            _print_issue(issue)
    elif args.command == "search":
        issues = jira.search(args.jql, args.limit)
        if args.json:
            print(json.dumps(issues, indent=2))
            return 0
        for issue in issues:
            fields = issue["fields"]
            print(f"{issue['key']}\t{fields['status']['name']}\t{_name(fields.get('assignee'))}\t{fields['summary']}")
    elif args.command == "create" and args.expedite is not None:
        key = jira.create_expedite(args.parent, args.summary, args.description, args.expedite)
        print(key)
        # The ticket exists: from here on a failure is a notice, never an exit code that invites a second create.
        # KeyError, TypeError and AttributeError mean an answer of unexpected shape, as in main(): still exit 0.
        try:
            jira.move_expedite(key)
        except (JiraError, KeyError, TypeError, AttributeError) as error:
            print(
                f"could not start {key} as expedite: {error}. Tell the owner now; the owner moves it to {IN_PROGRESS} "
                f"in the Jira UI, because transition refuses a start from {BACKLOG}"
            )
            return 0
        print(f"{key} -> {IN_PROGRESS} (expedite). Tell the owner now: {args.expedite.strip()}")
        for notice in jira.finish_expedite(key, args.expedite):
            print(notice)
    elif args.command == "create":
        key = jira.create(args.parent, args.summary, args.description, args.priority)
        print(key)
        # The ticket exists: from here on a failure is a notice, never an exit code that invites a second create.
        try:
            column = jira.place_created(key)
        except (JiraError, KeyError, TypeError, AttributeError) as error:
            print(f'could not place {key}: {error}. Move it: factory/bin/jira transition {key} "{BACKLOG}"')
            return 0
        if column is not None and column.name == BACKLOG:
            print(f"{key} in {BACKLOG}")
            return 0
        # The ticket exists: a failed sort is a notice, never an exit code that invites a second create.
        # KeyError, TypeError and AttributeError mean an answer of unexpected shape, as in main().
        try:
            order, sent = jira.sort_todo((key, args.description, args.priority))
        except (JiraError, KeyError, TypeError, AttributeError) as error:
            print(f"could not sort {column.name if column else TO_DO}: {error}. Run factory/bin/jira sort-todo")
        else:
            if sent:
                print(f"{order.column} sorted: {', '.join(order.desired)}")
    elif args.command == "sort-todo":
        order, sent = jira.sort_todo()
        state = f"sorted ({sent} rank requests)" if sent else "already sorted"
        print(f"{order.column} {state}: {', '.join(order.desired) or 'empty'}")
    elif args.command == "transition":
        return EXIT_REFILL if _print_move(args.key, *jira.transition(args.key, args.status)) else 0
    elif args.command == "ready":
        return EXIT_REFILL if _print_move(args.key, *jira.ready(args.key, args.trigger)) else 0
    elif args.command == "tick" and args.evidence_file is not None:
        number, text, changed = jira.tick_criterion(args.key, args.box, args.evidence)
        print(f"{args.key} criterion {number} {'ticked' if changed else 'unchanged'}: {text}")
    elif args.command == "tick":
        changed = jira.tick(args.key, args.box)
        print(f"{args.key} box {args.box!r} {'ticked' if changed else 'unchanged'}")
    elif args.command == "checklist":
        _checklist(jira, args.keys or [str(issue["key"]) for issue in jira.open_issues()])
    elif args.command == "comment":
        print(f"{args.key} comment {jira.comment(args.key, args.body)}")
    elif args.command == "assign":
        account_id = jira.myself()["accountId"] if args.me else args.account_id
        jira.assign(args.key, account_id)
        print(f"{args.key} assigned to {account_id}")
    elif args.command == "rank":
        jira.rank(args.keys, before=args.before, after=args.after)
        print(f"{' '.join(args.keys)} ranked {'before ' + args.before if args.before else 'after ' + args.after}")
    elif args.command == "section":
        changed = jira.set_section(args.key, args.heading, args.body, first=args.first)
        print(f"{args.key} section {args.heading!r} {'set' if changed else 'unchanged'}")
    elif args.command == "edit-comment":
        jira.edit_comment(args.key, args.comment_id, args.text)
        print(f"{args.key} comment {args.comment_id} updated")
    elif args.command == "description":
        jira.set_description(args.key, args.text)
        print(f"{args.key} description set")
    elif args.command == "link":
        created = jira.link(args.key, args.blocked_by)
        print(f"{args.key} is blocked by {args.blocked_by}: link {'created' if created else 'unchanged'}")
    elif args.command == "priority":
        jira.set_priority(args.key, args.priority)
        print(f"{args.key} priority {args.priority}")
    return 0


def main(
    argv: list[str],
    credentials_reader: Callable[[], tuple[str, str]] = read_credentials,
    opener_factory: Callable[[], Opener] = default_opener,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else 0
    try:
        if args.command == "link":
            check_link_input(args.key, args.blocked_by)
        keys = [getattr(args, name, None) for name in ("key", "parent", "before", "after", "blocked_by")] + getattr(args, "keys", [])
        for key in keys:
            if key is not None:
                check_key(key)  # before 1Password and before reading any file
        if args.command == "rank":
            check_rank_input(args.keys, args.before or args.after)
        if args.command == "search":
            scope_jql(args.jql)
        if getattr(args, "trigger", None) is not None and not args.trigger.strip():
            raise UsageError("trigger is empty: name the trigger and its evidence")
        if args.command == "create" and (args.ready or args.trigger is not None):
            raise UsageError(
                f"create puts a ticket in {BACKLOG}. To move it to {READY}, tick its {DOR_HEADING} "
                '(factory/bin/jira tick CHB-<n> <box>), then run factory/bin/jira ready CHB-<n> --trigger "<which trigger and its evidence>"'
            )
        if args.command == "transition" and args.trigger is not None:
            raise UsageError(
                f'transition takes no --trigger: factory/bin/jira ready {args.key} --trigger "<which trigger and its evidence>" '
                f"moves a ticket from {BACKLOG} to {READY} once its {DOR_HEADING} holds"
            )
        if args.command == "tick" and args.evidence_file is None:
            if args.box not in DOR_BOX_NAMES:
                raise UsageError(f"{args.box!r} is not a box of the {DOR_HEADING}; to tick an acceptance criterion add --evidence-file FILE")
            check_box(args.box)
        elif args.command == "tick":
            args.evidence = _read_text(args.evidence_file)
            if not args.evidence.strip():
                raise UsageError("the evidence is empty: say what you checked and what it showed")
        if args.command == "checklist" and bool(args.keys) == args.open:
            raise UsageError("checklist takes issue keys or --open, not both")
        if args.command == "create" and args.expedite is not None:
            if args.priority is not None:
                raise UsageError(f"--expedite sets the priority {EXPEDITE_PRIORITY}; leave out --priority")
            check_expedite(args.expedite)
        elif args.command == "create":
            if args.priority is None:
                raise UsageError("create needs --priority High|Medium|Low, or --expedite for a production incident")
            check_priority(args.priority)
        if args.command == "priority":
            check_priority(args.priority)
        if args.command == "create":
            check_summary(args.summary)
            args.description = _read_text(args.description_file)
            check_why_text(args.description)
        if args.command == "comment":
            args.body = _read_text(args.body_file)
            check_comment_body(args.body)
        if args.command == "section":
            args.body = _read_text(args.body_file)
            check_section_input(args.heading, args.body)
            check_speed_up(args.heading, args.body)
            check_answer_line(args.heading, args.body)
        if args.command == "edit-comment":
            check_comment_id(args.comment_id)
            args.text = _read_text(args.body_file)
            check_comment_body(args.text)
        if args.command == "description":
            args.text = _read_text(args.file)
            if not args.text.strip():
                raise UsageError("description is empty")
        jira = Jira(*credentials_reader(), opener_factory())
        code = run(args, jira)
    except CredentialError as error:
        print(f"jira: no Jira credential: {error}. STOP: do not use another Jira login. Report to the owner.", file=sys.stderr)
        return EXIT_CREDENTIAL
    except BlockedError as error:
        print(f"jira: blocked: {error}", file=sys.stderr)
        return EXIT_WIP_LIMIT if isinstance(error, WipLimitError) else EXIT_BLOCKED
    except JiraError as error:
        print(f"jira: {error}", file=sys.stderr)
        return EXIT_JIRA
    except (KeyError, TypeError, AttributeError) as error:
        print(f"jira: unexpected response from Jira ({type(error).__name__}: {error})", file=sys.stderr)
        return EXIT_JIRA
    except UsageError as error:
        print(f"jira: {error}", file=sys.stderr)
        return EXIT_USAGE
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
