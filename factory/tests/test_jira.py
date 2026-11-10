import base64
import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jira
from op_service_account import ServiceAccountError
from jira import (
    CredentialError,
    Jira,
    JiraError,
    UsageError,
    adf_to_text,
    main,
    read_credentials,
    read_secret,
    replace_section,
    text_to_adf,
)

TOKEN = "fake-token-123"
EMAIL = "agent@example.com"
CREDENTIALS = (EMAIL, TOKEN)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeJira:
    """Opener that answers from a table of (method, path) -> JSON or HTTP status, and records requests."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, Any, dict[str, str]]] = []

    def __call__(self, request: Any) -> FakeResponse:
        path = request.full_url.removeprefix(jira.SITE)
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), path, body, dict(request.header_items())))
        answer = self.routes[(request.get_method(), path)]
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, int):
            error_body = io.BytesIO(json.dumps({"errorMessages": ["nope"], "errors": {"f": "bad"}}).encode())
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, error_body)  # type: ignore[arg-type]
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(b"" if answer is None else json.dumps(answer).encode())

    def writes(self) -> list[tuple[str, str, Any]]:
        """Requests that change Jira. A JQL search is a POST but only reads."""
        return [(m, p, b) for m, p, b, _ in self.requests if m != "GET" and p != SEARCH]

    def searches(self) -> list[str]:
        return [b["jql"] for m, p, b, _ in self.requests if (m, p) == ("POST", SEARCH)]


def _run(argv: list[str], fake: FakeJira, credentials_reader: Any = lambda: CREDENTIALS) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, credentials_reader=credentials_reader, opener_factory=lambda: fake)
    return code, out.getvalue(), err.getvalue()


SEARCH = "/rest/api/3/search/jql"
RANK_PATH = "/rest/agile/1.0/issue/rank"
BOARD = "/rest/agile/1.0/board/2/configuration"
# Status ids as on the real board: To Do 10003, In Progress 3, In Review 10004, Validating 10006,
# Needs human 10007, Done 10005.
STATUS_IDS = {
    "To Do": "10003",
    "In Progress": "3",
    "In Review": "10004",
    "Validating": "10006",
    "Needs human": "10007",
    "Done": "10005",
}
TRANSITIONS = {
    "transitions": [
        {"id": str(n), "to": {"name": name, "id": status_id}} for n, (name, status_id) in enumerate(STATUS_IDS.items(), 11)
    ]
}


# CHB-153: the owner adds a "Backlog" column first, then renames "To Do" to "Ready". The project is
# team-managed, so a column is a status: Backlog is a new status, and the rename keeps the status id.
# "before": today's board. "half": Backlog plus To Do. "after": Backlog plus Ready.
BACKLOG_ID = "10009"  # as on the live board, 2026-11-10
READY_ID = "10010"  # the live Ready column also holds a new status "Ready" next to "To Do" (10003)
ALL_IDS = {**STATUS_IDS, "Backlog": BACKLOG_ID, "Ready": STATUS_IDS["To Do"]}
BOARD_STATES = ("before", "half", "after")


def state_ids(state: str = "before") -> dict[str, str]:
    """Status names and ids, in board order, of one board state."""
    if state == "before":
        return dict(STATUS_IDS)
    pull = "Ready" if state == "after" else "To Do"
    rest = {name: status_id for name, status_id in STATUS_IDS.items() if name != "To Do"}
    return {"Backlog": BACKLOG_ID, pull: STATUS_IDS["To Do"], **rest}


def pull_name(state: str) -> str:
    return "Ready" if state == "after" else "To Do"


def transitions_of(state: str = "before") -> dict[str, Any]:
    names = enumerate(state_ids(state).items(), 11)
    return {"transitions": [{"id": str(n), "to": {"name": name, "id": status_id}} for n, (name, status_id) in names]}


def board_config(*, state: str = "before", **limits: dict[str, int]) -> dict[str, Any]:
    """A board configuration with one column per status. Limits: board_config(**{"In Review": {"max": 3}})."""
    columns = []
    for name, status_id in state_ids(state).items():
        ids = [status_id, READY_ID] if (state, name) == ("after", "Ready") else [status_id]
        columns.append({"name": name, "statuses": [{"id": i, "self": "x"} for i in ids], **limits.get(name, {})})
    return {"id": 2, "columnConfig": {"columns": columns, "constraintType": "none"}}


BOARD_LIMITS = {"In Progress": {"min": 1, "max": 3}, "In Review": {"max": 3}, "Validating": {"max": 3}, "Needs human": {"max": 3}}
SECTIONS = {
    "Validating": "Acceptance criteria (Validating)",
    "Needs human": "Needs human: owner steps",
}


def description_with(*headings: str, body: str = "Some text.") -> dict[str, Any]:
    content: list[dict[str, Any]] = [*doc(WHY_SECTION)["content"], {"type": "paragraph", "content": [{"type": "text", "text": "Intro."}]}]
    for heading in headings:
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": heading}]})
        if body:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": body}]})
    return {"type": "doc", "version": 1, "content": content}


def issue_in(status: str, description: Any = None, links: list[dict[str, Any]] | None = None, *, kind: str = "Task") -> dict[str, Any]:
    fields = {
        "status": {"name": status, "id": ALL_IDS[status]},
        "description": description,
        "issuelinks": links or [],
        "issuetype": {"name": kind},
    }
    return {"fields": fields}


def blocked_by(*keys: str, category: str = "indeterminate") -> list[dict[str, Any]]:
    """Issue links as Jira lists them on the blocked ticket: "is blocked by" each key (CHB-151)."""
    linked = {"fields": {"status": {"name": "x", "statusCategory": {"key": category}}}}
    return [{"id": str(n), "type": BLOCKS_TYPE, "inwardIssue": {"key": key, **linked}} for n, key in enumerate(keys, 100)]


def blocks(*keys: str) -> list[dict[str, Any]]:
    """Issue links as Jira lists them on the blocking ticket: "blocks" each key."""
    return [{"id": str(n), "type": BLOCKS_TYPE, "outwardIssue": {"key": key}} for n, key in enumerate(keys, 200)]


BLOCKS_TYPE = {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"}
RELATES_TYPE = {"id": "10003", "name": "Relates", "inward": "relates to", "outward": "relates to"}
ISSUE_FIELDS = "status,description,issuelinks,issuetype"


def doc(markdown: str) -> dict[str, Any]:
    return text_to_adf(markdown)


WHY_SECTION = "## Why it matters\n\nThe owner waits for the thing."
READY = doc(f"{WHY_SECTION}\n\n## Acceptance criteria\n\n- A test shows the thing.")
NOT_READY = doc(f"{WHY_SECTION}\n\n## Plan\n\nDo the thing, some day.")
OPEN_JQL = "project = CHB AND (statusCategory != Done) ORDER BY Rank ASC"


def keys_answer(
    by_status: dict[str, list[str]],
    descriptions: dict[str, Any] | None = None,
    epics: tuple[str, ...] = (),
    links: dict[str, list[dict[str, Any]]] | None = None,
    priorities: dict[str, str | None] | None = None,
) -> Any:
    """Answer a JQL search like Jira would.

    The open-tickets search (statusCategory != Done, in rank order) gets every key listed for a
    status that is not Done, with its status, type, description (READY unless given), issue links and
    priority (Medium unless given; None leaves the field out).
    A column count (status in (...) AND key != X) gets the keys of the statuses it names, minus X,
    and without epics when it asks for no epics (CHB-157).
    """
    descriptions = descriptions or {}
    links = links or {}
    priorities = priorities or {}

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        jql = body["jql"]
        if "statusCategory != Done" in jql:
            assert "issuelinks" in body["fields"], "the open search must ask for the issue links"
            issues = [
                {
                    "key": k,
                    "fields": {
                        "status": {"name": name, "id": ALL_IDS[name]},
                        "issuetype": {"name": "Epic" if k in epics else "Task"},
                        "description": descriptions.get(k, READY),
                        "issuelinks": links.get(k, []),
                        "priority": None if priorities.get(k, "Medium") is None else {"name": priorities.get(k, "Medium")},
                    },
                }
                for name, ks in by_status.items()
                if name != "Done"
                for k in ks
            ]
            return {"issues": issues, "isLast": True}
        excluded = jql.split("key != ")[1].split()[0].rstrip(")")
        ids = {i.strip() for i in jql.split("status in (")[1].split(")")[0].split(",")}
        no_epics = "issuetype != Epic" in jql
        keys = [k for name, ks in by_status.items() if ALL_IDS[name] in ids for k in ks if k != excluded and not (no_epics and k in epics)]
        return {"issues": [{"key": k} for k in keys], "isLast": True}

    return answer


def transition_fake(
    current: str,
    by_status: dict[str, list[str]] | None = None,
    description: Any = READY,
    board: Any = None,
    descriptions: dict[str, Any] | None = None,
    epics: tuple[str, ...] = (),
    links: dict[str, list[dict[str, Any]]] | None = None,
    priorities: dict[str, str | None] | None = None,
    state: str = "before",
) -> FakeJira:
    """CHB-12 sits in `current`. A ticket in the pull list is listed there unless the test places it."""
    by_status = dict(by_status or {})
    if current in ("To Do", "Ready") and "CHB-12" not in by_status.get(current, []):
        by_status[current] = ["CHB-12", *by_status.get(current, [])]
    descriptions = {"CHB-12": description, **(descriptions or {})}
    links = links or {}
    return FakeJira(
        {
            ("GET", "/rest/api/3/issue/CHB-12/transitions"): transitions_of(state),
            ("GET", f"/rest/api/3/issue/CHB-12?fields={ISSUE_FIELDS}"): issue_in(current, description, links.get("CHB-12")),
            ("GET", BOARD): board_config(state=state, **BOARD_LIMITS) if board is None else board,
            ("POST", SEARCH): keys_answer(by_status, descriptions, epics, links, priorities),
            ("POST", "/rest/api/3/issue/CHB-12/transitions"): None,
            ("PUT", RANK_PATH): None,
        }
    )


def searches_before_the_move(fake: FakeJira) -> list[str]:
    """JQL of the searches sent before the transition POST."""
    jql = []
    for method, path, body, _ in fake.requests:
        if (method, path) == ("POST", "/rest/api/3/issue/CHB-12/transitions"):
            break
        if (method, path) == ("POST", SEARCH):
            jql.append(body["jql"])
    return jql


def _search_changes_at_the_move(
    fake: FakeJira,
    before: dict[str, list[str]],
    after: dict[str, list[str]],
    descriptions: dict[str, Any],
    links: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    """Answer searches from `before` until the transition POST, then from `after`."""
    old, new = keys_answer(before, descriptions, links=links), keys_answer(after, descriptions, links=links)

    def answer(body: dict[str, Any]) -> dict[str, Any]:
        moved = any((method, path) == ("POST", "/rest/api/3/issue/CHB-12/transitions") for method, path, _, _ in fake.requests)
        return new(body) if moved else old(body)

    return answer


def _no_credentials() -> tuple[str, str]:
    raise AssertionError("1Password must not be read")


class AuthTest(unittest.TestCase):
    def test_basic_auth_with_the_agent_email_and_token(self) -> None:
        fake = FakeJira({("GET", "/rest/api/3/myself"): {"accountId": "a1"}})
        Jira(EMAIL, TOKEN, fake).myself()

        headers = fake.requests[0][3]
        expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        self.assertEqual(headers["Authorization"], f"Basic {expected}")


class TransitionTest(unittest.TestCase):
    def test_moves_by_status_name_case_insensitively(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-1"]})
        code, out, _ = _run(["transition", "CHB-12", "in review"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [("POST", "/rest/api/3/issue/CHB-12/transitions", {"transition": {"id": "13"}})])
        self.assertEqual(out.splitlines()[0], "CHB-12 -> In Review")

    def test_unknown_status_fails_and_lists_the_allowed_ones(self) -> None:
        fake = FakeJira(
            {
                ("GET", "/rest/api/3/issue/CHB-12/transitions"): {"transitions": TRANSITIONS["transitions"][1:3]},
                ("GET", BOARD): board_config(),  # no status matches, so the column names are tried (CHB-153)
            }
        )
        code, _, err = _run(["transition", "CHB-12", "Done"], fake)

        self.assertEqual(code, 1)
        self.assertIn("Allowed: In Progress, In Review", err)
        self.assertEqual(fake.writes(), [])

    def test_hook_runs_before_the_move_and_can_block_it(self) -> None:
        """CHB-108 puts the WIP check in before_transition."""
        fake = FakeJira({("GET", "/rest/api/3/issue/CHB-12/transitions"): TRANSITIONS})
        hook = mock.Mock(side_effect=JiraError("WIP limit reached"))
        with mock.patch.object(jira, "before_transition", hook):
            code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 1)
        self.assertEqual(hook.call_args.args[1:], ("CHB-12", "In Progress"))
        self.assertEqual(hook.call_args.kwargs, {"status_id": "3", "trigger": None})
        self.assertIn("WIP limit reached", err)
        self.assertEqual(fake.writes(), [])


class WipLimitTest(unittest.TestCase):
    """CHB-108, CHB-127: a full column blocks starting work (exit 4), never recording a PR or a merge."""

    def test_starting_with_a_full_column_blocks_with_column_limit_and_tickets(self) -> None:
        for full_column in ["In Progress", "In Review", "Validating"]:
            with self.subTest(full_column=full_column):
                fake = transition_fake("To Do", {full_column: ["CHB-1", "CHB-2", "CHB-3"]})
                code, out, err = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 4)
                self.assertEqual(out, "")
                self.assertIn(f"column {full_column!r} is at its WIP limit (3/3): CHB-1, CHB-2, CHB-3", err)
                self.assertIn("Leave CHB-12 in To Do", err)
                self.assertEqual(fake.writes(), [])

    def test_starting_names_every_full_column(self) -> None:
        fake = transition_fake("To Do", {"In Review": ["CHB-1", "CHB-2", "CHB-3"], "Validating": ["CHB-4", "CHB-5", "CHB-6"]})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("'In Review' is at its WIP limit (3/3): CHB-1, CHB-2, CHB-3", err)
        self.assertIn("'Validating' is at its WIP limit (3/3): CHB-4, CHB-5, CHB-6", err)

    def test_a_column_over_its_max_blocks_starting_too(self) -> None:
        fake = transition_fake("To Do", {"Validating": ["CHB-1", "CHB-2", "CHB-3", "CHB-4"]})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("(4/3)", err)

    def test_count_asks_for_the_column_statuses_in_chb_without_the_ticket(self) -> None:
        fake = transition_fake("To Do", {"Validating": ["CHB-1", "CHB-2"]})
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertIn("project = CHB AND (status in (10006) AND key != CHB-12 AND issuetype != Epic) ORDER BY key ASC", fake.searches())

    def test_below_the_limits_starts(self) -> None:
        fake = transition_fake("To Do", {"In Progress": ["CHB-1"], "In Review": ["CHB-2", "CHB-3"], "Validating": ["CHB-4"]})
        code, out, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-12 -> In Progress", "In Progress 2/3: 1 slot free; next: nothing to start in To Do"])
        self.assertEqual(len(fake.writes()), 1)

    def test_limit_comes_from_the_board_config(self) -> None:
        board = board_config(**{"Validating": {"max": 1}})
        fake = transition_fake("To Do", {"Validating": ["CHB-1"]}, board=board)
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("(1/1): CHB-1", err)

    def test_recording_a_pr_or_a_merge_is_never_blocked(self) -> None:
        full = ["CHB-1", "CHB-2", "CHB-3", "CHB-4"]
        for current, target in [("In Progress", "In Review"), ("In Review", "Validating")]:
            with self.subTest(target=target):
                description = description_with(*SECTIONS.values())
                fake = transition_fake(current, {name: full for name in STATUS_IDS}, description=description)
                code, out, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines()[0], f"CHB-12 -> {target}")
                self.assertEqual(len(fake.writes()), 1)

    def test_going_back_to_in_progress_is_not_starting_work(self) -> None:
        full = ["CHB-1", "CHB-2", "CHB-3"]
        for current in ["In Review", "Validating", "Needs human"]:
            with self.subTest(current=current):
                fake = transition_fake(current, {"In Progress": full, "In Review": full, "Validating": full})
                code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 0)
                self.assertEqual(len(fake.writes()), 1)

    def test_a_full_needs_human_still_blocks(self) -> None:
        fake = transition_fake("In Progress", {"Needs human": ["CHB-1", "CHB-2", "CHB-3"]}, description=description_with(SECTIONS["Needs human"]))
        code, _, err = _run(["transition", "CHB-12", "Needs human"], fake)

        self.assertEqual(code, 4)
        self.assertIn("column 'Needs human' is at its WIP limit (3/3)", err)
        self.assertIn("Leave CHB-12 in In Progress", err)
        self.assertEqual(fake.writes(), [])

    def test_columns_without_a_max_never_block(self) -> None:
        many = [f"CHB-{n}" for n in range(1, 40)]
        for target in ["Done", "To Do"]:
            with self.subTest(target=target):
                fake = transition_fake("In Review", {target: many})
                code, _, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                self.assertEqual(searches_before_the_move(fake), [])

    def test_a_move_within_the_same_column_is_never_blocked(self) -> None:
        board = board_config(**{"Needs human": {"max": 1}})
        board["columnConfig"]["columns"][4]["statuses"].append({"id": "3"})  # In Progress and Needs human together
        del board["columnConfig"]["columns"][1]
        fake = transition_fake("In Progress", {"Needs human": ["CHB-1", "CHB-2"]}, board=board, description=description_with(SECTIONS["Needs human"]))
        code, out, _ = _run(["transition", "CHB-12", "Needs human"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.searches(), [])
        self.assertEqual(out.splitlines(), ["CHB-12 -> Needs human"])  # no free-slot line within one column

    def test_a_status_on_no_column_is_never_blocked(self) -> None:
        board = board_config(**BOARD_LIMITS)
        del board["columnConfig"]["columns"][4]  # Needs human is not on the board
        fake = transition_fake("In Progress", {"Needs human": ["CHB-1", "CHB-2", "CHB-3"]}, board=board, description=description_with(SECTIONS["Needs human"]))
        code, _, _ = _run(["transition", "CHB-12", "Needs human"], fake)

        self.assertEqual(code, 0)

    def test_an_unreadable_board_blocks_with_exit_1(self) -> None:
        fake = transition_fake("To Do", board=500)
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 1)
        self.assertIn("board/2/configuration: HTTP 500", err)
        self.assertEqual(fake.writes(), [])


class MinimumSignalTest(unittest.TestCase):
    """CHB-108: the In Progress minimum is a signal, never a block."""

    def test_leaving_in_progress_empty_prints_idle_and_exits_0(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-12"]})  # search still lists the moved ticket
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "CHB-12 -> In Review",
                "In Progress is below its minimum (0/1)",
                "In Progress 0/3: 3 slots free; next: nothing to start in To Do",
            ],
        )
        self.assertIn("project = CHB AND (status in (3) AND key != CHB-12 AND issuetype != Epic) ORDER BY key ASC", fake.searches())

    def test_a_failed_count_after_the_move_is_a_notice_not_a_failure(self) -> None:
        fake = transition_fake("In Progress")
        fake.routes[("POST", SEARCH)] = lambda body: 500 if "status in (3)" in body["jql"] else {"issues": []}
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-12 -> In Review")
        self.assertIn("could not check the In Progress minimum", out)
        self.assertEqual(len(fake.writes()), 1)

    def test_no_signal_while_in_progress_holds_its_minimum(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-7"]})
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertNotIn("minimum", out)

    def test_no_signal_when_the_ticket_did_not_leave_in_progress(self) -> None:
        fake = transition_fake("To Do")
        code, out, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-12 -> In Progress", "In Progress 1/3: 2 slots free; next: nothing to start in To Do"])


class ReadinessTest(unittest.TestCase):
    """CHB-125: acceptance criteria and dependencies, read from the description."""

    def test_acceptance_criteria_as_a_heading_or_a_line_with_text_under_it(self) -> None:
        ready = {
            "heading": "Intro.\n\n## Acceptance criteria\n\n- A test.",
            "heading, other case": "### acceptance criteria\n\nAC1. It works.",
            "line with a colon": "Intro.\n\nAcceptance criteria:\n\n- A test.",
            "bold line": "**Acceptance criteria:**\n\n1. A test.",
            "both sections": "## Acceptance criteria\n\n- A test.\n\n## Acceptance criteria (Validating)\n\n- Live.",
        }
        for name, markdown in ready.items():
            with self.subTest(name):
                self.assertTrue(jira.has_acceptance_criteria(doc(markdown)))

    def test_no_acceptance_criteria(self) -> None:
        not_ready = {
            "none": "Do the thing.",
            "empty section": "## Acceptance criteria\n\n## Notes\n\nText.",
            "mid-line mention": "Rejects tickets without testable acceptance criteria.",
            "prose that starts with the label": "Acceptance criteria are still to be written.\n\nMore text.",
            "only the Validating section": "## Acceptance criteria (Validating)\n\n- Live check.",
            "no description": None,
        }
        for name, markdown in not_ready.items():
            with self.subTest(name):
                self.assertFalse(jira.has_acceptance_criteria(None if markdown is None else doc(markdown)))

    def test_depends_on_text_is_not_read(self) -> None:
        """CHB-151: dependencies are links only; a "Depends on" line in the description blocks nothing."""
        fields = {"description": doc("Depends on: CHB-91.\n\n- Depends on CHB-88."), "issuelinks": []}
        self.assertEqual(jira.blockers(fields), [])

    def test_blockers_are_the_is_blocked_by_links(self) -> None:
        """CHB-151: a "Blocks" link with the other ticket as inwardIssue means this ticket is blocked by it."""
        fields = {"description": READY, "issuelinks": [*blocked_by("CHB-91", "CHB-88"), *blocks("CHB-5")]}
        self.assertEqual(jira.blockers(fields), ["CHB-91", "CHB-88"])

    def test_other_link_types_do_not_block(self) -> None:
        relates = [{"id": "1", "type": RELATES_TYPE, "inwardIssue": {"key": "CHB-91"}}]
        self.assertEqual(jira.blockers({"description": READY, "issuelinks": relates}), [])

    def test_no_links_field_means_no_blockers(self) -> None:
        self.assertEqual(jira.blockers({"description": READY}), [])
        self.assertEqual(jira.blockers({"description": READY, "issuelinks": None}), [])

    def test_open_blockers(self) -> None:
        fields = {"description": READY, "issuelinks": blocked_by("CHB-91", "CHB-88")}
        self.assertEqual(jira.open_blockers(fields, open_keys={"CHB-91", "CHB-7"}), ["CHB-91"])
        self.assertEqual(jira.open_blockers(fields, open_keys={"CHB-7"}), [])


class TopTicketTest(unittest.TestCase):
    """CHB-125: a start from To Do takes the top ticket only: the highest-ranked non-epic ticket
    without an open dependency. Missing acceptance criteria do not skip it: the agent writes them."""

    def test_a_ticket_below_the_top_is_refused_and_jira_is_unchanged(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-5", "CHB-12"]})
        code, out, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertIn("CHB-12 is not the top ticket of To Do: CHB-5 is", err)
        self.assertIn("factory/bin/jira rank CHB-12 --before CHB-5", err)
        self.assertEqual(fake.writes(), [])

    def test_the_top_ticket_goes_through(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-12", "CHB-5"]})
        code, out, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-12 -> In Progress")
        self.assertEqual(len(fake.writes()), 1)
        self.assertIn(OPEN_JQL, fake.searches())

    def test_a_top_ticket_without_acceptance_criteria_is_not_skipped(self) -> None:
        """CHB-50 has no criteria but is top: starting CHB-12 below it is refused and names CHB-50."""
        fake = transition_fake("To Do", {"To Do": ["CHB-50", "CHB-12"]}, descriptions={"CHB-50": NOT_READY})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 is not the top ticket of To Do: CHB-50 is", err)
        self.assertEqual(fake.writes(), [])

    def test_the_top_ticket_without_acceptance_criteria_waits_for_them(self) -> None:
        for name, description in {"none": NOT_READY, "only Validating": doc(f"{WHY_SECTION}\n\n## Acceptance criteria (Validating)\n\n- Live.")}.items():
            with self.subTest(name):
                fake = transition_fake("To Do", {"To Do": ["CHB-12", "CHB-5"]}, description=description)
                code, out, err = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 4)
                self.assertEqual(out, "")
                self.assertIn("CHB-12 has no acceptance criteria. Write them first:", err)
                self.assertIn('factory/bin/jira section CHB-12 --heading "Acceptance criteria" --body-file FILE', err)
                self.assertIn("ask the owner only for decisions that are the owner's", err)
                self.assertEqual(fake.writes(), [])

    def test_epics_and_tickets_with_open_dependencies_are_skipped_when_finding_the_top(self) -> None:
        fake = transition_fake(
            "To Do",
            {"To Do": ["CHB-9", "CHB-49", "CHB-12", "CHB-5"], "In Progress": ["CHB-91"]},
            links={"CHB-49": blocked_by("CHB-91")},
            epics=("CHB-9",),
        )
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)
        self.assertEqual(code, 0)

        refused = transition_fake(
            "To Do",
            {"To Do": ["CHB-9", "CHB-49", "CHB-5", "CHB-12"], "In Progress": ["CHB-91"]},
            links={"CHB-49": blocked_by("CHB-91")},
            epics=("CHB-9",),
        )
        code, _, err = _run(["transition", "CHB-12", "In Progress"], refused)
        self.assertEqual(code, 4)
        self.assertIn("CHB-12 is not the top ticket of To Do: CHB-5 is", err)

    def test_a_ticket_with_depends_on_text_but_no_link_starts(self) -> None:
        """CHB-151: the text is not read any more."""
        description = doc(f"{WHY_SECTION}\n\nDepends on CHB-91.\n\nAcceptance criteria:\n\n- A test.")
        fake = transition_fake("To Do", {"In Review": ["CHB-91"]}, description=description)
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)

    def test_a_ticket_blocked_by_an_open_ticket_cannot_start(self) -> None:
        """CHB-151 AC1: an "is blocked by" link to an open ticket blocks the start, with the reason."""
        fake = transition_fake("To Do", {"In Review": ["CHB-91"]}, links={"CHB-12": blocked_by("CHB-91")})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 cannot start: it is blocked by open tickets: CHB-91", err)
        self.assertEqual(fake.writes(), [])

    def test_a_ticket_blocked_by_a_done_ticket_starts(self) -> None:
        """CHB-151 AC1: Done and Discarded leave the open search, so their links no longer block."""
        fake = transition_fake("To Do", {"Done": ["CHB-91"]}, links={"CHB-12": blocked_by("CHB-91", category="done")})
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)

    def test_a_link_in_the_fresh_read_counts_even_when_the_search_misses_it(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-12"], "In Review": ["CHB-91"]})
        fake.routes[("GET", f"/rest/api/3/issue/CHB-12?fields={ISSUE_FIELDS}")] = issue_in("To Do", READY, blocked_by("CHB-91"))
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("blocked by open tickets: CHB-91", err)

    def test_the_blocking_side_of_a_link_is_not_blocked(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-12", "CHB-5"]}, links={"CHB-12": blocks("CHB-5"), "CHB-5": blocked_by("CHB-12")})
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)

    def test_a_ticket_blocked_by_an_open_ticket_is_skipped_when_finding_the_top(self) -> None:
        fake = transition_fake(
            "To Do", {"To Do": ["CHB-49", "CHB-12"], "In Progress": ["CHB-91"]}, links={"CHB-49": blocked_by("CHB-91")}
        )
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)

    def test_a_relates_link_does_not_skip_a_ticket(self) -> None:
        relates = [{"id": "1", "type": RELATES_TYPE, "inwardIssue": {"key": "CHB-91"}}]
        fake = transition_fake("To Do", {"To Do": ["CHB-49", "CHB-12"], "In Progress": ["CHB-91"]}, links={"CHB-49": relates})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 is not the top ticket of To Do: CHB-49 is", err)

    def test_moving_back_into_in_progress_is_not_checked(self) -> None:
        for current in ["In Review", "Validating", "Needs human"]:
            with self.subTest(current=current):
                fake = transition_fake(current, {"To Do": ["CHB-5"]}, description=NOT_READY)
                code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 0)
                self.assertNotIn(OPEN_JQL, searches_before_the_move(fake))

    def test_an_epic_is_refused(self) -> None:
        fake = transition_fake("To Do", epics=("CHB-12",))
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 cannot start: it is an epic", err)
        self.assertEqual(fake.writes(), [])

    def test_the_fresh_read_wins_over_a_stale_search_answer(self) -> None:
        """The search still holds an old link to an open ticket; the fresh read has none."""
        fake = transition_fake("To Do", {"To Do": ["CHB-12", "CHB-5"], "In Review": ["CHB-91"]}, links={"CHB-12": blocked_by("CHB-91")})
        fake.routes[("GET", f"/rest/api/3/issue/CHB-12?fields={ISSUE_FIELDS}")] = issue_in("To Do", READY)
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)

    def test_a_ticket_missing_from_the_search_is_refused(self) -> None:
        fake = transition_fake("To Do")
        answer = fake.routes[("POST", SEARCH)]
        fake.routes[("POST", SEARCH)] = lambda body: {"issues": [], "isLast": True} if "statusCategory" in body["jql"] else answer(body)
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 is missing from the search of open tickets", err)
        self.assertEqual(fake.writes(), [])

    def test_a_ticket_the_search_still_shows_outside_to_do_is_refused(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-5"]})
        fake.routes[("POST", SEARCH)] = keys_answer({"Done": [], "In Review": ["CHB-12"], "To Do": ["CHB-5"]}, {"CHB-12": READY})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 is not in To Do in the search of open tickets yet", err)
        self.assertEqual(fake.writes(), [])

    def test_a_truncated_open_search_blocks_with_exit_1(self) -> None:
        """Past the cap, a dependency would look closed: refuse instead of guessing."""
        fake = transition_fake("To Do", {"To Do": ["CHB-12", "CHB-5", "CHB-6"]})
        with mock.patch.object(jira, "OPEN_LIMIT", 2):
            code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 1)
        self.assertIn("2 or more open tickets", err)
        self.assertEqual(fake.writes(), [])

    def test_a_failed_search_blocks_with_exit_1(self) -> None:
        fake = transition_fake("To Do")
        fake.routes[("POST", SEARCH)] = lambda body: 500 if "statusCategory" in body["jql"] else {"issues": []}
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", err)
        self.assertEqual(fake.writes(), [])


class FreeSlotTest(unittest.TestCase):
    """CHB-125: after a move, print the free In Progress slots and the next top tickets."""

    def test_a_move_that_leaves_a_free_slot_names_the_top_non_epic_ticket(self) -> None:
        fake = transition_fake(
            "In Progress",
            {"In Progress": ["CHB-1", "CHB-2", "CHB-91"], "To Do": ["CHB-9", "CHB-50", "CHB-123", "CHB-7"]},
            links={"CHB-50": blocked_by("CHB-91")},
            epics=("CHB-9",),
            board=board_config(**{**BOARD_LIMITS, "In Progress": {"min": 1, "max": 4}}),
        )
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-12 -> In Review", "In Progress 3/4: 1 slot free; next: CHB-123"])

    def test_a_ticket_without_acceptance_criteria_is_next_and_marked(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-1"], "To Do": ["CHB-122", "CHB-7"]}, descriptions={"CHB-122": NOT_READY})
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn("In Progress 1/3: 2 slots free; next: CHB-122 (needs acceptance criteria), CHB-7", out.splitlines())

    def test_next_tickets_follow_the_sorted_order_and_name_a_missing_why_section(self) -> None:
        """CHB-148: the notice and the start check agree, so the notice uses the order sort-todo makes."""
        fake = transition_fake(
            "In Progress",
            {"In Progress": ["CHB-1"], "To Do": ["CHB-7", "CHB-8", "CHB-122"]},
            descriptions={"CHB-122": doc("Do it.")},
            priorities={"CHB-7": "Low", "CHB-122": "High"},
        )
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn(
            "In Progress 1/3: 2 slots free; next: CHB-122 (needs Why it matters and acceptance criteria), CHB-8", out.splitlines()
        )

    def test_one_next_ticket_per_free_slot(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-1"], "To Do": ["CHB-123", "CHB-7", "CHB-8"]})
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn("In Progress 1/3: 2 slots free; next: CHB-123, CHB-7", out.splitlines())

    def test_no_startable_ticket_says_so(self) -> None:
        fake = transition_fake(
            "In Progress", {"In Progress": ["CHB-1"], "In Review": ["CHB-91"], "To Do": ["CHB-50"]}, links={"CHB-50": blocked_by("CHB-91")}
        )
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn("In Progress 1/3: 2 slots free; next: nothing to start in To Do", out.splitlines())

    def test_the_started_ticket_counts_and_is_not_next(self) -> None:
        fake = transition_fake("To Do", {"In Progress": ["CHB-1"], "To Do": ["CHB-12", "CHB-123"]})
        code, out, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-12 -> In Progress", "In Progress 2/3: 1 slot free; next: CHB-123"])

    def test_a_ticket_that_depends_on_the_moved_ticket_is_not_next(self) -> None:
        """CHB-143: the moved ticket is still open, so its dependents cannot start, whatever the search still shows."""
        depends_on_12 = {"CHB-114": blocked_by("CHB-12")}
        queue = ["CHB-114", "CHB-7"]
        cases = {
            "into Validating, search shows the old status": ("In Review", "Validating", {"In Review": ["CHB-12"]}),
            "into Validating, search shows the new status": ("In Review", "Validating", {"Validating": ["CHB-12"]}),
            "into Validating, search misses it": ("In Review", "Validating", {}),
            "start from To Do, search shows the old status": ("To Do", "In Progress", {"To Do": ["CHB-12"]}),
            "start from To Do, search shows the new status": ("To Do", "In Progress", {"In Progress": ["CHB-12"]}),
        }
        for name, (current, target, after) in cases.items():
            with self.subTest(name):
                fake = transition_fake(
                    current,
                    {"In Progress": ["CHB-1"], "To Do": queue},
                    description=description_with("Acceptance criteria", *SECTIONS.values()),
                    links=depends_on_12,
                )
                before = {"In Progress": ["CHB-1"], "To Do": [*(["CHB-12"] if current == "To Do" else []), *queue]}
                if current != "To Do":
                    before[current] = ["CHB-12"]
                placed = {"In Progress": ["CHB-1"], "To Do": queue}
                for status, keys in after.items():
                    placed[status] = [*keys, *placed.get(status, [])] if status == "To Do" else [*placed.get(status, []), *keys]
                fake.routes[("POST", SEARCH)] = _search_changes_at_the_move(fake, before, placed, {}, depends_on_12)
                code, out, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                expected = "In Progress 2/3: 1 slot free" if target == "In Progress" else "In Progress 1/3: 2 slots free"
                self.assertEqual(out.splitlines()[-1], f"{expected}; next: CHB-7")

    def test_a_ticket_that_depends_on_a_ticket_moved_to_done_or_discarded_is_next(self) -> None:
        """CHB-143: a move into Done or Discarded closes the ticket, even while the search still shows it open."""
        board = board_config(**BOARD_LIMITS)
        board["columnConfig"]["columns"].append({"name": "Discarded", "statuses": [{"id": "10008", "self": "x"}]})
        transitions = {"transitions": [*TRANSITIONS["transitions"], {"id": "99", "to": {"name": "Discarded", "id": "10008"}}]}
        for target in ["Done", "Discarded"]:
            with self.subTest(target):
                fake = transition_fake(
                    "Validating",
                    {"Validating": ["CHB-12"], "In Progress": ["CHB-1"], "To Do": ["CHB-88", "CHB-7"]},
                    board=board,
                    links={"CHB-88": blocked_by("CHB-12")},
                )
                fake.routes[("GET", "/rest/api/3/issue/CHB-12/transitions")] = transitions
                code, out, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines()[-1], "In Progress 1/3: 2 slots free; next: CHB-88, CHB-7")

    def test_a_ticket_blocked_by_an_open_ticket_is_not_next_and_by_a_done_one_is(self) -> None:
        """CHB-151 AC1 for the free-slot line."""
        fake = transition_fake(
            "In Progress",
            {"In Progress": ["CHB-1"], "In Review": ["CHB-91"], "To Do": ["CHB-50", "CHB-60", "CHB-7"]},
            links={"CHB-50": blocked_by("CHB-91"), "CHB-60": blocked_by("CHB-17", category="done")},
        )
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn("In Progress 1/3: 2 slots free; next: CHB-60, CHB-7", out.splitlines())

    def test_a_ticket_blocked_by_the_moved_ticket_is_not_next_until_it_closes(self) -> None:
        """CHB-143 with links: the moved ticket's own status decides, whatever the search or the link still shows."""
        board = board_config(**BOARD_LIMITS)
        board["columnConfig"]["columns"].append({"name": "Discarded", "statuses": [{"id": "10008", "self": "x"}]})
        transitions = {"transitions": [*TRANSITIONS["transitions"], {"id": "99", "to": {"name": "Discarded", "id": "10008"}}]}
        links = {"CHB-88": blocked_by("CHB-12")}
        cases = {"Validating": "CHB-7", "Done": "CHB-88, CHB-7", "Discarded": "CHB-88, CHB-7"}
        for target, following in cases.items():
            with self.subTest(target):
                current = "In Review" if target == "Validating" else "Validating"
                fake = transition_fake(
                    current,
                    {current: ["CHB-12"], "In Progress": ["CHB-1"], "To Do": ["CHB-88", "CHB-7"]},
                    board=board,
                    description=description_with("Acceptance criteria", *SECTIONS.values()),
                    links=links,
                )
                fake.routes[("GET", "/rest/api/3/issue/CHB-12/transitions")] = transitions
                code, out, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                self.assertTrue(out.splitlines()[-1].endswith(f"next: {following}"), out)

    def test_the_notice_and_the_start_check_share_one_top_queue(self) -> None:
        """CHB-143 AC2: both get the top tickets from top_queue, with the whole open search, so they cannot disagree."""
        fake = transition_fake("To Do", {"In Progress": ["CHB-1"], "To Do": ["CHB-12", "CHB-114", "CHB-7"]})
        with mock.patch.object(jira, "top_queue", wraps=jira.top_queue) as top_queue:
            code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(top_queue.call_count, 2)
        searched = [[issue["key"] for issue in call.args[0]] for call in top_queue.call_args_list]
        self.assertEqual(searched, [["CHB-1", "CHB-12", "CHB-114", "CHB-7"]] * 2)

    def test_nothing_when_in_progress_is_at_its_max(self) -> None:
        fake = transition_fake("To Do", {"In Progress": ["CHB-1", "CHB-2"], "To Do": ["CHB-12", "CHB-123"]})
        code, out, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-12 -> In Progress"])

    def test_nothing_when_a_full_column_blocks_starting(self) -> None:
        cases = {
            "In Review full, counting the moved ticket": ("In Progress", "In Review", {"In Review": ["CHB-2", "CHB-3"]}),
            "Validating full": ("In Review", "Validating", {"Validating": ["CHB-2", "CHB-3", "CHB-4"]}),
        }
        for name, (current, target, by_status) in cases.items():
            with self.subTest(name):
                fake = transition_fake(current, {**by_status, "To Do": ["CHB-123"]}, description=description_with(*SECTIONS.values()))
                code, out, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                self.assertNotIn("free", out)

    def test_nothing_without_an_in_progress_max(self) -> None:
        board = board_config(**{"In Review": {"max": 3}})
        fake = transition_fake("In Progress", {"To Do": ["CHB-123"]}, board=board)
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-12 -> In Review"])

    def test_a_failed_search_after_the_move_is_a_notice_not_a_failure(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-1"]})
        answer = fake.routes[("POST", SEARCH)]
        fake.routes[("POST", SEARCH)] = lambda body: 500 if "statusCategory" in body["jql"] else answer(body)
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn("could not check free In Progress slots", out)
        self.assertEqual(len(fake.writes()), 1)


class RequiredSectionTest(unittest.TestCase):
    """CHB-108: Validating and Needs human need their description section first (exit 4)."""

    def test_missing_section_blocks_and_says_how_to_write_it(self) -> None:
        for target, heading in SECTIONS.items():
            with self.subTest(target=target):
                fake = transition_fake("In Progress", {"In Progress": ["CHB-1"]}, description=description_with("Other"))
                code, _, err = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 4)
                self.assertIn(f"no section headed {heading!r}", err)
                self.assertIn(f'factory/bin/jira section CHB-12 --heading "{heading}" --body-file FILE', err)
                self.assertEqual(fake.writes(), [])

    def test_present_section_lets_the_move_through(self) -> None:
        for target, heading in SECTIONS.items():
            with self.subTest(target=target):
                fake = transition_fake("In Progress", {"In Progress": ["CHB-1"]}, description=description_with(heading))
                code, out, _ = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0)
                self.assertIn(f"CHB-12 -> {target}", out)

    def test_heading_must_match_exactly_and_have_text_under_it(self) -> None:
        cases = {
            "other case": description_with("acceptance criteria (validating)"),
            "in a comment-like paragraph": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Acceptance criteria (Validating)"}]}],
            },
            "empty section": description_with("Acceptance criteria (Validating)", body=""),
            "no description": None,
        }
        for name, description in cases.items():
            with self.subTest(name):
                fake = transition_fake("In Review", description=description)
                code, _, _ = _run(["transition", "CHB-12", "Validating"], fake)

                self.assertEqual(code, 4)

    def test_other_targets_need_no_section(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-1"]}, description=None)
        code, _, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)


