#!/usr/bin/env python3
"""Act in Jira as the agent account "Claude (CHB agent)" (CHB-107).

Usage:
  factory/bin/jira view CHB-12 [--json]
  factory/bin/jira search 'project = CHB AND status = "In Review"' [--json]
  factory/bin/jira create --parent CHB-27 --summary TEXT --description-file FILE
  factory/bin/jira transition CHB-12 "In Review"
  factory/bin/jira comment CHB-12 --body-file FILE      (FILE "-" reads stdin)
  factory/bin/jira assign CHB-12 --me | --account-id ID
  factory/bin/jira rank CHB-12 [CHB-13 ...] --before CHB-9 | --after CHB-9
  factory/bin/jira section CHB-12 --heading TEXT --body-file FILE

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

Exit codes:
  0  done
  1  Jira refused or failed (HTTP error, unknown status, parent is not an epic,
     an issue that could not be ranked)
  2  usage error (also a bad section heading, an empty body, a duplicate rank key)
  3  no credential: no service account token in the keyring (keyring locked, item absent)
     or `op` failed. The agent stops and reports.
  4  a board rule blocks the transition: the target column is at its WIP limit, or the
     description lacks the section the target status requires (docs/process/kanban.md).
     The ticket stays where it is.

`transition` reads the column limits from the board configuration (board 2) on every call.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
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
SEARCH_FIELDS = ["summary", "status", "assignee", "issuetype", "parent"]
RANK_LIMIT = 50  # issues per rank request, the Jira Software limit
BOARD_ID = 2  # the CHB board; its column configuration holds the WIP limits
IN_PROGRESS = "In Progress"
# Target status (casefolded) -> description section it requires (docs/process/kanban.md).
REQUIRED_SECTIONS = {
    "validating": "Acceptance criteria (Validating)",
    "needs human": "Needs human: owner steps",
}

EXIT_JIRA = 1
EXIT_USAGE = 2
EXIT_CREDENTIAL = 3
EXIT_BLOCKED = 4


class JiraError(Exception):
    """Jira refused the request or the request makes no sense. Exit code 1."""


class UsageError(Exception):
    """A local input problem, such as an unreadable file. Exit code 2."""


class CredentialError(Exception):
    """The token could not be read. Exit code 3: the agent stops."""


class BlockedError(Exception):
    """A board rule blocks the transition: a full column or a missing section. Exit code 4."""


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


def read_secret(
    command: list[str],
    timeout: int = 60,
    token_reader: Callable[[], str] = op_service_account.read_token,
    run: Callable[..., Any] = subprocess.run,
) -> str:
    """Output of `op` run with the service account token in its environment only."""
    try:
        env = op_service_account.op_env(os.environ, token_reader())
    except op_service_account.ServiceAccountError as error:
        raise CredentialError(str(error)) from None
    try:
        result = run(  # noqa: S603 - fixed command
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CredentialError(f"could not run {command[0]}: {type(error).__name__}") from None
    if result.returncode != 0:
        raise CredentialError(f"{command[0]} exited {result.returncode} (service account token expired or revoked?)")
    return result.stdout.decode()


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


def text_to_adf(text: str, *, bullets: bool = False) -> dict[str, Any]:
    """Plain text to an Atlassian Document. Blank lines split paragraphs, URLs become links.

    With bullets, consecutive lines that start with "- " become a bullet list.
    """
    blocks: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.split("\n")
        if not bullets:
            blocks.append(_paragraph(lines))
            continue
        run: list[str] = []
        items: list[str] = []
        for line in lines:
            if line.startswith("- "):
                if run:
                    blocks.append(_paragraph(run))
                    run = []
                items.append(line[2:])
            else:
                if items:
                    blocks.append(_bullet_list(items))
                    items = []
                run.append(line)
        if run:
            blocks.append(_paragraph(run))
        if items:
            blocks.append(_bullet_list(items))
    return {"type": "doc", "version": 1, "content": blocks}


def _paragraph(lines: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "paragraph", "content": content}


def _bullet_list(items: list[str]) -> dict[str, Any]:
    return {"type": "bulletList", "content": [{"type": "listItem", "content": [_paragraph([item])]} for item in items]}


def _inline(line: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in URL_PATTERN.finditer(line):
        if match.start() > position:
            nodes.append({"type": "text", "text": line[position : match.start()]})
        url = match.group(0)
        nodes.append({"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]})
        position = match.end()
    if position < len(line):
        nodes.append({"type": "text", "text": line[position:]})
    return nodes


def adf_to_text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return str(node.get("text", ""))
    if kind == "hardBreak":
        return "\n"
    if kind == "bulletList":
        items = (adf_to_text(item).strip("\n") for item in node.get("content", []))
        return "".join(f"- {item}\n" for item in items) + "\n"
    inner = "".join(adf_to_text(child) for child in node.get("content", []))
    if kind == "doc":
        return inner.strip("\n")
    if kind in {"paragraph", "heading", "codeBlock", "listItem", "blockquote", "rule"}:
        return inner.rstrip("\n") + "\n\n"
    return inner


def check_key(key: str) -> str:
    if not KEY_PATTERN.fullmatch(key):
        raise JiraError(f"{key!r} is not a {PROJECT} issue key ({PROJECT}-<n>)")
    return key


def replace_section(description: dict[str, Any] | None, heading: str, body: str) -> dict[str, Any]:
    """Return a copy of the description with the section under `heading` set to `body`.

    The section starts at the top-level heading whose text is `heading` and ends before the next
    heading of the same or a higher level, or at the end. It is replaced by a level-2 heading and
    the body. Without such a heading the section is appended. Other nodes are kept as they are.
    """
    heading = check_section_input(heading, body)
    doc = description if isinstance(description, dict) and description.get("type") == "doc" else None
    content: list[Any] = list(doc.get("content", [])) if doc else []
    new = [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": heading}]},
        *text_to_adf(body, bullets=True)["content"],
    ]
    starts = [i for i, node in enumerate(content) if _is_heading(node) and adf_to_text(node).strip() == heading]
    if len(starts) > 1:
        raise JiraError(f"the description has {len(starts)} sections headed {heading!r}; fix it by hand")
    if not starts:
        content.extend(new)
    else:
        start = starts[0]
        level = _level(content[start])
        end = next(
            (i for i in range(start + 1, len(content)) if _is_heading(content[i]) and _level(content[i]) <= level),
            len(content),
        )
        content[start:end] = new
    result = dict(doc) if doc else {"type": "doc", "version": 1}
    result["content"] = content
    return result


def check_section_input(heading: str, body: str) -> str:
    """Refuse a bad heading or an empty body (UsageError, exit 2). Return the stripped heading."""
    heading = heading.strip()
    if not heading or "\n" in heading:
        raise UsageError("heading must be one non-empty line")
    if not body.strip():
        raise UsageError("section body is empty")
    return heading


def check_rank_input(keys: list[str], anchor: str) -> None:
    """Refuse a rank request that makes no sense (UsageError, exit 2). Keys are checked apart."""
    if not keys or len(keys) > RANK_LIMIT:
        raise UsageError(f"rank takes 1 to {RANK_LIMIT} issues, got {len(keys)}")
    if len(set(keys)) != len(keys) or anchor in keys:
        raise UsageError("each issue may appear once, and not as its own anchor")


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
        fields = "summary,status,assignee,issuetype,parent,description,comment"
        return self.request("GET", f"/rest/api/3/issue/{check_key(key)}?fields={fields}")

    def search(self, jql: str, limit: int = 500) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page_token = None
        while len(issues) < limit:
            body: dict[str, Any] = {"jql": jql, "fields": SEARCH_FIELDS, "maxResults": min(100, limit - len(issues))}
            if page_token:
                body["nextPageToken"] = page_token
            page = self.request("POST", "/rest/api/3/search/jql", body)
            issues.extend(page.get("issues", []))
            page_token = page.get("nextPageToken")
            if page.get("isLast", True) or not page_token:
                break
        return issues

    def create(self, parent: str, summary: str, description: str) -> str:
        if not summary.strip():
            raise JiraError("summary is empty")
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
                    "description": text_to_adf(description),
                }
            },
        )
        return str(created["key"])

    def transition(self, key: str, status: str) -> tuple[str, list[str]]:
        """Move the issue. Return the status name and notices to print (a column below its minimum)."""
        check_key(key)
        transitions = self.request("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
        matches = [t for t in transitions if t["to"]["name"].casefold() == status.casefold()]
        if not matches:
            names = sorted({t["to"]["name"] for t in transitions})
            raise JiraError(f"{key} cannot move to {status!r}. Allowed: {', '.join(names) or 'none'}")
        target = matches[0]["to"]["name"]
        move = before_transition(self, key, target, status_id=str(matches[0]["to"]["id"]))
        self.request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": matches[0]["id"]}}, empty_ok=True)
        return target, after_transition(self, key, move)

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

    def keys_in(self, column: Column, *, excluding: str) -> list[str]:
        """Keys of the CHB issues in the column's statuses, without `excluding`."""
        if not column.status_ids:
            return []
        ids = ", ".join(sorted(column.status_ids))
        jql = f"project = {PROJECT} AND status in ({ids}) AND key != {check_key(excluding)} ORDER BY key ASC"
        return [str(issue["key"]) for issue in self.search(jql, limit=100)]

    def comment(self, key: str, body: str) -> str:
        if not body.strip():
            raise JiraError("comment body is empty")
        created = self.request("POST", f"/rest/api/3/issue/{check_key(key)}/comment", {"body": text_to_adf(body)})
        return str(created["id"])

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

    def set_section(self, key: str, heading: str, body: str) -> bool:
        """Set one section of the description. Return False when it already had that content."""
        check_key(key)
        current = self.request("GET", f"/rest/api/3/issue/{key}?fields=description")["fields"].get("description")
        updated = replace_section(current, heading, body)
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


