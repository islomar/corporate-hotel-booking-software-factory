#!/usr/bin/env python3
"""Act in Jira as the agent account "Claude (CHB agent)" (CHB-107).

Usage:
  factory/bin/jira view CHB-12 [--json]
  factory/bin/jira search 'project = CHB AND status = "In Review"' [--json]
  factory/bin/jira create --parent CHB-27 --summary TEXT --description-file FILE
  factory/bin/jira transition CHB-12 "In Review"
  factory/bin/jira comment CHB-12 --body-file FILE      (FILE "-" reads stdin)
  factory/bin/jira assign CHB-12 --me | --account-id ID

The account email and API token are read from the 1Password item chb-jira-claude.api-token
(fields username and credential) on every call. The token stays in this process: it is
never printed, logged, written to a file or passed to another process.

The account is a CHB project Administrator (Jira Free has no Member role), so this
wrapper is the guardrail. It only touches issues whose key is CHB-<n>, creates only
Tasks under a CHB Epic, and has no delete, no project, board, workflow or permission
writes, and no raw request command. Do not add one.

Exit codes:
  0  done
  1  Jira refused or failed (HTTP error, unknown status, parent is not an epic)
  2  usage error
  3  no credential: 1Password is locked or `op` failed. The agent stops and reports.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

SITE = "https://islomar.atlassian.net"
PROJECT = "CHB"
ITEM = "chb-jira-claude.api-token"
USERNAME_COMMAND = ["op", "item", "get", ITEM, "--fields", "label=username"]
TOKEN_COMMAND = ["op", "item", "get", ITEM, "--fields", "label=credential", "--reveal"]
KEY_PATTERN = re.compile(rf"^{PROJECT}-[1-9][0-9]*$")
URL_PATTERN = re.compile(r"https?://[^\s<>()]*[^\s<>().,;:!?'\"]")
SEARCH_FIELDS = ["summary", "status", "assignee", "issuetype", "parent"]

EXIT_JIRA = 1
EXIT_USAGE = 2
EXIT_CREDENTIAL = 3


class JiraError(Exception):
    """Jira refused the request or the request makes no sense. Exit code 1."""


class UsageError(Exception):
    """A local input problem, such as an unreadable file. Exit code 2."""


class CredentialError(Exception):
    """The token could not be read. Exit code 3: the agent stops."""


def read_secret(command: list[str], timeout: int = 60) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed command
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CredentialError(f"could not run {command[0]}: {type(error).__name__}") from None
    value = result.stdout.decode().strip()
    if result.returncode != 0:
        raise CredentialError(f"{command[0]} exited {result.returncode} (is 1Password locked?)")
    if not value:
        field = next((arg for arg in command if arg.startswith("label=")), command[-1])
        raise CredentialError(f"1Password field {field} is empty")
    return value


def read_credentials(reader: Callable[[list[str]], str] = read_secret) -> tuple[str, str]:
    """Email and API token of the agent account, both from the 1Password item, on every call."""
    return reader(USERNAME_COMMAND), reader(TOKEN_COMMAND)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header never follows one to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


Opener = Callable[[urllib.request.Request], Any]


def default_opener() -> Opener:
    opener = urllib.request.build_opener(_NoRedirect)
    return lambda request: opener.open(request, timeout=30)


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text to an Atlassian Document. Blank lines split paragraphs, URLs become links."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        content: list[dict[str, Any]] = []
        for i, line in enumerate(block.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            content.extend(_inline(line))
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


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


class Jira:
    def __init__(self, email: str, token: str, opener: Opener, site: str = SITE) -> None:
        credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
        self._auth = f"Basic {credentials}"
        self._opener = opener
        self._site = site

    def request(self, method: str, path: str, body: object | None = None, *, empty_ok: bool = False) -> dict[str, Any]:
        """Send one request. The answer must be a JSON object, or empty where empty_ok (HTTP 204)."""
        request = urllib.request.Request(  # noqa: S310 - fixed https site
            self._site + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": self._auth, "Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise JiraError(f"{method} {path}: HTTP {error.code} {_jira_messages(error)}") from None
        except urllib.error.URLError as error:
            raise JiraError(f"{method} {path}: {error.reason}") from None
        except OSError as error:  # timeouts and resets while reading the response
            raise JiraError(f"{method} {path}: {type(error).__name__}") from None
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

    def transition(self, key: str, status: str) -> str:
        check_key(key)
        transitions = self.request("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
        matches = [t for t in transitions if t["to"]["name"].casefold() == status.casefold()]
        if not matches:
            names = sorted({t["to"]["name"] for t in transitions})
            raise JiraError(f"{key} cannot move to {status!r}. Allowed: {', '.join(names) or 'none'}")
        target = matches[0]["to"]["name"]
        before_transition(self, key, target)
        self.request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": matches[0]["id"]}}, empty_ok=True)
        return target

    def comment(self, key: str, body: str) -> str:
        if not body.strip():
            raise JiraError("comment body is empty")
        created = self.request("POST", f"/rest/api/3/issue/{check_key(key)}/comment", {"body": text_to_adf(body)})
        return str(created["id"])

    def assign(self, key: str, account_id: str) -> None:
        self.request("PUT", f"/rest/api/3/issue/{check_key(key)}/assignee", {"accountId": account_id}, empty_ok=True)


def before_transition(jira: Jira, key: str, status: str) -> None:
    """Hook point, called with the target status name just before every transition POST.

    CHB-108 adds the WIP-limit check here: raise JiraError to block the move.
    It does nothing yet.
    """


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
        print(f"{args.key} -> {jira.transition(args.key, args.status)}")
    elif args.command == "comment":
        print(f"{args.key} comment {jira.comment(args.key, _read_text(args.body_file))}")
    elif args.command == "assign":
        account_id = jira.myself()["accountId"] if args.me else args.account_id
        jira.assign(args.key, account_id)
        print(f"{args.key} assigned to {account_id}")


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
        for key in (getattr(args, "key", None), getattr(args, "parent", None)):
            if key is not None:
                check_key(key)  # before 1Password and before reading any file
        jira = Jira(*credentials_reader(), opener_factory())
        run(args, jira)
    except CredentialError as error:
        print(f"jira: no Jira credential: {error}. STOP: do not use another Jira login. Report to the owner.", file=sys.stderr)
        return EXIT_CREDENTIAL
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