class CommentTest(unittest.TestCase):
    def test_posts_the_file_as_a_document(self) -> None:
        fake = FakeJira({("POST", "/rest/api/3/issue/CHB-12/comment"): {"id": "100"}})
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as body:
            body.write("PR: https://github.com/x/y/pull/1\n")
            body.flush()
            code, out, _ = _run(["comment", "CHB-12", "--body-file", body.name], fake)

        self.assertEqual(code, 0)
        (_, _, sent), = fake.writes()
        self.assertEqual(adf_to_text(sent["body"]), "PR: https://github.com/x/y/pull/1")
        self.assertIn("100", out)

    def test_empty_body_is_a_usage_error_before_1password(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("w") as body:
            body.write(" \n")
            body.flush()
            code, _, err = _run(["comment", "CHB-12", "--body-file", body.name], fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("comment body is empty", err)
        self.assertEqual(fake.requests, [])

    def test_a_body_file_that_is_not_utf8_is_a_usage_error(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("wb") as body:
            body.write(b"caf\xe9 \xff\xfe")
            body.flush()
            code, _, err = _run(["comment", "CHB-12", "--body-file", body.name], fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("not valid UTF-8", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(fake.requests, [])

    def test_stdin_that_is_not_utf8_is_a_usage_error(self) -> None:
        fake = FakeJira({})
        stdin = io.TextIOWrapper(io.BytesIO(b"\xff\xfe"), encoding="utf-8")
        with mock.patch.object(sys, "stdin", stdin):
            code, _, err = _run(["comment", "CHB-12", "--body-file", "-"], fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("not valid UTF-8", err)
        self.assertEqual(fake.requests, [])


class AssignTest(unittest.TestCase):
    def test_me_assigns_to_the_token_owner(self) -> None:
        fake = FakeJira(
            {
                ("GET", "/rest/api/3/myself"): {"accountId": "claude-id"},
                ("PUT", "/rest/api/3/issue/CHB-12/assignee"): None,
            }
        )
        code, _, _ = _run(["assign", "CHB-12", "--me"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [("PUT", "/rest/api/3/issue/CHB-12/assignee", {"accountId": "claude-id"})])

    def test_by_account_id(self) -> None:
        fake = FakeJira({("PUT", "/rest/api/3/issue/CHB-12/assignee"): None})
        code, _, _ = _run(["assign", "CHB-12", "--account-id", "owner-id"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes()[0][2], {"accountId": "owner-id"})


def create_fake(parent_type: str = "Epic") -> FakeJira:
    """CHB-27 is the parent; the new ticket is CHB-200; To Do is empty, so create's sort ranks nothing."""
    return FakeJira(
        {
            ("GET", "/rest/api/3/issue/CHB-27?fields=issuetype"): {"fields": {"issuetype": {"name": parent_type}}},
            ("POST", "/rest/api/3/issue"): {"key": "CHB-200"},
            ("GET", BOARD): board_config(**BOARD_LIMITS),
            ("POST", SEARCH): keys_answer({"To Do": []}),
            ("PUT", RANK_PATH): None,
        }
    )


class CreateTest(unittest.TestCase):
    def _fake(self, parent_type: str) -> FakeJira:
        return create_fake(parent_type)

    def _create(self, fake: FakeJira, priority: tuple[str, ...] = ("--priority", "Medium")) -> tuple[int, str, str]:
        with tempfile.NamedTemporaryFile("w") as description:
            description.write(f"{WHY_SECTION}\n\nAC1: works.")
            description.flush()
            argv = ["create", "--parent", "CHB-27", "--summary", "Do it", "--description-file", description.name]
            return _run([*argv, *priority], fake)

    def test_creates_a_task_in_chb_under_the_epic(self) -> None:
        fake = self._fake("Epic")
        code, out, _ = self._create(fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-200")
        (_, _, sent), = fake.writes()
        fields = sent["fields"]
        self.assertEqual(fields["project"], {"key": "CHB"})
        self.assertEqual(fields["issuetype"], {"name": "Task"})
        self.assertEqual(fields["parent"], {"key": "CHB-27"})
        checklist = f"## Definition of Ready\n\n{jira.checklist_markdown()}"
        self.assertEqual(adf_to_text(fields["description"]), f"{WHY_SECTION}\n\nAC1: works.\n\n{checklist}")
        self.assertEqual(fields["priority"], {"name": "Medium"})

    def test_priority_is_required(self) -> None:
        fake = self._fake("Epic")
        code, _, _ = self._create(fake, priority=())

        self.assertEqual(code, 2)
        self.assertEqual(fake.requests, [])

    def test_only_high_medium_and_low_are_accepted(self) -> None:
        for value in ("Highest", "Lowest", "Critical", "high"):
            fake = self._fake("Epic")
            code, _, _ = self._create(fake, priority=("--priority", value))

            self.assertEqual(code, 2, value)
            self.assertEqual(fake.requests, [], value)

    def test_empty_summary_is_refused_before_any_request(self) -> None:
        fake = self._fake("Epic")
        with tempfile.NamedTemporaryFile("w") as description:
            argv = ["create", "--parent", "CHB-27", "--summary", "  ", "--description-file", description.name, "--priority", "Low"]
            code, _, err = _run(argv, fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("summary is empty", err)
        self.assertEqual(fake.requests, [])

    def test_a_description_file_that_is_not_utf8_is_a_usage_error(self) -> None:
        fake = self._fake("Epic")
        with tempfile.NamedTemporaryFile("wb") as description:
            description.write(b"\xff\xfe")
            description.flush()
            argv = ["create", "--parent", "CHB-27", "--summary", "S", "--description-file", description.name, "--priority", "Low"]
            code, _, err = _run(argv, fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("not valid UTF-8", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(fake.requests, [])

    def test_a_parent_outside_chb_is_refused_before_1password(self) -> None:
        fake = self._fake("Epic")
        with tempfile.NamedTemporaryFile("w") as description:
            description.write("Text.")
            description.flush()
            argv = ["create", "--parent", "OTHER-1", "--summary", "S", "--description-file", description.name, "--priority", "Low"]
            code, _, err = _run(argv, fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 1)
        self.assertIn("'OTHER-1' is not a CHB issue key", err)
        self.assertEqual(fake.requests, [])

    def test_parent_must_be_an_epic(self) -> None:
        fake = self._fake("Task")
        code, _, err = self._create(fake)

        self.assertEqual(code, 1)
        self.assertIn("not an Epic", err)
        self.assertEqual(fake.writes(), [])


class ScopeTest(unittest.TestCase):
    def test_only_chb_keys_are_accepted(self) -> None:
        for key in ("OTHER-1", "CHB-0", "CHB-1/../../project/CHB", "chb-1", "CHB-1\n"):
            fake = FakeJira({})
            code, _, _ = _run(["comment", key, "--body-file", "-"], fake)
            self.assertEqual(code, 1, key)
            self.assertEqual(fake.requests, [], key)

    def test_no_delete_or_raw_request_command(self) -> None:
        for command in ("delete", "request", "raw", "api"):
            code, _, _ = _run([command, "CHB-1"], FakeJira({}))
            self.assertEqual(code, 2, command)


class SearchTest(unittest.TestCase):
    def test_follows_next_page_tokens(self) -> None:
        issue = {"fields": {"summary": "S", "status": {"name": "Done"}, "assignee": None}}
        pages = [
            {"issues": [{"key": "CHB-1", **issue}], "nextPageToken": "p2", "isLast": False},
            {"issues": [{"key": "CHB-2", **issue}], "isLast": True},
        ]

        def opener(request: Any) -> FakeResponse:
            return FakeResponse(json.dumps(pages.pop(0)).encode())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["search", "project = CHB"], credentials_reader=lambda: CREDENTIALS, opener_factory=lambda: opener)

        self.assertEqual(code, 0)
        self.assertEqual([line.split("\t")[0] for line in out.getvalue().splitlines()], ["CHB-1", "CHB-2"])
        self.assertIn("Unassigned", out.getvalue())


class SearchScopeTest(unittest.TestCase):
    def _search(self, jql: str) -> tuple[int, list[str], str]:
        fake = FakeJira({("POST", SEARCH): {"issues": [], "isLast": True}})
        code, _, err = _run(["search", jql], fake)
        return code, fake.searches(), err

    def test_any_jql_is_wrapped_in_the_chb_project(self) -> None:
        cases = {
            "project = OTHER": "project = CHB AND (project = OTHER)",
            "status = Done OR project = OTHER": "project = CHB AND (status = Done OR project = OTHER)",
            'summary ~ "a) OR (b"': 'project = CHB AND (summary ~ "a) OR (b")',
            "summary ~ 'x\\'' ": "project = CHB AND (summary ~ 'x\\'')",
            "status = Done order by key ASC": "project = CHB AND (status = Done) order by key ASC",
            'summary ~ "order by" ORDER BY key': 'project = CHB AND (summary ~ "order by") ORDER BY key',
            "ORDER BY key DESC": "project = CHB ORDER BY key DESC",
            "  ": "project = CHB",
        }
        for jql, sent in cases.items():
            code, searches, _ = self._search(jql)
            self.assertEqual((code, searches), (0, [sent]), jql)

    def test_jql_that_escapes_the_parentheses_is_a_usage_error(self) -> None:
        cases = (
            "x) OR (project = OTHER",
            "x)",
            "(x",
            'summary ~ "open',
            "a ORDER BY key ) OR (project = OTHER",
            'a \\"b) OR (c"',
            "(a ORDER BY key) OR (project = OTHER)",
            "a ORDER BY key, (b) OR (project = OTHER)",
        )
        for jql in cases:
            code, searches, err = self._search(jql)
            self.assertEqual(code, 2, jql)
            self.assertEqual(searches, [], jql)
            self.assertNotIn("Traceback", err, jql)

    def test_an_issue_outside_chb_in_the_answer_is_refused(self) -> None:
        page = {"issues": [{"key": "OTHER-1", "fields": {}}], "isLast": True}
        code, out, err = _run_raw(["search", "status = Done", "--json"], json.dumps(page).encode())

        self.assertEqual(code, 1)
        self.assertNotIn("OTHER-1", out)
        self.assertIn("outside CHB", err)


class FailureTest(unittest.TestCase):
    def test_locked_1password_exits_3_and_tells_the_agent_to_stop(self) -> None:
        def locked() -> tuple[str, str]:
            raise CredentialError("op exited 1 without a token (is 1Password locked?)")

        fake = FakeJira({})
        code, _, err = _run(["view", "CHB-1"], fake, credentials_reader=locked)

        self.assertEqual(code, 3)
        self.assertIn("STOP", err)
        self.assertEqual(fake.requests, [])

    def test_http_error_exits_1_with_jira_messages_and_no_token(self) -> None:
        fake = FakeJira({("PUT", "/rest/api/3/issue/CHB-12/assignee"): 403})
        code, out, err = _run(["assign", "CHB-12", "--account-id", "x"], fake)

        self.assertEqual(code, 1)
        self.assertIn("HTTP 403 nope; f: bad", err)
        encoded = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        for secret in (TOKEN, encoded):
            self.assertNotIn(secret, out + err)


def _op_output(**values: str) -> str:
    return json.dumps([{"id": k, "label": k, "type": "STRING", "value": v} for k, v in values.items()])


class ReadCredentialsTest(unittest.TestCase):
    def test_email_and_token_come_from_one_read_of_the_item_in_the_factory_vault(self) -> None:
        commands: list[list[str]] = []

        def reader(command: list[str]) -> str:
            commands.append(command)
            return _op_output(username="agent@example.com", credential="tok")

        self.assertEqual(read_credentials(reader), ("agent@example.com", "tok"))
        self.assertEqual(
            commands,
            [
                ["op", "item", "get", "chb-jira-claude.api-token", "--vault", "chb-factory",
                 "--fields", "label=username,label=credential", "--reveal", "--format", "json"],
            ],
        )  # fmt: skip

    def test_fields_are_picked_by_label_not_position(self) -> None:
        output = json.dumps([{"label": "credential", "value": "tok"}, {"label": "username", "value": "a@b.c"}])
        self.assertEqual(read_credentials(lambda command: output), ("a@b.c", "tok"))

    def test_missing_or_empty_field_is_named_without_values(self) -> None:
        for output in (_op_output(username="a@b.c"), _op_output(username="a@b.c", credential=" ")):
            with self.assertRaisesRegex(CredentialError, "label=credential is missing or empty") as caught:
                read_credentials(lambda command, output=output: output)
            self.assertNotIn("a@b.c", str(caught.exception))

    def test_output_that_is_not_a_list_of_fields_is_a_credential_error(self) -> None:
        for output in ("not json", "42", "null"):
            with self.assertRaises(CredentialError):
                read_credentials(lambda command, output=output: output)


def _completed(returncode: int, stdout: bytes = b"") -> Any:
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> Any:
        calls.append({"command": command, **kwargs})
        return mock.Mock(returncode=returncode, stdout=stdout)

    run.calls = calls  # type: ignore[attr-defined]
    return run


class ReadSecretTest(unittest.TestCase):
    def test_op_gets_the_service_account_token_in_its_own_environment_only(self) -> None:
        run = _completed(0, b"out\n")

        with mock.patch.dict(jira.os.environ, {"PATH": "/bin"}, clear=True):
            self.assertEqual(read_secret(["op", "x"], token_reader=lambda: "ops_fake", run=run), "out\n")
            self.assertEqual(dict(jira.os.environ), {"PATH": "/bin"})
        (call,) = run.calls
        self.assertEqual(call["env"], {"PATH": "/bin", "OP_SERVICE_ACCOUNT_TOKEN": "ops_fake"})

    def test_no_token_in_the_keyring_is_a_credential_error_and_op_never_runs(self) -> None:
        def no_token() -> str:
            raise ServiceAccountError("no 1Password service account token in the keyring")

        run = _completed(0)
        with self.assertRaisesRegex(CredentialError, "keyring"):
            read_secret(["op"], token_reader=no_token, run=run)
        self.assertEqual(run.calls, [])

    def test_failing_op_is_a_credential_error(self) -> None:
        with self.assertRaisesRegex(CredentialError, "op exited 1"):
            read_secret(["op"], token_reader=lambda: "ops_fake", run=_completed(1))

    def test_missing_op_is_a_credential_error(self) -> None:
        with self.assertRaises(CredentialError):
            read_secret(["/nonexistent/op"], token_reader=lambda: "ops_fake")


def _run_raw(argv: list[str], body: bytes) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, credentials_reader=lambda: CREDENTIALS, opener_factory=lambda: lambda r: FakeResponse(body))
    return code, out.getvalue(), err.getvalue()


class JsonOutputTest(unittest.TestCase):
    def test_view_json_prints_the_issue_as_jira_returns_it(self) -> None:
        issue = {"key": "CHB-12", "fields": {"summary": "S"}}
        code, out, _ = _run_raw(["view", "CHB-12", "--json"], json.dumps(issue).encode())

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), issue)

    def test_search_json_prints_the_list_of_issues(self) -> None:
        page = {"issues": [{"key": "CHB-1", "fields": {}}], "isLast": True}
        code, out, _ = _run_raw(["search", "project = CHB", "--json"], json.dumps(page).encode())

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), page["issues"])


class ViewTest(unittest.TestCase):
    def test_prints_status_assignee_description_and_comments(self) -> None:
        doc = text_to_adf("The description.")
        issue = {
            "key": "CHB-12",
            "fields": {
                "summary": "Title",
                "issuetype": {"name": "Task"},
                "status": {"name": "In Review"},
                "priority": {"name": "High"},
                "assignee": {"displayName": "Claude (CHB agent)"},
                "parent": {"key": "CHB-27"},
                "description": doc,
                "comment": {"comments": [{"author": {"displayName": "Owner"}, "created": "t", "body": doc}]},
            },
        }
        path = "/rest/api/3/issue/CHB-12?fields=summary,status,priority,assignee,issuetype,parent,description,comment,issuelinks"
        code, out, _ = _run(["view", "CHB-12"], FakeJira({("GET", path): issue}))

        self.assertEqual(code, 0)
        for text in ("CHB-12 [Task] Title", "Status: In Review", "Priority: High", "Assignee: Claude (CHB agent)", "Parent: CHB-27", "--- Owner"):
            self.assertIn(text, out)
        self.assertEqual(out.count("The description."), 2)


class NetworkTest(unittest.TestCase):
    def test_timeout_is_a_jira_failure(self) -> None:
        def opener(request: Any) -> FakeResponse:
            raise TimeoutError("timed out")

        code = main(["assign", "CHB-1", "--account-id", "x"], credentials_reader=lambda: CREDENTIALS, opener_factory=lambda: opener)
        self.assertEqual(code, 1)

    def test_non_json_response_is_a_jira_failure(self) -> None:
        code = main(
            ["view", "CHB-1"], credentials_reader=lambda: CREDENTIALS, opener_factory=lambda: lambda r: FakeResponse(b"<html>")
        )
        self.assertEqual(code, 1)

    def test_json_that_is_not_an_object_is_a_jira_failure(self) -> None:
        for body in (b"[]", b"null", b"", b"42"):
            code, _, err = _run_raw(["search", "project = CHB"], body)
            self.assertEqual(code, 1, body)
            self.assertNotIn("Traceback", err, body)

    def test_http_error_with_a_non_object_body_still_exits_1(self) -> None:
        def opener(request: Any) -> FakeResponse:
            raise urllib.error.HTTPError(request.full_url, 500, "error", {}, io.BytesIO(b"[1]"))  # type: ignore[arg-type]

        code = main(["view", "CHB-1"], credentials_reader=lambda: CREDENTIALS, opener_factory=lambda: opener)
        self.assertEqual(code, 1)

    def test_redirects_are_refused(self) -> None:
        redirect = jira._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/")
        self.assertIsNone(redirect)

    def test_default_opener_does_not_follow_a_302_and_exits_1(self) -> None:
        target_hits: list[str] = []
        target = _local_server(lambda handler: (target_hits.append(handler.path), handler.send_response(200)))
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        target_url = f"http://127.0.0.1:{target.server_port}/stolen"

        def redirect(handler: http.server.BaseHTTPRequestHandler) -> None:
            handler.send_response(302)
            handler.send_header("Location", target_url)
            handler.send_header("Content-Length", "0")

        source = _local_server(redirect)
        self.addCleanup(source.server_close)
        self.addCleanup(source.shutdown)
        source_site = f"http://127.0.0.1:{source.server_port}"

        def opener_factory() -> Any:
            real = jira.default_opener()

            def open_(request: Any) -> Any:
                request.full_url = request.full_url.replace(jira.SITE, source_site, 1)
                return real(request)

            return open_

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["view", "CHB-1"], credentials_reader=lambda: CREDENTIALS, opener_factory=opener_factory)

        self.assertEqual(code, 1)
        self.assertIn("HTTP 302", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(target_hits, [])


def _local_server(answer: Any) -> http.server.ThreadingHTTPServer:
    """An HTTP server on a free local port. answer(handler) sends the status and headers of every GET."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
            answer(self)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class AdfTest(unittest.TestCase):
    def test_paragraphs_line_breaks_and_links(self) -> None:
        doc = text_to_adf("a\nb\n\nsee https://x.y/z now")
        self.assertEqual(text_to_adf("PR: https://x.y/1.")["content"][0]["content"][1]["text"], "https://x.y/1")

        first, second = doc["content"]
        self.assertEqual([n["type"] for n in first["content"]], ["text", "hardBreak", "text"])
        link = second["content"][1]
        self.assertEqual(link["marks"], [{"type": "link", "attrs": {"href": "https://x.y/z"}}])
        self.assertEqual(adf_to_text(doc), "a\nb\n\nsee https://x.y/z now")

RANK = ("PUT", "/rest/agile/1.0/issue/rank")


class RankTest(unittest.TestCase):
    def test_ranks_one_issue_before_another(self) -> None:
        fake = FakeJira({RANK: FakeResponse(b"", 204)})
        code, out, _ = _run(["rank", "CHB-5", "--before", "CHB-9"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [(*RANK, {"issues": ["CHB-5"], "rankBeforeIssue": "CHB-9"})])
        self.assertEqual(out.strip(), "CHB-5 ranked before CHB-9")

    def test_ranks_several_issues_in_order_after_another(self) -> None:
        fake = FakeJira({RANK: FakeResponse(b"", 204)})
        code, _, _ = _run(["rank", "CHB-5", "CHB-3", "--after", "CHB-9"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [(*RANK, {"issues": ["CHB-5", "CHB-3"], "rankAfterIssue": "CHB-9"})])

    def test_partial_failure_exits_1_with_the_issue_error(self) -> None:
        entries = {
            "entries": [
                {"issueId": 1, "issueKey": "CHB-5", "status": 200},
                {"issueId": 2, "issueKey": "CHB-3", "status": 400, "errors": ["Issue is not on the board"]},
            ]
        }
        fake = FakeJira({RANK: FakeResponse(json.dumps(entries).encode(), 207)})
        code, _, err = _run(["rank", "CHB-5", "CHB-3", "--before", "CHB-9"], fake)

        self.assertEqual(code, 1)
        self.assertIn("CHB-3: Issue is not on the board", err)
        self.assertNotIn("CHB-5:", err)

    def test_207_without_entries_still_fails(self) -> None:
        fake = FakeJira({RANK: FakeResponse(b"", 207)})
        code, _, err = _run(["rank", "CHB-5", "--before", "CHB-9"], fake)

        self.assertEqual(code, 1)
        self.assertIn("HTTP 207", err)

    def test_refuses_non_chb_keys_before_any_request(self) -> None:
        for argv in (
            ["rank", "OTHER-1", "--before", "CHB-9"],
            ["rank", "CHB-5", "--before", "OTHER-9"],
            ["rank", "CHB-5", "CHB-1/../x", "--after", "CHB-9"],
            ["rank", "CHB-5", "--after", "CHB-9\n"],
        ):
            fake = FakeJira({})
            code, _, _ = _run(argv, fake, credentials_reader=mock.Mock(side_effect=AssertionError("read credentials")))
            self.assertEqual(code, 1, argv)
            self.assertEqual(fake.requests, [], argv)

    def test_needs_exactly_one_anchor(self) -> None:
        for argv in (["rank", "CHB-5"], ["rank", "CHB-5", "--before", "CHB-9", "--after", "CHB-8"], ["rank", "--before", "CHB-9"]):
            self.assertEqual(_run(argv, FakeJira({}))[0], 2, argv)

    def test_refuses_an_issue_ranked_against_itself_listed_twice_or_more_than_50_as_usage_errors(self) -> None:
        too_many = [f"CHB-{n}" for n in range(1, 52)]
        for argv in (
            ["rank", "CHB-5", "--before", "CHB-5"],
            ["rank", "CHB-5", "CHB-5", "--before", "CHB-9"],
            ["rank", *too_many, "--before", "CHB-99"],
        ):
            fake = FakeJira({})
            code, _, _ = _run(argv, fake, credentials_reader=mock.Mock(side_effect=AssertionError("read credentials")))
            self.assertEqual(code, 2, argv[:4])
            self.assertEqual(fake.requests, [], argv[:4])

    def test_unexpected_207_body_shape_still_fails_cleanly(self) -> None:
        for body in (b'{"entries": "x"}', b'{"entries": [1, {"issueKey": "CHB-5", "errors": "bad"}]}'):
            fake = FakeJira({RANK: FakeResponse(body, 207)})
            code, _, err = _run(["rank", "CHB-5", "--before", "CHB-9"], fake)
            self.assertEqual(code, 1, body)
            self.assertNotIn("unexpected response", err, body)


HEADING = "Acceptance criteria (Validating)"
DESCRIPTION_PATH = "/rest/api/3/issue/CHB-12?fields=description"


def _heading(text: str, level: int = 2) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": text}]}


def _para(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _doc(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(nodes)}


class ReplaceSectionTest(unittest.TestCase):
    def test_appends_the_section_when_absent(self) -> None:
        before = _doc(_para("Intro."), _heading("Notes"), _para("A note."))
        after = replace_section(before, HEADING, "AC1 pending.")

        self.assertEqual(after["content"][:3], before["content"])
        self.assertEqual(after["content"][3:], [_heading(HEADING), text_to_adf("AC1 pending.")["content"][0]])

    def test_replaces_the_section_and_keeps_content_before_and_after(self) -> None:
        intro, rest = _para("Intro."), [_heading("Later"), _para("Kept.")]
        before = _doc(intro, _heading(HEADING), _para("Old AC."), _heading("Detail", 3), _para("Old detail."), *rest)
        after = replace_section(before, HEADING, "New AC.")

        self.assertEqual(after["content"], [intro, _heading(HEADING), _para("New AC."), *rest])
        self.assertEqual(adf_to_text(after).count(HEADING), 1)
        self.assertNotIn("Old", adf_to_text(after))

    def test_a_higher_level_heading_ends_the_section(self) -> None:
        before = _doc(_heading(HEADING, 3), _para("Old."), _heading("Top", 1), _para("Kept."))
        after = replace_section(before, HEADING, "New.")

        self.assertEqual(after["content"], [_heading(HEADING), _para("New."), _heading("Top", 1), _para("Kept.")])

    def test_setting_the_same_section_twice_leaves_one(self) -> None:
        once = replace_section(_doc(_para("Intro.")), HEADING, "First.")
        twice = replace_section(once, HEADING, "Second.")

        self.assertEqual(twice["content"], [_para("Intro."), _heading(HEADING), _para("Second.")])
        self.assertEqual(replace_section(twice, HEADING, "Second."), twice)

    def test_empty_or_missing_description_gets_only_the_section(self) -> None:
        for description in (None, _doc()):
            after = replace_section(description, HEADING, "Body.")
            self.assertEqual(after, _doc(_heading(HEADING), _para("Body.")), description)

    def test_unknown_node_types_and_attributes_are_kept_as_they_are(self) -> None:
        panel = {"type": "panel", "attrs": {"panelType": "info"}, "content": [_para("Careful.")]}
        before = {"type": "doc", "version": 1, "content": [panel, _heading(HEADING), _para("Old.")]}
        after = replace_section(before, HEADING, "New.")

        self.assertIs(after["content"][0], panel)
        self.assertEqual(before["content"][2], _para("Old."), "the input is not changed")

    def test_dash_lines_become_a_bullet_list(self) -> None:
        after = replace_section(None, HEADING, "AC1. Works.\n- When: Monday\n- Where: https://x.y/z\nAfter list.")
        _, paragraph, bullets, tail = after["content"]

        self.assertEqual(paragraph, _para("AC1. Works."))
        self.assertEqual(bullets["type"], "bulletList")
        self.assertEqual([adf_to_text(item).strip() for item in bullets["content"]], ["When: Monday", "Where: https://x.y/z"])
        self.assertEqual(bullets["content"][1]["content"][0]["content"][1]["marks"][0]["attrs"]["href"], "https://x.y/z")
        self.assertEqual(tail, _para("After list."))
        self.assertIn("- When: Monday\n- Where: https://x.y/z\n", adf_to_text(after))

    def test_refuses_an_ambiguous_section_an_empty_body_or_a_bad_heading(self) -> None:
        twice = _doc(_heading(HEADING), _para("a"), _heading(HEADING), _para("b"))
        with self.assertRaises(JiraError):
            replace_section(twice, HEADING, "x")
        for heading, body in ((HEADING, "  \n"), (" ", "x"), ("a\nb", "x")):
            with self.assertRaises(UsageError):
                replace_section(None, heading, body)


class SectionCommandTest(unittest.TestCase):
    def _section(self, fake: FakeJira, body: str = "New AC.", key: str = "CHB-12") -> tuple[int, str, str]:
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write(body)
            handle.flush()
            return _run(["section", key, "--heading", HEADING, "--body-file", handle.name], fake)

    def test_writes_only_the_description_field(self) -> None:
        current = _doc(_para("Intro."), _heading(HEADING), _para("Old."))
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": current}}, ("PUT", "/rest/api/3/issue/CHB-12"): None})
        code, out, _ = self._section(fake)

        self.assertEqual(code, 0)
        (_, path, sent), = fake.writes()
        self.assertEqual(path, "/rest/api/3/issue/CHB-12")
        self.assertEqual(sent, {"fields": {"description": _doc(_para("Intro."), _heading(HEADING), _para("New AC."))}})
        self.assertIn("set", out)

    def test_no_write_when_the_section_already_has_that_body(self) -> None:
        current = _doc(_heading(HEADING), _para("New AC."))
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": current}}})
        code, out, _ = self._section(fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("unchanged", out)

    def test_empty_description_from_jira(self) -> None:
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": None}}, ("PUT", "/rest/api/3/issue/CHB-12"): None})
        self.assertEqual(self._section(fake)[0], 0)
        self.assertEqual(fake.writes()[0][2]["fields"]["description"], _doc(_heading(HEADING), _para("New AC.")))

    def test_refuses_non_chb_keys_before_any_request(self) -> None:
        fake = FakeJira({})
        self.assertEqual(self._section(fake, key="OTHER-12")[0], 1)
        self.assertEqual(fake.requests, [])

    def test_empty_body_is_a_usage_error_before_credentials_or_requests(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("w") as handle:
            argv = ["section", "CHB-12", "--heading", HEADING, "--body-file", handle.name]
            code, _, err = _run(argv, fake, credentials_reader=mock.Mock(side_effect=AssertionError("read credentials")))

        self.assertEqual(code, 2)
        self.assertIn("body is empty", err)
        self.assertEqual(fake.requests, [])

    def test_a_when_without_speed_up_is_a_usage_error_before_credentials_or_requests(self) -> None:
        fake = FakeJira({})
        bodies = (
            "AC1. Works.\n- When: the next Dependabot run\n- Where: https://x.y/z",
            "AC1.\n- When: a\n- Speed up: run it\nAC2.\n- **When**: b\n- Where: c",
        )
        for body in bodies:
            with tempfile.NamedTemporaryFile("w") as handle:
                handle.write(body)
                handle.flush()
                argv = ["section", "CHB-12", "--heading", HEADING, "--body-file", handle.name]
                code, _, err = _run(argv, fake, credentials_reader=mock.Mock(side_effect=AssertionError("read credentials")))

            self.assertEqual(code, 2, body)
            self.assertIn("Speed up:", err)
            self.assertEqual(fake.requests, [])

    def test_each_when_with_a_speed_up_line_is_accepted(self) -> None:
        bodies = (
            "AC1.\n- When: the next Dependabot run\n- Speed up: Insights, Dependency graph, Check for updates\n"
            "AC2.\n1. when: 2026-11-12 06:00\n2. **Speed up:** cannot be sped up (a fixed date)",
            "AC1. No timing line here.",
        )
        for body in bodies:
            fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": None}}, ("PUT", "/rest/api/3/issue/CHB-12"): None})
            self.assertEqual(self._section(fake, body)[0], 0, body)

    def test_other_headings_need_no_speed_up(self) -> None:
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": None}}, ("PUT", "/rest/api/3/issue/CHB-12"): None})
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("1. Click Merge.\n- When: today")
            handle.flush()
            code, _, _ = _run(["section", "CHB-12", "--heading", "Needs human: owner steps", "--body-file", handle.name], fake)

        self.assertEqual(code, 0)

    def test_two_matching_headings_exit_1_without_a_write(self) -> None:
        current = _doc(_heading(HEADING), _para("a"), _heading(HEADING), _para("b"))
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": current}}})
        code, _, err = self._section(fake)

        self.assertEqual(code, 1)
        self.assertIn("2 sections", err)
        self.assertEqual(fake.writes(), [])


class PriorityTest(unittest.TestCase):
    def test_sets_the_priority_of_an_issue(self) -> None:
        fake = FakeJira({("PUT", "/rest/api/3/issue/CHB-12"): None})
        code, out, _ = _run(["priority", "CHB-12", "High"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-12 priority High")
        self.assertEqual(fake.writes(), [("PUT", "/rest/api/3/issue/CHB-12", {"fields": {"priority": {"name": "High"}}})])

    def test_refuses_any_other_value_before_any_request(self) -> None:
        for value in ("Highest", "Lowest", "Urgent", ""):
            fake = FakeJira({})
            code, _, _ = _run(["priority", "CHB-12", value], fake)

            self.assertEqual(code, 2, value)
            self.assertEqual(fake.requests, [], value)

    def test_only_chb_keys(self) -> None:
        fake = FakeJira({})
        self.assertEqual(_run(["priority", "OTHER-1", "Low"], fake)[0], 1)
        self.assertEqual(fake.requests, [])


class MarkdownHeadingTest(unittest.TestCase):
    """A description written with "## Heading" lines gets real headings, so `section` finds them."""

    def test_markdown_heading_lines_become_headings(self) -> None:
        doc = text_to_adf("## Goal\n\nDo it.\n\n### Detail\nMore.")

        self.assertEqual(doc["content"][0], _heading("Goal"))
        self.assertEqual(doc["content"][1], _para("Do it."))
        self.assertEqual(doc["content"][2], _heading("Detail", 3))
        self.assertEqual(doc["content"][3], _para("More."))

    def test_a_hash_without_a_space_stays_text(self) -> None:
        self.assertEqual(text_to_adf("#123 is an issue")["content"], [_para("#123 is an issue")])

    def test_section_replaces_a_section_written_at_creation(self) -> None:
        created = text_to_adf("## Goal\n\nG.\n\n## Work\n\nOld step.\n\n## Acceptance criteria\n\nAC.")
        after = replace_section(created, "Work", "New step.")

        text = adf_to_text(after)
        self.assertEqual(text.count("Work"), 1)
        self.assertNotIn("Old step.", text)
        self.assertLess(text.index("New step."), text.index("Acceptance criteria"))


def _text(text: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


CODE = {"type": "code"}
STRONG = {"type": "strong"}


class MarkdownTest(unittest.TestCase):
    """Descriptions, comments and sections are written in Markdown and must render in Jira (CHB-129)."""

    def test_bullet_lines_become_a_bullet_list_everywhere(self) -> None:
        bullets, = text_to_adf("- one\n- two")["content"]

        self.assertEqual(bullets["type"], "bulletList")
        self.assertEqual([adf_to_text(item).strip() for item in bullets["content"]], ["one", "two"])

    def test_numbered_lines_become_an_ordered_list_with_continuation_lines(self) -> None:
        listed, = text_to_adf("1. First.\n   More of first.\n2. Second.")["content"]

        self.assertEqual((listed["type"], listed["attrs"]), ("orderedList", {"order": 1}))
        first, second = listed["content"]
        self.assertEqual(first["content"][0]["content"], [_text("First."), {"type": "hardBreak"}, _text("More of first.")])
        self.assertEqual(adf_to_text(second).strip(), "Second.")

    def test_a_list_split_by_blank_lines_keeps_its_numbers(self) -> None:
        _, second = text_to_adf("1. a\n\n2. b")["content"]

        self.assertEqual(second["attrs"], {"order": 2})

    def test_backticks_become_code_and_double_stars_bold(self) -> None:
        paragraph, = text_to_adf("Run `make check` **now**.")["content"]

        self.assertEqual(paragraph["content"], [_text("Run "), _text("make check", CODE), _text(" "), _text("now", STRONG), _text(".")])

    def test_a_url_inside_code_is_not_a_link(self) -> None:
        paragraph, = text_to_adf("`https://x.y/z`")["content"]

        self.assertEqual(paragraph["content"], [_text("https://x.y/z", CODE)])

    def test_plain_text_view_shows_the_lists_back(self) -> None:
        doc = text_to_adf("## Work\n\n1. a\n2. b\n\n- c")

        self.assertEqual(adf_to_text(doc), "## Work\n\n1. a\n2. b\n\n- c")

    def test_markdown_survives_a_round_trip(self) -> None:
        text = "## Goal\n\nRun `make check` for **CHB-1** (#42).\nNext line.\n\n1. a\n   more of a\n2. b\n\n- c\n  more of c\n- d"

        self.assertEqual(text_to_adf(adf_to_text(text_to_adf(text))), text_to_adf(text))


STATUS_READ = ("GET", "/rest/api/3/issue/CHB-12?fields=status")
ME = ("GET", "/rest/api/3/myself")
COMMENT = "/rest/api/3/issue/CHB-12/comment/10071"


def _status(name: str) -> dict[str, Any]:
    return {"fields": {"status": {"name": name}}}


class DescriptionTest(unittest.TestCase):
    def _write(self, fake: FakeJira, text: str) -> tuple[int, str, str]:
        with tempfile.NamedTemporaryFile("w") as body:
            body.write(text)
            body.flush()
            return _run(["description", "CHB-12", "--file", body.name], fake)

    def test_refuses_to_drop_the_section_the_status_requires(self) -> None:
        for status, heading in SECTIONS.items():
            fake = FakeJira({STATUS_READ: _status(status)})
            code, _, err = self._write(fake, "## Goal\n\nNew.")

            self.assertEqual(code, 4, status)
            self.assertIn(heading, err)
            self.assertEqual(fake.writes(), [], status)

    def test_keeps_a_required_section_when_the_text_has_it(self) -> None:
        heading = SECTIONS["Validating"]
        fake = FakeJira({STATUS_READ: _status("Validating"), ("PUT", "/rest/api/3/issue/CHB-12"): None})
        code, _, _ = self._write(fake, f"## Goal\n\nNew.\n\n## {heading}\n\nAC1 checked.")

        self.assertEqual(code, 0)
        self.assertEqual(len(fake.writes()), 1)

    def test_replaces_the_whole_description(self) -> None:
        fake = FakeJira({STATUS_READ: _status("To Do"), ("PUT", "/rest/api/3/issue/CHB-12"): None})
        with tempfile.NamedTemporaryFile("w") as body:
            body.write("## Goal\n\nNew.")
            body.flush()
            code, out, _ = _run(["description", "CHB-12", "--file", body.name], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-12 description set")
        (method, path, sent), = fake.writes()
        self.assertEqual((method, path, list(sent["fields"])), ("PUT", "/rest/api/3/issue/CHB-12", ["description"]))
        self.assertEqual(sent["fields"]["description"]["content"][0], _heading("Goal"))

    def test_an_empty_file_is_refused_before_any_request(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("w") as body:
            code, _, _ = _run(["description", "CHB-12", "--file", body.name], fake)

        self.assertEqual(code, 2)
        self.assertEqual(fake.requests, [])


BROWSE = "https://islomar.atlassian.net/browse/"
PULL = "https://github.com/islomar/corporate-hotel-booking-software-factory/pull/"


def _link(text: str, href: str) -> dict[str, Any]:
    return _text(text, {"type": "link", "attrs": {"href": href}})


class ReferenceLinkTest(unittest.TestCase):
    """Ticket keys and PR numbers in Jira text are links (CHB-129)."""

    def test_ticket_keys_link_to_jira(self) -> None:
        paragraph, = text_to_adf("See CHB-122, then CHB-7.")["content"]

        self.assertEqual(
            paragraph["content"],
            [_text("See "), _link("CHB-122", BROWSE + "CHB-122"), _text(", then "), _link("CHB-7", BROWSE + "CHB-7"), _text(".")],
        )

    def test_pr_numbers_link_to_github(self) -> None:
        paragraph, = text_to_adf("PR #44 merged (#42, main 5e51f6a).")["content"]

        self.assertEqual(
            paragraph["content"],
            [_link("PR #44", PULL + "44"), _text(" merged ("), _link("#42", PULL + "42"), _text(", main 5e51f6a).")],
        )

    def test_keys_in_lists_and_headings_link_too(self) -> None:
        heading, bullets = text_to_adf("## CHB-1\n\n- CHB-2 done")["content"]

        self.assertEqual(heading["content"], [_link("CHB-1", BROWSE + "CHB-1")])
        self.assertEqual(bullets["content"][0]["content"][0]["content"][0], _link("CHB-2", BROWSE + "CHB-2"))

    def test_lookalikes_stay_text(self) -> None:
        for text in ("XCHB-12", "CHB-0", "issue #12 here", "CHB-12a", "`CHB-12`"):
            paragraph, = text_to_adf(text)["content"]
            links = [n for n in paragraph["content"] if any(m["type"] == "link" for m in n.get("marks", []))]
            self.assertEqual(links, [], text)

    def test_a_key_inside_a_url_keeps_the_url_link(self) -> None:
        url = "https://islomar.atlassian.net/browse/CHB-9"
        paragraph, = text_to_adf(url)["content"]

        self.assertEqual(paragraph["content"], [_link(url, url)])


class EditCommentTest(unittest.TestCase):
    def test_refuses_a_comment_written_by_someone_else(self) -> None:
        fake = FakeJira({ME: {"accountId": "agent"}, ("GET", COMMENT): {"id": "10071", "author": {"accountId": "owner"}}})
        with tempfile.NamedTemporaryFile("w") as body:
            body.write("x")
            body.flush()
            code, _, err = _run(["edit-comment", "CHB-12", "10071", "--body-file", body.name], fake)

        self.assertEqual(code, 1)
        self.assertIn("not written by the agent", err)
        self.assertEqual(fake.writes(), [])

    def test_replaces_the_body_of_a_comment(self) -> None:
        fake = FakeJira(
            {
                ME: {"accountId": "agent"},
                ("GET", COMMENT): {"id": "10071", "author": {"accountId": "agent"}},
                ("PUT", COMMENT): {"id": "10071"},
            }
        )
        with tempfile.NamedTemporaryFile("w") as body:
            body.write("See CHB-1.")
            body.flush()
            code, out, _ = _run(["edit-comment", "CHB-12", "10071", "--body-file", body.name], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-12 comment 10071 updated")
        (_, _, sent), = fake.writes()
        self.assertEqual(sent["body"]["content"][0]["content"][1], _link("CHB-1", BROWSE + "CHB-1"))

    def test_comment_id_must_be_a_number(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("w") as body:
            body.write("x")
            body.flush()
            code, _, _ = _run(["edit-comment", "CHB-12", "../worklog", "--body-file", body.name], fake)

        self.assertEqual(code, 2)
        self.assertEqual(fake.requests, [])

    def test_empty_body_is_a_usage_error_before_1password(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("w") as body:
            body.write("\n")
            body.flush()
            argv = ["edit-comment", "CHB-12", "10071", "--body-file", body.name]
            code, _, err = _run(argv, fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("comment body is empty", err)
        self.assertEqual(fake.requests, [])


LINKS_OF_88 = "/rest/api/3/issue/CHB-88?fields=issuelinks"
ISSUE_LINK = "/rest/api/3/issueLink"


def link_fake(before: list[dict[str, Any]], after: list[dict[str, Any]] | None = None) -> FakeJira:
    """CHB-88 has the links `before` until the link POST, then `after` (before plus the new link by default)."""
    after = [*before, *blocked_by("CHB-146")] if after is None else after
    fake = FakeJira({("POST", ISSUE_LINK): FakeResponse(b"", status=201)})
    fake.routes[("GET", LINKS_OF_88)] = lambda _: {"fields": {"issuelinks": after if fake.writes() else before}}
    return fake


class LinkTest(unittest.TestCase):
    """CHB-151: `link CHB-<a> --blocked-by CHB-<b>` creates a "Blocks" link: CHB-<b> blocks CHB-<a>."""

    def test_creates_a_blocks_link_with_the_blocker_as_the_inward_issue_of_the_post(self) -> None:
        """Checked live (CHB-151): a POST with inwardIssue=CHB-114, outwardIssue=CHB-17 made CHB-114 block CHB-17."""
        fake = link_fake([])
        code, out, _ = _run(["link", "CHB-88", "--blocked-by", "CHB-146"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-88 is blocked by CHB-146: link created")
        body = {"type": {"name": "Blocks"}, "inwardIssue": {"key": "CHB-146"}, "outwardIssue": {"key": "CHB-88"}}
        self.assertEqual(fake.writes(), [("POST", ISSUE_LINK, body)])

    def test_an_existing_link_is_not_duplicated(self) -> None:
        fake = link_fake(blocked_by("CHB-146"))
        code, out, _ = _run(["link", "CHB-88", "--blocked-by", "CHB-146"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-88 is blocked by CHB-146: link unchanged")
        self.assertEqual(fake.writes(), [])

    def test_the_reverse_link_or_another_type_does_not_count_as_existing(self) -> None:
        relates = [{"id": "1", "type": RELATES_TYPE, "inwardIssue": {"key": "CHB-146"}}]
        for name, before in {"reverse": blocks("CHB-146"), "relates": relates}.items():
            with self.subTest(name):
                fake = link_fake(before)
                code, _, _ = _run(["link", "CHB-88", "--blocked-by", "CHB-146"], fake)

                self.assertEqual(code, 0)
                self.assertEqual(len(fake.writes()), 1)

    def test_a_link_that_does_not_show_after_the_post_exits_1(self) -> None:
        """A link Jira stored the other way round would block the wrong ticket: say so instead of reporting success."""
        fake = link_fake([], after=blocks("CHB-146"))
        code, _, err = _run(["link", "CHB-88", "--blocked-by", "CHB-146"], fake)

        self.assertEqual(code, 1)
        self.assertIn("CHB-88 does not show CHB-146 as a blocker after the link", err)

    def test_refuses_non_chb_keys_and_self_links_before_any_request(self) -> None:
        """CHB-151 AC2: exit 2, no 1Password read, no request."""
        cases = {
            "other project, blocked": ["link", "OPS-1", "--blocked-by", "CHB-146"],
            "other project, blocker": ["link", "CHB-88", "--blocked-by", "OPS-1"],
            "lower case": ["link", "CHB-88", "--blocked-by", "chb-146"],
            "self": ["link", "CHB-88", "--blocked-by", "CHB-88"],
            "missing blocker": ["link", "CHB-88"],
        }
        for name, argv in cases.items():
            with self.subTest(name):
                fake = FakeJira({})
                code, _, err = _run(argv, fake, credentials_reader=_no_credentials)

                self.assertEqual(code, 2)
                self.assertEqual(fake.requests, [])
                if name == "self":
                    self.assertIn("CHB-88 cannot be blocked by itself", err)

    def test_a_refused_post_exits_1(self) -> None:
        fake = link_fake([])
        fake.routes[("POST", ISSUE_LINK)] = 404
        code, _, err = _run(["link", "CHB-88", "--blocked-by", "CHB-146"], fake)

        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", err)


class ViewLinksTest(unittest.TestCase):
    def test_view_lists_blocked_by_and_blocks(self) -> None:
        """CHB-151 AC3: both tickets of a link show it in `view`."""
        status = {"fields": {"status": {"name": "To Do"}}}
        links = [
            {"id": "1", "type": BLOCKS_TYPE, "inwardIssue": {"key": "CHB-146", **status}},
            {"id": "2", "type": BLOCKS_TYPE, "outwardIssue": {"key": "CHB-49", **status}},
            {"id": "3", "type": RELATES_TYPE, "outwardIssue": {"key": "CHB-5", **status}},
        ]
        issue = {
            "key": "CHB-88",
            "fields": {"summary": "T", "issuetype": {"name": "Task"}, "status": {"name": "To Do"}, "issuelinks": links},
        }
        path = "/rest/api/3/issue/CHB-88?fields=summary,status,priority,assignee,issuetype,parent,description,comment,issuelinks"
        code, out, _ = _run(["view", "CHB-88"], FakeJira({("GET", path): issue}))

        self.assertEqual(code, 0)
        self.assertIn("Is blocked by: CHB-146 (To Do)", out.splitlines())
        self.assertIn("Blocks: CHB-49 (To Do)", out.splitlines())
        self.assertIn("Relates to: CHB-5 (To Do)", out.splitlines())
WHY = "Why it matters"
CREATE_PATH = "/rest/api/3/issue"
MOVE_PATH = "/rest/api/3/issue/CHB-12/transitions"


def _create_with(
    description: str, fake: FakeJira | None = None, credentials_reader: Any = lambda: CREDENTIALS, priority: str = "Medium"
) -> tuple[int, str, str, FakeJira]:
    fake = fake or create_fake()
    with tempfile.NamedTemporaryFile("w") as handle:
        handle.write(description)
        handle.flush()
        argv = ["create", "--parent", "CHB-27", "--summary", "Do it", "--description-file", handle.name, "--priority", priority]
        code, out, err = _run(argv, fake, credentials_reader=credentials_reader)
    return code, out, err, fake


def _write_paths(fake: FakeJira) -> list[str]:
    return [path for _, path, _ in fake.writes()]


class WhyItMattersTest(unittest.TestCase):
    """CHB-148: every ticket opens with a non-empty "Why it matters" section."""

    def test_why_problem(self) -> None:
        cases = {
            "first, with text": (f"## {WHY}\n\nThe owner waits.\n\n## Plan\n\nDo it.", None),
            "level 3 heading": (f"### {WHY}\n\nThe owner waits.", None),
            "a list as its text": (f"## {WHY}\n\n- The owner waits.", None),
            "no description": ("", "does not start with"),
            "intro before it": (f"Intro.\n\n## {WHY}\n\nThe owner waits.", "does not start with"),
            "another heading first": (f"## Plan\n\nDo it.\n\n## {WHY}\n\nThe owner waits.", "does not start with"),
            "wrong case": ("## Why It Matters\n\nThe owner waits.", "does not start with"),
            "empty": (f"## {WHY}\n\n## Plan\n\nDo it.", "is empty"),
            "empty at the end": (f"## {WHY}", "is empty"),
            "a subheading is not text": (f"## {WHY}\n\n### Detail\n\n## Plan\n\nDo it.", "is empty"),
        }
        for name, (markdown, problem) in cases.items():
            with self.subTest(name):
                found = jira.why_problem(text_to_adf(markdown) if markdown else None)
                if problem is None:
                    self.assertIsNone(found)
                else:
                    self.assertIn(problem, found or "")

    def test_create_without_the_section_exits_2_before_any_request(self) -> None:
        for name, description in {
            "missing": "Line one.\n\nAC1: works.",
            "not first": f"Intro.\n\n## {WHY}\n\nThe owner waits.",
            "empty": f"## {WHY}\n\n## Acceptance criteria\n\n- AC1.",
        }.items():
            with self.subTest(name):
                code, out, err, fake = _create_with(description, credentials_reader=_no_credentials)

                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(WHY, err)
                self.assertEqual(fake.requests, [])

    def test_create_with_the_section_creates(self) -> None:
        code, out, _, fake = _create_with(f"## {WHY}\n\nThe owner waits.\n\n## Acceptance criteria\n\n- AC1.")

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-200")
        self.assertIn(CREATE_PATH, _write_paths(fake))

    def test_a_start_without_the_section_exits_4_with_the_command_to_add_it(self) -> None:
        for name, description in {
            "missing": doc("Do it.\n\n## Acceptance criteria\n\n- A test."),
            "empty": doc(f"## {WHY}\n\n## Acceptance criteria\n\n- A test."),
        }.items():
            with self.subTest(name):
                fake = transition_fake("To Do", {"To Do": ["CHB-12", "CHB-5"]}, description=description)
                code, out, err = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 4)
                self.assertEqual(out, "")
                self.assertIn(f'factory/bin/jira section CHB-12 --heading "{WHY}" --first --body-file FILE', err)
                self.assertNotIn(MOVE_PATH, _write_paths(fake))

    def test_the_top_ticket_rule_comes_before_the_why_rule(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-5", "CHB-12"]}, description=doc("Do it."))
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("CHB-12 is not the top ticket of To Do: CHB-5 is", err)

    def test_moving_back_into_in_progress_needs_no_why_section(self) -> None:
        fake = transition_fake("In Review", {"To Do": ["CHB-5"]}, description=doc("Do it."))
        code, _, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)


def _apply_ranks(order: list[str], fake: FakeJira) -> list[str]:
    """The To Do order after the rank requests the fake received, applied like Jira does."""
    order = list(order)
    for body in _rank_writes(fake):
        keys = body["issues"]
        order = [key for key in order if key not in keys]
        if "rankBeforeIssue" in body:
            at = order.index(body["rankBeforeIssue"])
        else:
            at = order.index(body["rankAfterIssue"]) + 1
        order[at:at] = keys
    return order


def _rank_writes(fake: FakeJira) -> list[Any]:
    return [body for method, path, body in fake.writes() if (method, path) == ("PUT", RANK_PATH)]


def sort_fake(to_do: list[str], by_status: dict[str, list[str]] | None = None, state: str = "before", **kwargs: Any) -> FakeJira:
    return FakeJira(
        {
            ("GET", BOARD): board_config(state=state, **BOARD_LIMITS),
            ("POST", SEARCH): keys_answer({pull_name(state): to_do, **(by_status or {})}, **kwargs),
            ("PUT", RANK_PATH): None,
        }
    )


class SortTodoTest(unittest.TestCase):
    """CHB-148: To Do holds ready tickets High, Medium, Low, then blocked tickets, then epics."""

    def test_ready_by_priority_then_blocked_then_epics_keeping_relative_order(self) -> None:
        to_do = ["CHB-1", "CHB-2", "CHB-3", "CHB-4", "CHB-5", "CHB-6", "CHB-7", "CHB-8", "CHB-9"]
        fake = sort_fake(
            to_do,
            by_status={"In Review": ["CHB-90"]},
            links={"CHB-1": blocked_by("CHB-90"), "CHB-6": blocked_by("CHB-90")},
            priorities={"CHB-1": "High", "CHB-2": "Low", "CHB-3": "High", "CHB-4": "High", "CHB-5": "Low", "CHB-8": "High"},
            epics=("CHB-3",),
        )
        code, out, _ = _run(["sort-todo"], fake)

        self.assertEqual(code, 0)
        expected = ["CHB-4", "CHB-8", "CHB-7", "CHB-9", "CHB-2", "CHB-5", "CHB-1", "CHB-6", "CHB-3"]
        self.assertEqual(_apply_ranks(to_do, fake), expected)
        self.assertIn(", ".join(expected), out)

    def test_a_missing_priority_sorts_as_medium(self) -> None:
        to_do = ["CHB-1", "CHB-2", "CHB-3"]
        fake = sort_fake(to_do, priorities={"CHB-1": "Low", "CHB-2": None, "CHB-3": "High"})
        self.assertEqual(_run(["sort-todo"], fake)[0], 0)

        self.assertEqual(_apply_ranks(to_do, fake), ["CHB-3", "CHB-2", "CHB-1"])

    def test_already_sorted_is_a_no_op(self) -> None:
        fake = sort_fake(["CHB-1", "CHB-2", "CHB-3"], priorities={"CHB-1": "High", "CHB-3": "Low"})
        code, out, _ = _run(["sort-todo"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("already sorted", out)

    def test_only_tickets_from_the_first_misplaced_one_are_ranked(self) -> None:
        to_do = ["CHB-1", "CHB-2", "CHB-3", "CHB-4"]
        fake = sort_fake(to_do, priorities={"CHB-1": "High", "CHB-4": "High"})
        _run(["sort-todo"], fake)

        self.assertEqual(_rank_writes(fake), [{"issues": ["CHB-4", "CHB-2", "CHB-3"], "rankAfterIssue": "CHB-1"}])
        self.assertEqual(_apply_ranks(to_do, fake), ["CHB-1", "CHB-4", "CHB-2", "CHB-3"])

    def test_a_new_top_ticket_goes_before_the_old_one(self) -> None:
        to_do = ["CHB-1", "CHB-2", "CHB-3"]
        fake = sort_fake(to_do, priorities={"CHB-3": "High"})
        _run(["sort-todo"], fake)

        self.assertEqual(_rank_writes(fake), [{"issues": ["CHB-3"], "rankBeforeIssue": "CHB-1"}])
        self.assertEqual(_apply_ranks(to_do, fake), ["CHB-3", "CHB-1", "CHB-2"])

    def test_more_than_50_tickets_are_ranked_in_chunks_of_50(self) -> None:
        low, medium = [f"CHB-{n}" for n in range(1, 61)], [f"CHB-{n}" for n in range(61, 122)]
        fake = sort_fake([*low, *medium], priorities=dict.fromkeys(low, "Low"))
        _run(["sort-todo"], fake)

        sizes = [len(body["issues"]) for body in _rank_writes(fake)]
        self.assertEqual(sizes, [1, 50, 50, 20])
        self.assertEqual(_apply_ranks([*low, *medium], fake), [*medium, *low])

    def test_tickets_outside_to_do_are_not_ranked(self) -> None:
        fake = sort_fake(["CHB-1", "CHB-2"], by_status={"In Progress": ["CHB-7"]}, priorities={"CHB-2": "High", "CHB-7": "High"})
        _run(["sort-todo"], fake)

        touched = {key for body in _rank_writes(fake) for key in [*body["issues"], body.get("rankBeforeIssue"), body.get("rankAfterIssue")]}
        self.assertEqual(touched - {None}, {"CHB-1", "CHB-2"})

    def test_a_failed_rank_exits_1(self) -> None:
        fake = sort_fake(["CHB-1", "CHB-2"], priorities={"CHB-2": "High"})
        fake.routes[("PUT", RANK_PATH)] = 500
        code, _, err = _run(["sort-todo"], fake)

        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", err)

    def test_ready_means_in_top_queue(self) -> None:
        """The dependency rule lives in top_queue only, so CHB-151 changes it in one place."""
        fake = sort_fake(["CHB-1", "CHB-2"], priorities={"CHB-2": "High"})
        with mock.patch.object(jira, "top_queue", return_value=["CHB-1"]) as top_queue:
            _run(["sort-todo"], fake)

        top_queue.assert_called_once()
        self.assertEqual(fake.writes(), [], "top_queue says CHB-2 is blocked, so CHB-1 stays first")

    def test_create_sorts_to_do_and_places_the_new_ticket(self) -> None:
        """The search can miss a ticket created a moment ago: it is sorted as the last To Do ticket."""
        fake = create_fake()
        fake.routes[("POST", SEARCH)] = keys_answer({"To Do": ["CHB-1", "CHB-2"]}, priorities={"CHB-1": "Low", "CHB-2": "Low"})
        code, out, _, _ = _create_with(f"## {WHY}\n\nThe owner waits.", fake, priority="High")

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-200")
        self.assertEqual(_apply_ranks(["CHB-1", "CHB-2", "CHB-200"], fake), ["CHB-200", "CHB-1", "CHB-2"])
        self.assertEqual(_write_paths(fake)[0], CREATE_PATH, "the ticket is created first")

    def test_a_failed_sort_after_create_is_a_notice_and_exits_0(self) -> None:
        fake = create_fake()
        fake.routes[("POST", SEARCH)] = 500
        code, out, _, _ = _create_with(f"## {WHY}\n\nThe owner waits.", fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-200")
        self.assertIn("could not sort To Do", out)
        self.assertIn("factory/bin/jira sort-todo", out)

    def test_a_start_sorts_to_do_before_the_top_ticket_check(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-5", "CHB-12"]}, priorities={"CHB-5": "Low", "CHB-12": "High"})
        code, out, _ = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-12 -> In Progress")
        self.assertEqual(_write_paths(fake), [RANK_PATH, MOVE_PATH])

    def test_a_blocked_epic_or_unlisted_start_writes_no_rank_even_when_to_do_is_out_of_order(self) -> None:
        """The refusals that do not depend on the order come before the sort."""
        cases = {
            "blocked": {"links": {"CHB-12": blocked_by("CHB-91")}},
            "epic": {"epics": ("CHB-12",)},
            "missing from the search": {},
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                fake = transition_fake(
                    "To Do", {"To Do": ["CHB-5", "CHB-6"], "In Review": ["CHB-91"]}, priorities={"CHB-6": "High"}, **kwargs
                )
                if name == "missing from the search":
                    fake.routes[("POST", SEARCH)] = keys_answer(
                        {"To Do": ["CHB-5", "CHB-6"], "In Review": ["CHB-91"]}, priorities={"CHB-6": "High"}
                    )
                code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 4)
                self.assertEqual(fake.writes(), [])
                if name == "missing from the search":
                    self.assertIn("CHB-12 is missing from the search of open tickets", err)

    def test_a_start_refused_by_the_top_rule_names_the_priority_command(self) -> None:
        fake = transition_fake("To Do", {"To Do": ["CHB-5", "CHB-12"]}, priorities={"CHB-5": "High", "CHB-12": "Medium"})
        code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 4)
        self.assertIn("factory/bin/jira priority CHB-12", err)
        self.assertEqual(fake.writes(), [])


class SectionFirstTest(unittest.TestCase):
    """CHB-148: `section --first` puts the section at the top and keeps every other node as it is."""

    def _rest(self) -> list[dict[str, Any]]:
        link = {"type": "text", "text": "the docs", "marks": [{"type": "link", "attrs": {"href": "https://x.y/z", "title": "t"}}]}
        mention = {"type": "mention", "attrs": {"id": "a1", "text": "@Owner"}}
        return [
            _heading("Problem"),
            _para("Intro."),
            {"type": "paragraph", "content": [link, mention]},
            {"type": "codeBlock", "attrs": {"language": "sh"}, "content": [{"type": "text", "text": "make check\n"}]},
            _heading("Notes", 3),
            {"type": "panel", "attrs": {"panelType": "info"}, "content": [_para("Careful.")]},
            _heading("Acceptance criteria"),
            _para("- AC1."),
        ]

    def test_inserts_the_section_first_and_keeps_the_rest_byte_for_byte(self) -> None:
        rest = self._rest()
        before = {"type": "doc", "version": 1, "content": rest}
        after = replace_section(json.loads(json.dumps(before)), WHY, "The owner waits.", first=True)

        self.assertEqual(after["content"][:2], [_heading(WHY), _para("The owner waits.")])
        self.assertEqual(json.dumps(after["content"][2:]), json.dumps(rest))
        self.assertEqual({k: v for k, v in after.items() if k != "content"}, {"type": "doc", "version": 1})

    def test_replaces_a_first_section_in_place(self) -> None:
        rest = self._rest()
        after = replace_section(_doc(_heading(WHY), _para("Old."), *rest), WHY, "New.", first=True)

        self.assertEqual(after["content"][:2], [_heading(WHY), _para("New.")])
        self.assertEqual(json.dumps(after["content"][2:]), json.dumps(rest))

    def test_moves_a_section_from_further_down_to_the_top(self) -> None:
        rest = self._rest()
        after = replace_section(_doc(*rest[:4], _heading(WHY, 3), _para("Old."), *rest[4:]), WHY, "New.", first=True)

        self.assertEqual(after["content"][:2], [_heading(WHY), _para("New.")])
        self.assertEqual(json.dumps(after["content"][2:]), json.dumps(rest))

    def test_the_command_writes_the_section_first(self) -> None:
        rest = self._rest()
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": _doc(*rest)}}, ("PUT", "/rest/api/3/issue/CHB-12"): None})
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("The owner waits.")
            handle.flush()
            code, out, _ = _run(["section", "CHB-12", "--heading", WHY, "--first", "--body-file", handle.name], fake)

        self.assertEqual(code, 0)
        self.assertIn("set", out)
        (_, _, sent), = fake.writes()
        content = sent["fields"]["description"]["content"]
        self.assertEqual(content[:2], [_heading(WHY), _para("The owner waits.")])
        self.assertEqual(json.dumps(content[2:]), json.dumps(rest))

    def test_the_why_heading_always_goes_first(self) -> None:
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": _doc(_para("Intro."))}}, ("PUT", "/rest/api/3/issue/CHB-12"): None})
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("The owner waits.")
            handle.flush()
            _run(["section", "CHB-12", "--heading", WHY, "--body-file", handle.name], fake)

        (_, _, sent), = fake.writes()
        self.assertEqual(sent["fields"]["description"]["content"][0], _heading(WHY))


COMMENT_PATH = "/rest/api/3/issue/CHB-12/comment"
NEW_STATUS = "/rest/api/3/issue/CHB-200?fields=status"
NEW_MOVE = "/rest/api/3/issue/CHB-200/transitions"
NEW_COMMENT = "/rest/api/3/issue/CHB-200/comment"


def _comments(fake: FakeJira, path: str = COMMENT_PATH) -> list[str]:
    return [adf_to_text(body["body"]) for method, where, body in fake.writes() if (method, where) == ("POST", path)]


def backlog_create_fake(state: str, initial: str, pull: list[str] | None = None, **kwargs: Any) -> FakeJira:
    """CHB-200 is created under CHB-27 and Jira puts it in `initial`."""
    return FakeJira(
        {
            ("GET", "/rest/api/3/issue/CHB-27?fields=issuetype"): {"fields": {"issuetype": {"name": "Epic"}}},
            ("POST", CREATE_PATH): {"key": "CHB-200"},
            ("GET", BOARD): board_config(state=state, **BOARD_LIMITS),
            ("GET", NEW_STATUS): {"fields": {"status": {"name": initial, "id": ALL_IDS[initial]}}},
            ("GET", NEW_MOVE): transitions_of(state),
            ("POST", NEW_MOVE): None,
            ("POST", NEW_COMMENT): {"id": "7"},
            ("POST", SEARCH): keys_answer({pull_name(state): pull or []}, **kwargs),
            ("PUT", RANK_PATH): None,
        }
    )


def _create_args(
    fake: FakeJira, *extra: str, credentials_reader: Any = lambda: CREDENTIALS, priority: str | None = "Medium"
) -> tuple[int, str, str]:
    """`create` with a valid description; priority None leaves --priority out."""
    with tempfile.NamedTemporaryFile("w") as handle:
        handle.write(f"## {WHY}\n\nThe owner waits.")
        handle.flush()
        argv = ["create", "--parent", "CHB-27", "--summary", "Do it", "--description-file", handle.name]
        argv += [] if priority is None else ["--priority", priority]
        argv += extra
        return _run(argv, fake, credentials_reader=credentials_reader)


class PullListTest(unittest.TestCase):
    """CHB-153 AC1: the pull list is the column Ready, else To Do; Backlog is never sorted and never next."""

    def test_the_pull_list_in_each_board_state(self) -> None:
        for state, expected in {"before": "To Do", "half": "To Do", "after": "Ready"}.items():
            with self.subTest(state):
                columns = Jira(EMAIL, TOKEN, FakeJira({("GET", BOARD): board_config(state=state)})).board_columns()
                pull = jira.pull_list(columns)
                self.assertIsNotNone(pull)
                self.assertEqual(pull.name if pull else None, expected)

    def test_a_board_with_only_backlog_has_no_pull_list(self) -> None:
        self.assertIsNone(jira.pull_list([jira.Column("Backlog", frozenset({BACKLOG_ID}))]))

    def test_sort_ranks_only_the_pull_list_never_backlog(self) -> None:
        for state in BOARD_STATES:
            with self.subTest(state):
                by_status = {"Backlog": ["CHB-30", "CHB-31"]} if state != "before" else {}
                fake = sort_fake(["CHB-1", "CHB-2"], by_status, state=state, priorities={"CHB-2": "High", "CHB-30": "High", "CHB-31": "High"})
                code, out, _ = _run(["sort-todo"], fake)

                self.assertEqual(code, 0)
                touched = {key for body in _rank_writes(fake) for key in [*body["issues"], body.get("rankBeforeIssue"), body.get("rankAfterIssue")]}
                self.assertEqual(touched - {None}, {"CHB-1", "CHB-2"})
                self.assertIn(f"{pull_name(state)} sorted", out)
                self.assertNotIn("CHB-30", out)

    def test_a_backlog_ticket_is_never_the_top_ticket(self) -> None:
        for state in ("half", "after"):
            with self.subTest(state):
                pull = pull_name(state)
                fake = transition_fake(pull, {"Backlog": ["CHB-30"], pull: ["CHB-12"]}, priorities={"CHB-30": "High"}, state=state)
                code, out, err = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 0, err)
                self.assertEqual(out.splitlines()[0], "CHB-12 -> In Progress")
                self.assertEqual(_write_paths(fake), [MOVE_PATH])

    def test_a_start_from_the_pull_list_checks_the_top_ticket_of_that_list(self) -> None:
        for state in BOARD_STATES:
            with self.subTest(state):
                pull = pull_name(state)
                fake = transition_fake(pull, {pull: ["CHB-5", "CHB-12"]}, state=state)
                code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

                self.assertEqual(code, 4)
                self.assertIn(f"CHB-12 is not the top ticket of {pull}: CHB-5 is", err)
                self.assertEqual(fake.writes(), [])

    def test_a_start_from_backlog_is_refused(self) -> None:
        for state, target in [("half", "In Progress"), ("after", "In Progress"), ("after", "In Review"), ("after", "Needs human")]:
            with self.subTest(f"{state}: {target}"):
                description = description_with("Acceptance criteria", *SECTIONS.values())
                fake = transition_fake("Backlog", {"In Progress": ["CHB-1"]}, description=description, state=state)
                code, out, err = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 4)
                self.assertEqual(out, "")
                self.assertIn(f"CHB-12 is in Backlog: move it to {pull_name(state)} first", err)
                self.assertIn("factory/bin/jira ready CHB-12 --trigger", err)
                self.assertEqual(fake.writes(), [])

    def test_backlog_to_done_or_discarded_needs_no_trigger(self) -> None:
        board = board_config(state="after", **BOARD_LIMITS)
        board["columnConfig"]["columns"].append({"name": "Discarded", "statuses": [{"id": "10008", "self": "x"}]})
        for target in ("Done", "Discarded"):
            with self.subTest(target):
                fake = transition_fake("Backlog", board=board, state="after")
                transitions = transitions_of("after")
                transitions["transitions"].append({"id": "99", "to": {"name": "Discarded", "id": "10008"}})
                fake.routes[("GET", "/rest/api/3/issue/CHB-12/transitions")] = transitions
                code, out, err = _run(["transition", "CHB-12", target], fake)

                self.assertEqual(code, 0, err)
                self.assertEqual(_write_paths(fake), [MOVE_PATH])

    def test_the_free_slot_line_names_only_pull_list_tickets(self) -> None:
        for state in BOARD_STATES:
            with self.subTest(state):
                pull = pull_name(state)
                by_status = {"In Progress": ["CHB-1"], pull: ["CHB-7"], **({"Backlog": ["CHB-30", "CHB-31"]} if state != "before" else {})}
                fake = transition_fake("In Progress", by_status, priorities={"CHB-30": "High"}, state=state)
                code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines()[-1], "In Progress 1/3: 2 slots free; next: CHB-7")

    def test_nothing_to_start_names_the_pull_list(self) -> None:
        for state in BOARD_STATES:
            with self.subTest(state):
                by_status = {"In Progress": ["CHB-1"], **({"Backlog": ["CHB-30"]} if state != "before" else {})}
                fake = transition_fake("In Progress", by_status, state=state)
                code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines()[-1], f"In Progress 1/3: 2 slots free; next: nothing to start in {pull_name(state)}")


class TriggerTest(unittest.TestCase):
    """CHB-153 AC2: a move from Backlog to the pull list needs --trigger, posted as a comment."""

    def test_backlog_to_the_pull_list_without_a_trigger_exits_2_and_writes_nothing(self) -> None:
        for state in ("half", "after"):
            with self.subTest(state):
                fake = transition_fake("Backlog", state=state)
                code, out, err = _run(["transition", "CHB-12", pull_name(state)], fake)

                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--trigger", err)
                self.assertEqual(fake.writes(), [])

    def test_the_message_points_to_the_ready_command(self) -> None:
        """CHB-154: `ready` replaces `transition ... Ready --trigger`."""
        fake = transition_fake("Backlog", state="after")
        code, _, err = _run(["transition", "CHB-12", "Ready"], fake)

        self.assertEqual(code, 2)
        self.assertIn('factory/bin/jira ready CHB-12 --trigger "<which trigger and its evidence>"', err)
        self.assertNotIn("transition CHB-12", err)

    def test_with_a_trigger_ready_moves_and_comments(self) -> None:
        for state in ("half", "after"):
            with self.subTest(state):
                pull = pull_name(state)
                fake = ready_fake(state=state)
                code, out, err = _run(["ready", "CHB-12", "--trigger", "1. broken: the build fails on main"], fake)

                self.assertEqual(code, 0, err)
                self.assertEqual(out.splitlines()[0], f"CHB-12 -> {pull}")
                self.assertEqual(_write_paths(fake), [ISSUE_PATH, MOVE_PATH, COMMENT_PATH])
                self.assertEqual(_comments(fake), [f"Moved to {pull}. Trigger: 1. broken: the build fails on main"])

    def test_a_failed_trigger_comment_says_the_ticket_moved(self) -> None:
        fake = ready_fake()
        fake.routes[("POST", COMMENT_PATH)] = 500
        code, _, err = _run(["ready", "CHB-12", "--trigger", "2. the owner asked"], fake)

        self.assertEqual(code, 1)
        self.assertIn("CHB-12 moved to Ready, but the trigger comment failed", err)
        self.assertIn("factory/bin/jira comment CHB-12", err)

    def test_a_move_into_backlog_needs_no_trigger(self) -> None:
        for state in ("half", "after"):
            with self.subTest(state):
                fake = transition_fake(pull_name(state), state=state)
                code, out, err = _run(["transition", "CHB-12", "Backlog"], fake)

                self.assertEqual(code, 0, err)
                self.assertEqual(out.splitlines()[0], "CHB-12 -> Backlog")
                self.assertEqual(_write_paths(fake), [MOVE_PATH])

    def test_a_trigger_on_another_move_exits_2_and_writes_nothing(self) -> None:
        for state, current, target in [("after", "Ready", "Backlog"), ("before", "In Progress", "In Review"), ("after", "In Review", "Ready")]:
            with self.subTest(f"{state}: {current} -> {target}"):
                fake = transition_fake(current, {"In Progress": ["CHB-1"]}, state=state)
                code, _, err = _run(["transition", "CHB-12", target, "--trigger", "1. broken"], fake)

                self.assertEqual(code, 2)
                self.assertIn("transition takes no --trigger: factory/bin/jira ready CHB-12", err)
                self.assertEqual(fake.writes(), [])

    def test_a_trigger_on_a_start_writes_no_rank(self) -> None:
        """`transition` refuses any --trigger (CHB-154) before it reads or sorts anything."""
        for state in BOARD_STATES:
            with self.subTest(state):
                pull = pull_name(state)
                fake = transition_fake(pull, {pull: ["CHB-5", "CHB-12"]}, priorities={"CHB-12": "High"}, state=state)
                code, _, err = _run(["transition", "CHB-12", "In Progress", "--trigger", "1. broken"], fake)

                self.assertEqual(code, 2)
                self.assertIn("transition takes no --trigger: factory/bin/jira ready CHB-12", err)
                self.assertEqual(fake.writes(), [])

    def test_a_trigger_on_no_move_exits_2(self) -> None:
        fake = transition_fake("Ready", state="after")
        code, _, err = _run(["transition", "CHB-12", "Ready", "--trigger", "2. the owner asked"], fake)

        self.assertEqual(code, 2)
        self.assertIn("transition takes no --trigger: factory/bin/jira ready CHB-12", err)
        self.assertEqual(fake.writes(), [])

    def test_an_empty_trigger_exits_2_before_1password(self) -> None:
        fake = FakeJira({})
        code, _, err = _run(["transition", "CHB-12", "Ready", "--trigger", "  "], fake, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("trigger is empty", err)
        self.assertEqual(fake.requests, [])

    def test_a_column_name_finds_the_status_of_that_column(self) -> None:
        """A column renamed on a board whose status keeps its old name: `ready` still finds the status."""
        board = board_config(state="half", **BOARD_LIMITS)
        board["columnConfig"]["columns"][1]["name"] = "Ready"  # the column holds the status "To Do"
        fake = ready_fake(state="half", board=board)
        code, out, err = _run(["ready", "CHB-12", "--trigger", "3. unblocks CHB-1"], fake)

        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "CHB-12 -> To Do")
        self.assertIn(("POST", MOVE_PATH, {"transition": {"id": "12"}}), fake.writes())
        self.assertEqual(_comments(fake), ["Moved to Ready. Trigger: 3. unblocks CHB-1"])


class CreateInBacklogTest(unittest.TestCase):
    """CHB-153 AC3: `create` lands in Backlog (CHB-154: always; `ready` moves it on)."""

    def test_without_a_backlog_column_the_ticket_stays_in_the_pull_list(self) -> None:
        fake = backlog_create_fake("before", "To Do")
        code, out, _ = _create_args(fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "CHB-200")
        self.assertEqual(_write_paths(fake), [CREATE_PATH])

    def test_lands_in_backlog_and_the_pull_list_is_not_sorted(self) -> None:
        for state in ("half", "after"):
            with self.subTest(state):
                pull = pull_name(state)
                fake = backlog_create_fake(state, pull, pull=["CHB-1", "CHB-2"], priorities={"CHB-2": "High"})
                code, out, err = _create_args(fake, priority="High")

                self.assertEqual(code, 0, err)
                self.assertEqual(out.splitlines(), ["CHB-200", "CHB-200 in Backlog"])
                self.assertEqual(_write_paths(fake), [CREATE_PATH, NEW_MOVE])
                self.assertEqual(fake.writes()[1][2], {"transition": {"id": "11"}})

    def test_a_ticket_jira_put_in_backlog_is_not_moved(self) -> None:
        fake = backlog_create_fake("after", "Backlog")
        code, out, _ = _create_args(fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["CHB-200", "CHB-200 in Backlog"])
        self.assertEqual(_write_paths(fake), [CREATE_PATH])

    def test_ready_and_trigger_are_refused_and_point_to_the_ready_command(self) -> None:
        """CHB-154: `ready` is the only way into Ready, so `create` always lands in Backlog."""
        for extra in (["--ready"], ["--trigger", "1. broken"], ["--ready", "--trigger", "2. the owner asked"]):
            with self.subTest(extra):
                fake = backlog_create_fake("after", "Ready")
                code, out, err = _create_args(fake, *extra, credentials_reader=_no_credentials)

                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("create puts a ticket in Backlog", err)
                self.assertIn("factory/bin/jira ready CHB-<n> --trigger", err)
                self.assertEqual(fake.requests, [])

    def test_a_failed_move_after_create_is_a_notice_and_exits_0(self) -> None:
        fake = backlog_create_fake("after", "Ready")
        fake.routes[("POST", NEW_MOVE)] = 500
        code, out, _ = _create_args(fake)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-200")
        self.assertIn("could not place CHB-200", out)
        self.assertIn('factory/bin/jira transition CHB-200 "Backlog"', out)


# CHB-154: the Definition of Ready checklist.
ISSUE_PATH = "/rest/api/3/issue/CHB-12"
ALL_BOXES = tuple(box.name for box in jira.DOR_BOXES)


def dor_description(done: tuple[str, ...] = jira.JUDGMENT_BOXES, *, why: bool = True, acceptance: bool = True) -> dict[str, Any]:
    """A description with the checklist (`done` ticked), and optionally Why it matters and acceptance criteria."""
    parts = [WHY_SECTION] if why else []
    parts.append(f"## Definition of Ready\n\n{jira.checklist_markdown(done)}")
    if acceptance:
        parts.append("## Acceptance criteria\n\n- A test shows the thing.")
    return doc("\n\n".join(parts))


def ready_fake(
    description: Any = None,
    *,
    state: str = "after",
    links: dict[str, list[dict[str, Any]]] | None = None,
    by_status: dict[str, list[str]] | None = None,
    board: Any = None,
    current: str = "Backlog",
    epics: tuple[str, ...] = (),
) -> FakeJira:
    """CHB-12 sits in Backlog with a passing checklist unless told otherwise."""
    fake = transition_fake(
        current,
        by_status,
        description=dor_description() if description is None else description,
        board=board,
        links=links,
        state=state,
        epics=epics,
    )
    fake.routes[("PUT", ISSUE_PATH)] = None
    fake.routes[("POST", COMMENT_PATH)] = {"id": "5"}
    fake.routes[("GET", DESCRIPTION_PATH)] = {"fields": {"description": dor_description() if description is None else description}}
    return fake


def _put_descriptions(fake: FakeJira, path: str = ISSUE_PATH) -> list[Any]:
    return [body["fields"]["description"] for method, where, body in fake.writes() if (method, where) == ("PUT", path)]


def _task_items(description: Any) -> list[dict[str, Any]]:
    return [item for node in description["content"] if node.get("type") == "taskList" for item in node["content"]]


def _heading_of(node: dict[str, Any]) -> str:
    return adf_to_text(node).strip().lstrip("#").strip()


class TaskListTest(unittest.TestCase):
    """CHB-154: "- [ ]" and "- [x]" lines render as a Jira task list (ADF taskList, taskItem)."""

    def test_task_lines_become_a_task_list(self) -> None:
        (task_list,) = text_to_adf("- [ ] first `x`\n- [x] second\n- [X] third")["content"]

        self.assertEqual(task_list["type"], "taskList")
        self.assertTrue(task_list["attrs"]["localId"])
        states = [item["attrs"]["state"] for item in task_list["content"]]
        self.assertEqual(states, ["TODO", "DONE", "DONE"])
        ids = [item["attrs"]["localId"] for item in task_list["content"]]
        self.assertEqual(len(set(ids + [task_list["attrs"]["localId"]])), 4)
        first = task_list["content"][0]
        self.assertEqual(first["type"], "taskItem")
        self.assertEqual(first["content"], [_text("first "), _text("x", CODE)])

    def test_reads_back_as_markdown(self) -> None:
        text = "- [ ] first\n- [x] second"
        self.assertEqual(adf_to_text(text_to_adf(text)), text)

    def test_a_nested_task_list_reads_back_as_lines(self) -> None:
        inner = text_to_adf("- [x] inner")["content"][0]
        outer = text_to_adf("- [ ] outer")["content"][0]
        outer["content"].append(inner)

        self.assertEqual(adf_to_text({"type": "doc", "content": [outer]}), "- [ ] outer\n- [x] inner")

    def test_a_bullet_list_and_a_task_list_stay_apart(self) -> None:
        nodes = text_to_adf("- plain\n- [ ] task\n- plain again")["content"]

        self.assertEqual([node["type"] for node in nodes], ["bulletList", "taskList", "bulletList"])

    def test_checklist_text_is_neither_acceptance_criteria_nor_a_why_section(self) -> None:
        description = doc(f"## Definition of Ready\n\n{jira.checklist_markdown()}")

        self.assertFalse(jira.has_acceptance_criteria(description))
        self.assertIsNotNone(jira.why_problem(description))


class AddChecklistTest(unittest.TestCase):
    """CHB-154 AC2: the checklist goes right after "Why it matters"; other nodes stay as they are."""

    def test_goes_after_the_why_section_and_keeps_the_rest(self) -> None:
        before = doc(f"{WHY_SECTION}\n\nSecond paragraph.\n\n## Plan\n\n- [ ] an old line\n\nDo it.")
        after = jira.add_checklist(before)

        assert after is not None
        headings = [_heading_of(node) for node in after["content"] if node["type"] == "heading"]
        self.assertEqual(headings, ["Why it matters", "Definition of Ready", "Plan"])
        self.assertEqual(json.dumps(after["content"][:3]), json.dumps(before["content"][:3]))
        self.assertEqual(json.dumps(after["content"][5:]), json.dumps(before["content"][3:]))
        self.assertEqual(jira.box_states(after), dict.fromkeys(ALL_BOXES, "TODO"))

    def test_without_a_why_section_it_goes_first(self) -> None:
        after = jira.add_checklist(doc("## Plan\n\nDo it."))

        assert after is not None
        self.assertEqual(_heading_of(after["content"][0]), "Definition of Ready")
        self.assertEqual(_heading_of(after["content"][2]), "Plan")

    def test_an_empty_description_gets_only_the_checklist(self) -> None:
        after = jira.add_checklist(None)

        assert after is not None
        self.assertEqual(after["type"], "doc")
        self.assertEqual(jira.checklist_count(after), 1)

    def test_the_heading_takes_the_level_of_the_why_heading(self) -> None:
        """A level-1 Why followed by a level-2 checklist would make an empty Why look full."""
        after = jira.add_checklist(doc("# Why it matters\n\n# Plan\n\nDo it."))

        assert after is not None
        self.assertEqual(after["content"][1]["attrs"]["level"], 1)
        self.assertIsNotNone(jira.why_problem(after))

    def test_a_description_with_the_checklist_is_left_alone(self) -> None:
        self.assertIsNone(jira.add_checklist(dor_description()))


class CreateChecklistTest(unittest.TestCase):
    """CHB-154 AC2: `create` adds the checklist, once."""

    def test_create_adds_one_unticked_checklist(self) -> None:
        code, _, err, fake = _create_with(f"{WHY_SECTION}\n\n## Plan\n\nDo it.")

        self.assertEqual(code, 0, err)
        sent = fake.writes()[0][2]["fields"]["description"]
        self.assertEqual(jira.checklist_count(sent), 1)
        self.assertEqual(jira.box_states(sent), dict.fromkeys(ALL_BOXES, "TODO"))

    def test_a_description_with_the_checklist_keeps_one(self) -> None:
        text = f"{WHY_SECTION}\n\n## Definition of Ready\n\n{jira.checklist_markdown(('small',))}"
        code, _, err, fake = _create_with(text)

        self.assertEqual(code, 0, err)
        sent = fake.writes()[0][2]["fields"]["description"]
        self.assertEqual(jira.checklist_count(sent), 1)
        self.assertEqual(jira.box_states(sent)["small"], "DONE")


def backfill_fake(by_status: dict[str, list[str]], descriptions: dict[str, Any], epics: tuple[str, ...] = ()) -> FakeJira:
    """Open tickets as the search lists them. Each issue read answers from `descriptions`, which a PUT updates."""
    routes: dict[tuple[str, str], Any] = {("POST", SEARCH): keys_answer(by_status, epics=epics)}
    for key in [k for keys in by_status.values() for k in keys]:

        def read(_: Any, key: str = key) -> dict[str, Any]:
            kind = "Epic" if key in epics else "Task"
            return {"fields": {"description": descriptions.get(key), "issuetype": {"name": kind}}}

        def write(body: dict[str, Any], key: str = key) -> None:
            descriptions[key] = body["fields"]["description"]

        routes[("GET", f"/rest/api/3/issue/{key}?fields=description,issuetype")] = read
        routes[("PUT", f"/rest/api/3/issue/{key}")] = write
    return FakeJira(routes)


class BackfillTest(unittest.TestCase):
    """CHB-154 AC2: a backfill run leaves every open ticket with exactly one checklist."""

    BY_STATUS = {
        "Backlog": ["CHB-1"],
        "Ready": ["CHB-2", "CHB-3"],
        "In Progress": ["CHB-4"],
        "Validating": ["CHB-5"],
        "Needs human": ["CHB-6"],
    }

    def _descriptions(self) -> dict[str, Any]:
        return {
            "CHB-1": doc(f"{WHY_SECTION}\n\n## Plan\n\nDo it."),
            "CHB-2": dor_description(()),
            "CHB-4": doc("## Plan\n\nNo Why section yet."),
            "CHB-5": doc(WHY_SECTION),
            "CHB-6": None,
        }

    def test_every_open_ticket_gets_exactly_one_checklist_and_epics_none(self) -> None:
        descriptions = self._descriptions()
        fake = backfill_fake(self.BY_STATUS, descriptions, epics=("CHB-3",))
        code, out, err = _run(["checklist", "--open"], fake)

        self.assertEqual(code, 0, err)
        self.assertEqual(
            out.splitlines(),
            [
                "CHB-1: added",
                "CHB-2: already there",
                "CHB-3: skipped (epic)",
                "CHB-4: added",
                "CHB-5: added",
                "CHB-6: added",
                "6 tickets: 4 added, 1 already there, 1 epic skipped",
            ],
        )
        for key in ("CHB-1", "CHB-2", "CHB-4", "CHB-5", "CHB-6"):
            self.assertEqual(jira.checklist_count(descriptions[key]), 1, key)
        self.assertNotIn("CHB-3", descriptions)

    def test_a_second_run_writes_nothing(self) -> None:
        descriptions = self._descriptions()
        _run(["checklist", "--open"], backfill_fake(self.BY_STATUS, descriptions, epics=("CHB-3",)))
        fake = backfill_fake(self.BY_STATUS, descriptions, epics=("CHB-3",))
        code, out, _ = _run(["checklist", "--open"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("0 added, 5 already there", out)

    def test_existing_text_is_never_changed(self) -> None:
        descriptions = self._descriptions()
        before = json.dumps(descriptions["CHB-1"]["content"])
        _run(["checklist", "CHB-1"], backfill_fake({"Backlog": ["CHB-1"]}, descriptions))

        content = descriptions["CHB-1"]["content"]
        self.assertEqual(json.dumps(content[:2] + content[4:]), before)

    def test_two_checklists_are_reported_and_left_alone(self) -> None:
        twice = doc(f"{WHY_SECTION}\n\n## Definition of Ready\n\n- [ ] a\n\n## Definition of Ready\n\n- [ ] b")
        descriptions = {"CHB-1": twice, "CHB-2": doc(WHY_SECTION)}
        fake = backfill_fake({"Backlog": ["CHB-1", "CHB-2"]}, descriptions)
        code, out, err = _run(["checklist", "--open"], fake)

        self.assertEqual(code, 1)
        self.assertIn("CHB-1: 2 Definition of Ready sections; fix it by hand", out)
        self.assertIn("CHB-2: added", out)
        self.assertIn("CHB-1", err)
        self.assertEqual(_write_paths(fake), ["/rest/api/3/issue/CHB-2"])

    def test_keys_or_open_but_not_both(self) -> None:
        for argv in (["checklist"], ["checklist", "CHB-1", "--open"]):
            with self.subTest(argv):
                code, _, _ = _run(argv, FakeJira({}), credentials_reader=_no_credentials)
                self.assertEqual(code, 2)


class TickTest(unittest.TestCase):
    """CHB-154: `tick` ticks one judgment box and keeps the rest of the description."""

    def _fake(self, description: Any) -> FakeJira:
        return FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": description}}, ("PUT", ISSUE_PATH): None})

    def test_ticks_one_box_and_keeps_the_ids(self) -> None:
        before = dor_description(())
        fake = self._fake(before)
        code, out, err = _run(["tick", "CHB-12", "small"], fake)

        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "CHB-12 box 'small' ticked")
        (after,) = _put_descriptions(fake)
        self.assertEqual(jira.box_states(after), {**dict.fromkeys(ALL_BOXES, "TODO"), "small": "DONE"})
        ids = [item["attrs"]["localId"] for item in _task_items(after)]
        self.assertEqual(ids, [item["attrs"]["localId"] for item in _task_items(before)])

    def test_a_ticked_box_writes_nothing(self) -> None:
        fake = self._fake(dor_description(("small",)))
        code, out, _ = _run(["tick", "CHB-12", "small"], fake)

        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(fake.writes(), [])

    def test_a_verified_box_cannot_be_ticked(self) -> None:
        for box in jira.VERIFIED_BOXES:
            with self.subTest(box):
                fake = FakeJira({})
                code, _, err = _run(["tick", "CHB-12", box], fake, credentials_reader=_no_credentials)

                self.assertEqual(code, 2)
                self.assertIn("ready verifies", err)
                self.assertEqual(fake.requests, [])

    def test_an_unknown_box_is_a_usage_error(self) -> None:
        code, _, _ = _run(["tick", "CHB-12", "everything"], FakeJira({}), credentials_reader=_no_credentials)

        self.assertEqual(code, 2)

    def test_no_checklist_names_the_command_that_adds_it(self) -> None:
        fake = self._fake(READY)
        code, _, err = _run(["tick", "CHB-12", "small"], fake)

        self.assertEqual(code, 1)
        self.assertIn("factory/bin/jira checklist CHB-12", err)
        self.assertEqual(fake.writes(), [])


class ReadyCommandTest(unittest.TestCase):
    """CHB-154 AC1: `ready` re-verifies the checklist and moves a Backlog ticket to Ready with its trigger."""

    TRIGGER = ["--trigger", "2. the owner asked for it on 2026-11-10"]

    def test_all_boxes_pass_it_ticks_the_verified_ones_moves_and_comments(self) -> None:
        fake = ready_fake()
        code, out, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "CHB-12 -> Ready")
        self.assertEqual(_write_paths(fake), [ISSUE_PATH, MOVE_PATH, COMMENT_PATH])
        (ticked,) = _put_descriptions(fake)
        self.assertEqual(jira.box_states(ticked), dict.fromkeys(ALL_BOXES, "DONE"))
        self.assertEqual(_comments(fake), ["Moved to Ready. Trigger: 2. the owner asked for it on 2026-11-10"])

    def test_the_ticks_go_on_a_fresh_read_of_the_description(self) -> None:
        """An edit made while `ready` checks and reads the board is kept."""
        fake = ready_fake()
        edited = dor_description()
        edited["content"].append(_para("Added by someone else meanwhile."))
        fake.routes[("GET", DESCRIPTION_PATH)] = {"fields": {"description": edited}}
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 0, err)
        (ticked,) = _put_descriptions(fake)
        self.assertEqual(ticked["content"][-1], _para("Added by someone else meanwhile."))
        self.assertEqual(jira.box_states(ticked), dict.fromkeys(ALL_BOXES, "DONE"))

    def test_a_missing_verified_box_refuses(self) -> None:
        for box in jira.VERIFIED_BOXES:
            with self.subTest(box):
                text = next(b.text for b in jira.DOR_BOXES if b.name == box)
                lines = [line for line in jira.checklist_markdown(jira.JUDGMENT_BOXES).splitlines() if text not in line]
                description = doc(f"{WHY_SECTION}\n\n## Definition of Ready\n\n" + "\n".join(lines) + "\n\n## Acceptance criteria\n\n- A test.")
                fake = ready_fake(description)
                code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

                self.assertEqual(code, 4)
                self.assertIn("the box is missing from the checklist", err)
                self.assertEqual(fake.writes(), [])

    def test_a_ticket_that_leaves_backlog_during_the_checks_does_not_move(self) -> None:
        fake = ready_fake()
        reads = [issue_in("Backlog", dor_description()), issue_in("In Progress", dor_description())]
        fake.routes[("GET", f"/rest/api/3/issue/CHB-12?fields={ISSUE_FIELDS}")] = lambda _: reads.pop(0)
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 1)
        self.assertIn("CHB-12 left Backlog while ready checked it; nothing moved", err)
        self.assertEqual(fake.writes(), [])

    def test_a_move_jira_does_not_offer_writes_nothing(self) -> None:
        """The verified boxes are ticked only once the move is about to be sent."""
        fake = ready_fake()
        transitions = transitions_of("after")
        transitions["transitions"] = [t for t in transitions["transitions"] if t["to"]["name"] != "Ready"]
        fake.routes[("GET", "/rest/api/3/issue/CHB-12/transitions")] = transitions
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 1)
        self.assertIn("cannot move to 'Ready'", err)
        self.assertEqual(fake.writes(), [])

    def test_all_boxes_ticked_already_it_writes_no_description(self) -> None:
        fake = ready_fake(dor_description(ALL_BOXES))
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 0, err)
        self.assertEqual(_write_paths(fake), [MOVE_PATH, COMMENT_PATH])

    def test_each_unticked_judgment_box_refuses(self) -> None:
        for box in jira.JUDGMENT_BOXES:
            with self.subTest(box):
                fake = ready_fake(dor_description(tuple(b for b in jira.JUDGMENT_BOXES if b != box)))
                code, out, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

                self.assertEqual(code, 4)
                self.assertEqual(out, "")
                self.assertIn(f"factory/bin/jira tick CHB-12 {box}", err)
                self.assertEqual(fake.writes(), [])

    def test_no_acceptance_criteria_refuses_even_with_the_box_ticked(self) -> None:
        fake = ready_fake(dor_description(ALL_BOXES, acceptance=False))
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        self.assertIn("no acceptance criteria", err)
        self.assertEqual(fake.writes(), [])

    def test_an_open_blocker_refuses_even_with_the_box_ticked(self) -> None:
        fake = ready_fake(dor_description(ALL_BOXES), links={"CHB-12": blocked_by("CHB-3")}, by_status={"In Progress": ["CHB-3"]})
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        self.assertIn("blocked by open tickets: CHB-3", err)
        self.assertEqual(fake.writes(), [])

    def test_a_closed_blocker_does_not_refuse(self) -> None:
        fake = ready_fake(links={"CHB-12": blocked_by("CHB-3")}, by_status={"Done": ["CHB-3"]})
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 0, err)

    def test_no_why_section_refuses_even_with_the_box_ticked(self) -> None:
        fake = ready_fake(dor_description(ALL_BOXES, why=False))
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        self.assertIn("does not start with a section headed 'Why it matters'", err)
        self.assertEqual(fake.writes(), [])

    def test_the_refusal_names_every_open_box(self) -> None:
        description = dor_description((), why=False, acceptance=False)
        fake = ready_fake(description, links={"CHB-12": blocked_by("CHB-3")}, by_status={"In Progress": ["CHB-3"]})
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        for box in jira.DOR_BOXES:
            self.assertIn(box.prefix, err)

    def test_no_checklist_refuses_and_names_the_command_that_adds_it(self) -> None:
        fake = ready_fake(READY)
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        self.assertIn("factory/bin/jira checklist CHB-12", err)
        self.assertEqual(fake.writes(), [])

    def test_no_trigger_is_a_usage_error_before_1password(self) -> None:
        for argv in (["ready", "CHB-12"], ["ready", "CHB-12", "--trigger", "  "]):
            with self.subTest(argv):
                fake = FakeJira({})
                code, _, _ = _run(argv, fake, credentials_reader=_no_credentials)

                self.assertEqual(code, 2)
                self.assertEqual(fake.requests, [])

    def test_a_ticket_outside_backlog_is_refused(self) -> None:
        for state, current in [("after", "In Progress"), ("after", "Ready"), ("before", "To Do")]:
            with self.subTest(f"{state}: {current}"):
                fake = ready_fake(state=state, current=current)
                code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

                self.assertEqual(code, 2)
                self.assertIn("ready moves a ticket from Backlog", err)
                self.assertEqual(fake.writes(), [])


# CHB-157: the Ready limits and the Expedite lane.
READY_LIMITS = {**BOARD_LIMITS, "Ready": {"min": 3, "max": 10}}
TEN = [f"CHB-{n}" for n in range(1, 11)]
BELOW_READY = "look for Backlog tickets with a trigger; none qualify means tell the owner"


def ready_board(**limits: dict[str, int]) -> dict[str, Any]:
    return board_config(state="after", **{**READY_LIMITS, **limits})


class ReadyMaximumTest(unittest.TestCase):
    """CHB-157 AC1: a move into Ready is refused while Ready holds its board max; epics are not counted."""

    TRIGGER = ["--trigger", "2. the owner asked for it on 2026-11-10"]

    def test_ready_is_refused_at_the_max_and_names_the_tickets(self) -> None:
        fake = ready_fake(board=ready_board(), by_status={"Ready": TEN})
        code, out, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertIn("column 'Ready' is at its maximum (10/10): " + ", ".join(TEN), err)
        self.assertIn("factory/bin/jira transition CHB-<n> Backlog", err)
        self.assertEqual(fake.writes(), [])

    def test_ready_is_allowed_one_below_the_max(self) -> None:
        fake = ready_fake(board=ready_board(), by_status={"Ready": TEN[:9]})
        code, out, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[0], "CHB-12 -> Ready")

    def test_epics_in_ready_are_not_counted(self) -> None:
        fake = ready_fake(board=ready_board(), by_status={"Ready": [*TEN[:9], "CHB-90", "CHB-91"]}, epics=("CHB-90", "CHB-91"))
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 0, err)
        self.assertIn(
            "project = CHB AND (status in (10003, 10010) AND key != CHB-12 AND issuetype != Epic) ORDER BY key ASC", fake.searches()
        )

    def test_the_limit_comes_from_the_board(self) -> None:
        fake = ready_fake(board=ready_board(Ready={"max": 2}), by_status={"Ready": ["CHB-1", "CHB-2"]})
        code, _, err = _run(["ready", "CHB-12", *self.TRIGGER], fake)

        self.assertEqual(code, 4)
        self.assertIn("(2/2): CHB-1, CHB-2", err)

    def test_a_move_back_into_ready_is_refused_at_the_max_too(self) -> None:
        fake = transition_fake("In Progress", {"Ready": TEN}, board=ready_board(), state="after")
        code, _, err = _run(["transition", "CHB-12", "Ready"], fake)

        self.assertEqual(code, 4)
        self.assertIn("'Ready' is at its maximum (10/10)", err)
        self.assertEqual(fake.writes(), [])

    def test_an_epic_moving_back_into_ready_is_not_limited(self) -> None:
        fake = transition_fake("In Progress", {"Ready": TEN}, board=ready_board(), state="after")
        fake.routes[("GET", f"/rest/api/3/issue/CHB-12?fields={ISSUE_FIELDS}")] = issue_in("In Progress", READY, kind="Epic")
        code, _, err = _run(["transition", "CHB-12", "Ready"], fake)

        self.assertEqual(code, 0, err)


class ReadyMinimumTest(unittest.TestCase):
    """CHB-157 AC2: a move that leaves Ready below its board min prints the signal line; nothing is promoted."""

    def test_leaving_ready_below_its_min_prints_the_signal(self) -> None:
        fake = transition_fake("Ready", {"Ready": ["CHB-12", "CHB-1"]}, board=ready_board(), state="after")
        code, out, err = _run(["transition", "CHB-12", "Backlog"], fake)

        self.assertEqual(code, 0, err)
        self.assertIn(f"Ready is below its minimum (1/3): {BELOW_READY}", out.splitlines())
        self.assertEqual(len(fake.writes()), 1)

    def test_epics_do_not_count_toward_the_min(self) -> None:
        epics = ("CHB-90", "CHB-91", "CHB-92")
        fake = transition_fake("Ready", {"Ready": ["CHB-12", *epics]}, board=ready_board(), state="after", epics=epics)
        code, out, _ = _run(["transition", "CHB-12", "Backlog"], fake)

        self.assertEqual(code, 0)
        self.assertIn(f"Ready is below its minimum (0/3): {BELOW_READY}", out.splitlines())

    def test_a_move_into_ready_that_leaves_it_below_its_min_prints_the_signal(self) -> None:
        fake = ready_fake(board=ready_board(), by_status={"Ready": ["CHB-1"]})
        code, out, err = _run(["ready", "CHB-12", "--trigger", "1. broken"], fake)

        self.assertEqual(code, 0, err)
        self.assertIn(f"Ready is below its minimum (2/3): {BELOW_READY}", out.splitlines())

    def test_no_signal_while_ready_holds_its_min(self) -> None:
        fake = transition_fake("Ready", {"Ready": ["CHB-12", "CHB-1", "CHB-2", "CHB-3"]}, board=ready_board(), state="after")
        code, out, _ = _run(["transition", "CHB-12", "Backlog"], fake)

        self.assertEqual(code, 0)
        self.assertNotIn("minimum", out)

    def test_other_columns_keep_the_plain_signal(self) -> None:
        fake = transition_fake("In Progress", {"In Progress": ["CHB-12"]}, board=ready_board(), state="after")
        code, out, _ = _run(["transition", "CHB-12", "In Review"], fake)

        self.assertEqual(code, 0)
        self.assertIn("In Progress is below its minimum (0/1)", out.splitlines())


EXPEDITE = ["--expedite", "production bug: bookings fail with HTTP 500 since 10:02, see the deploy log"]
NEW_ASSIGNEE = "/rest/api/3/issue/CHB-200/assignee"


def expedite_fake(by_status: dict[str, list[str]] | None = None, priorities: dict[str, str | None] | None = None) -> FakeJira:
    """CHB-200 is created under CHB-27; In Progress and In Review are full unless `by_status` says otherwise."""
    full = {"In Progress": ["CHB-1", "CHB-2", "CHB-3"], "In Review": ["CHB-4", "CHB-5", "CHB-6"]}
    return FakeJira(
        {
            ("GET", "/rest/api/3/issue/CHB-27?fields=issuetype"): {"fields": {"issuetype": {"name": "Epic"}}},
            ("POST", CREATE_PATH): {"key": "CHB-200"},
            ("GET", BOARD): ready_board(),
            ("GET", NEW_MOVE): transitions_of("after"),
            ("POST", NEW_MOVE): None,
            ("POST", NEW_COMMENT): {"id": "7"},
            ("GET", "/rest/api/3/myself"): {"accountId": "agent-id"},
            ("PUT", NEW_ASSIGNEE): None,
            ("POST", SEARCH): keys_answer(full if by_status is None else by_status, priorities=priorities),
        }
    )


class ExpediteTest(unittest.TestCase):
    """CHB-157 AC3: `create --expedite` lands in In Progress with Highest, one at a time, for three cases only."""

    def test_lands_in_in_progress_with_highest_even_when_in_progress_is_full(self) -> None:
        fake = expedite_fake()
        code, out, err = _create_args(fake, *EXPEDITE, priority=None)

        self.assertEqual(code, 0, err)
        self.assertEqual(_write_paths(fake), [CREATE_PATH, NEW_MOVE, NEW_COMMENT, NEW_ASSIGNEE])
        self.assertEqual(fake.writes()[0][2]["fields"]["priority"], {"name": "Highest"})
        self.assertEqual(fake.writes()[1][2], {"transition": {"id": "13"}})  # In Progress on the "after" board
        (comment,) = _comments(fake, NEW_COMMENT)
        self.assertIn("Expedite: production bug: bookings fail with HTTP 500", comment)
        lines = out.splitlines()
        self.assertEqual(lines[0], "CHB-200")
        self.assertIn("CHB-200 -> In Progress (expedite). Tell the owner now:", lines[1])

    def test_a_second_open_expedite_ticket_is_refused_before_any_write(self) -> None:
        fake = expedite_fake({"In Progress": ["CHB-150"]}, priorities={"CHB-150": "Highest"})
        code, out, err = _create_args(fake, *EXPEDITE, priority=None)

        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertIn("CHB-150 is an open expedite ticket", err)
        self.assertEqual(fake.writes(), [])

    def test_a_reason_without_one_of_the_three_cases_is_refused_before_1password(self) -> None:
        for reason in ("", "  ", "the site is slow", "production bug:", "feature: add a button"):
            with self.subTest(reason):
                fake = expedite_fake()
                code, _, err = _create_args(fake, "--expedite", reason, priority=None, credentials_reader=_no_credentials)

                self.assertEqual(code, 2)
                self.assertIn("production bug:", err)
                self.assertEqual(fake.requests, [])

    def test_each_case_is_accepted(self) -> None:
        for reason in ("production bug: x", "Pipeline broken: CI on main is red", "secret leak: token in a log"):
            with self.subTest(reason):
                fake = expedite_fake()
                code, _, err = _create_args(fake, "--expedite", reason, priority=None)

                self.assertEqual(code, 0, err)

    def test_expedite_with_a_priority_is_refused(self) -> None:
        fake = expedite_fake()
        code, _, err = _create_args(fake, *EXPEDITE, priority="High", credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("--expedite sets the priority Highest", err)
        self.assertEqual(fake.requests, [])

    def test_create_without_priority_or_expedite_is_refused(self) -> None:
        fake = expedite_fake()
        code, _, err = _create_args(fake, priority=None, credentials_reader=_no_credentials)

        self.assertEqual(code, 2)
        self.assertIn("--priority", err)
        self.assertEqual(fake.requests, [])

    def test_highest_outside_expedite_is_refused_and_names_expedite(self) -> None:
        fake = FakeJira({})
        code, _, err = _run(["priority", "CHB-12", "Highest"], fake, credentials_reader=_no_credentials)
        self.assertEqual(code, 2)
        self.assertIn("create --expedite", err)
        self.assertEqual(fake.requests, [])

        fake = expedite_fake()
        code, _, err = _create_args(fake, priority="Highest", credentials_reader=_no_credentials)
        self.assertEqual(code, 2)
        self.assertIn("create --expedite", err)
        self.assertEqual(fake.requests, [])

    def test_a_failed_move_after_create_is_a_notice_and_exits_0(self) -> None:
        fake = expedite_fake()
        fake.routes[("POST", NEW_MOVE)] = 500
        code, out, _ = _create_args(fake, *EXPEDITE, priority=None)

        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "CHB-200")
        self.assertIn("could not start CHB-200", out)
        self.assertIn("Jira UI", out)
        self.assertEqual(_write_paths(fake), [CREATE_PATH, NEW_MOVE])

    def test_a_failed_comment_or_assign_after_the_move_says_the_ticket_is_in_progress(self) -> None:
        for path, command in ((NEW_COMMENT, "factory/bin/jira comment CHB-200"), (NEW_ASSIGNEE, "factory/bin/jira assign CHB-200 --me")):
            with self.subTest(path):
                fake = expedite_fake()
                fake.routes[("POST" if path == NEW_COMMENT else "PUT", path)] = 500
                code, out, _ = _create_args(fake, *EXPEDITE, priority=None)

                self.assertEqual(code, 0)
                self.assertIn("CHB-200 -> In Progress (expedite). Tell the owner now:", out)
                self.assertIn(command, out)
                self.assertNotIn("Jira UI", out)

    def test_a_second_highest_ticket_created_meanwhile_is_reported(self) -> None:
        """Two agents can both pass the one-at-a-time check; the search after the move shows it."""
        fake = expedite_fake()
        searches = [
            keys_answer({"In Progress": ["CHB-1"]})({"jql": OPEN_JQL, "fields": ["issuelinks"]}),
            keys_answer({"In Progress": ["CHB-200", "CHB-201"]}, priorities={"CHB-200": "Highest", "CHB-201": "Highest"})(
                {"jql": OPEN_JQL, "fields": ["issuelinks"]}
            ),
        ]
        fake.routes[("POST", SEARCH)] = lambda _: searches.pop(0)
        code, out, _ = _create_args(fake, *EXPEDITE, priority=None)

        self.assertEqual(code, 0)
        self.assertIn("more than one open expedite ticket: CHB-200, CHB-201", out)


if __name__ == "__main__":
    unittest.main()