def before_transition(jira: Jira, key: str, status: str, *, status_id: str) -> Move:
    """Called with the target status just before every transition POST. Raise to block the move.

    Raises BlockedError (exit 4) when the target status needs a description section that is
    missing, or when the target column holds at least its board maximum of other tickets.
    A move within one column, out of a column, or into a column without a maximum is never
    blocked by the limit. A failed read of the board raises JiraError and blocks too.
    """
    issue = jira.request("GET", f"/rest/api/3/issue/{key}?fields=status,description")["fields"]
    current_id = str(issue["status"]["id"])
    if current_id == status_id:
        return Move(None, None)
    heading = REQUIRED_SECTIONS.get(status.casefold())
    if heading and not has_section(issue.get("description"), heading):
        raise BlockedError(
            f"{key} cannot move to {status}: its description has no section headed {heading!r}. "
            f'Write it first: factory/bin/jira section {key} --heading "{heading}" --body-file FILE '
            "(docs/process/kanban.md)."
        )
    columns = jira.board_columns()
    source = _column_of(columns, current_id)
    target = _column_of(columns, status_id)
    if target is None or target == source or target.max is None:
        return Move(source, target)
    others = jira.keys_in(target, excluding=key)
    if len(others) >= target.max:
        where = source.name if source else issue["status"]["name"]
        raise BlockedError(
            f"{key} cannot move to {status}: column {target.name!r} is at its WIP limit "
            f"({len(others)}/{target.max}): {', '.join(others)}. "
            f"Leave {key} in {where}, comment on it, tell the owner and start nothing else. "
            "Only the owner overrides a limit, in the Jira UI, with a comment on the ticket (docs/process/kanban.md)."
        )
    return Move(source, target)


def after_transition(jira: Jira, key: str, move: Move) -> list[str]:
    """After a move out of a column with a board minimum, say when that column is below it.

    The minimum is a signal, never a block: a failed count becomes a notice, not an error.
    The moved key is left out of the count because Jira search can still list it in its old
    status for a moment.
    """
    source = move.source
    if source is None or source == move.target or source.min is None:
        return []
    try:
        count = len(jira.keys_in(source, excluding=key))
    except JiraError as error:  # the move is done: report, do not fail it
        return [f"could not check the {source.name} minimum: {error}"]
    if count >= source.min:
        return []
    notice = f"{source.name} is below its minimum ({count}/{source.min})"
    if source.name == IN_PROGRESS:
        notice += ": idle, ask the owner for the next ticket"
    return [notice]


def has_section(description: Any, heading: str) -> bool:
    """True when the description has a top-level heading with exactly this text and text under it."""
    content = description.get("content", []) if isinstance(description, dict) else []
    for start, node in enumerate(content):
        if not (_is_heading(node) and adf_to_text(node).strip() == heading):
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
    print(f"Assignee: {_name(fields.get('assignee'))}")
    if parent:
        print(f"Parent: {parent['key']}")
    print()
    print(adf_to_text(fields.get("description")) or "(no description)")
    for item in fields.get("comment", {}).get("comments", []):
        print(f"\n--- {_name(item.get('author'))}, {item.get('created', '')}")
        print(adf_to_text(item.get("body")))


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise UsageError(f"cannot read {path}: {error.strerror}") from None


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

    transition = commands.add_parser("transition", help="move an issue to a status, by status name")
    transition.add_argument("key")
    transition.add_argument("status")

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
    return parser


def run(args: argparse.Namespace, jira: Jira) -> None:
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
            return
        for issue in issues:
            fields = issue["fields"]
            print(f"{issue['key']}\t{fields['status']['name']}\t{_name(fields.get('assignee'))}\t{fields['summary']}")
    elif args.command == "create":
        print(jira.create(args.parent, args.summary, _read_text(args.description_file)))
    elif args.command == "transition":
        target, notices = jira.transition(args.key, args.status)
        print(f"{args.key} -> {target}")
        for notice in notices:
            print(notice)
    elif args.command == "comment":
        print(f"{args.key} comment {jira.comment(args.key, _read_text(args.body_file))}")
    elif args.command == "assign":
        account_id = jira.myself()["accountId"] if args.me else args.account_id
        jira.assign(args.key, account_id)
        print(f"{args.key} assigned to {account_id}")
    elif args.command == "rank":
        jira.rank(args.keys, before=args.before, after=args.after)
        print(f"{' '.join(args.keys)} ranked {'before ' + args.before if args.before else 'after ' + args.after}")
    elif args.command == "section":
        changed = jira.set_section(args.key, args.heading, args.body)
        print(f"{args.key} section {args.heading!r} {'set' if changed else 'unchanged'}")


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
        keys = [getattr(args, name, None) for name in ("key", "parent", "before", "after")] + getattr(args, "keys", [])
        for key in keys:
            if key is not None:
                check_key(key)  # before 1Password and before reading any file
        if args.command == "rank":
            check_rank_input(args.keys, args.before or args.after)
        if args.command == "section":
            args.body = _read_text(args.body_file)
            check_section_input(args.heading, args.body)
        jira = Jira(*credentials_reader(), opener_factory())
        run(args, jira)
    except CredentialError as error:
        print(f"jira: no Jira credential: {error}. STOP: do not use another Jira login. Report to the owner.", file=sys.stderr)
        return EXIT_CREDENTIAL
    except BlockedError as error:
        print(f"jira: blocked: {error}", file=sys.stderr)
        return EXIT_BLOCKED
    except JiraError as error:
        print(f"jira: {error}", file=sys.stderr)
        return EXIT_JIRA
    except (KeyError, TypeError, AttributeError) as error:
        print(f"jira: unexpected response from Jira ({type(error).__name__}: {error})", file=sys.stderr)
        return EXIT_JIRA
    except UsageError as error:
        print(f"jira: {error}", file=sys.stderr)
        return EXIT_USAGE
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
