import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jira
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
        if isinstance(answer, int):
            error_body = io.BytesIO(json.dumps({"errorMessages": ["nope"], "errors": {"f": "bad"}}).encode())
            raise urllib.error.HTTPError(request.full_url, answer, "error", {}, error_body)  # type: ignore[arg-type]
        if isinstance(answer, FakeResponse):
            return answer
        return FakeResponse(b"" if answer is None else json.dumps(answer).encode())

    def writes(self) -> list[tuple[str, str, Any]]:
        return [(m, p, b) for m, p, b, _ in self.requests if m != "GET"]


def _run(argv: list[str], fake: FakeJira, credentials_reader: Any = lambda: CREDENTIALS) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, credentials_reader=credentials_reader, opener_factory=lambda: fake)
    return code, out.getvalue(), err.getvalue()


TRANSITIONS = {
    "transitions": [
        {"id": "21", "to": {"name": "In Progress"}},
        {"id": "31", "to": {"name": "In Review"}},
    ]
}


class AuthTest(unittest.TestCase):
    def test_basic_auth_with_the_agent_email_and_token(self) -> None:
        fake = FakeJira({("GET", "/rest/api/3/myself"): {"accountId": "a1"}})
        Jira(EMAIL, TOKEN, fake).myself()

        headers = fake.requests[0][3]
        expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        self.assertEqual(headers["Authorization"], f"Basic {expected}")


class TransitionTest(unittest.TestCase):
    def test_moves_by_status_name_case_insensitively(self) -> None:
        fake = FakeJira(
            {
                ("GET", "/rest/api/3/issue/CHB-12/transitions"): TRANSITIONS,
                ("POST", "/rest/api/3/issue/CHB-12/transitions"): None,
            }
        )
        code, out, _ = _run(["transition", "CHB-12", "in review"], fake)

        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [("POST", "/rest/api/3/issue/CHB-12/transitions", {"transition": {"id": "31"}})])
        self.assertEqual(out.strip(), "CHB-12 -> In Review")

    def test_unknown_status_fails_and_lists_the_allowed_ones(self) -> None:
        fake = FakeJira({("GET", "/rest/api/3/issue/CHB-12/transitions"): TRANSITIONS})
        code, _, err = _run(["transition", "CHB-12", "Done"], fake)

        self.assertEqual(code, 1)
        self.assertIn("In Progress, In Review", err)
        self.assertEqual(fake.writes(), [])

    def test_hook_runs_before_the_move_and_can_block_it(self) -> None:
        """CHB-108 puts the WIP check in before_transition."""
        fake = FakeJira({("GET", "/rest/api/3/issue/CHB-12/transitions"): TRANSITIONS})
        hook = mock.Mock(side_effect=JiraError("WIP limit reached"))
        with mock.patch.object(jira, "before_transition", hook):
            code, _, err = _run(["transition", "CHB-12", "In Progress"], fake)

        self.assertEqual(code, 1)
        self.assertEqual(hook.call_args.args[1:], ("CHB-12", "In Progress"))
        self.assertIn("WIP limit reached", err)
        self.assertEqual(fake.writes(), [])


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

    def test_empty_body_is_refused(self) -> None:
        fake = FakeJira({})
        with tempfile.NamedTemporaryFile("w") as body:
            code, _, _ = _run(["comment", "CHB-12", "--body-file", body.name], fake)

        self.assertEqual(code, 1)
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


class CreateTest(unittest.TestCase):
    def _fake(self, parent_type: str) -> FakeJira:
        return FakeJira(
            {
                ("GET", "/rest/api/3/issue/CHB-27?fields=issuetype"): {"fields": {"issuetype": {"name": parent_type}}},
                ("POST", "/rest/api/3/issue"): {"key": "CHB-200"},
            }
        )

    def _create(self, fake: FakeJira) -> tuple[int, str, str]:
        with tempfile.NamedTemporaryFile("w") as description:
            description.write("Line one.\n\nAC1: works.")
            description.flush()
            argv = ["create", "--parent", "CHB-27", "--summary", "Do it", "--description-file", description.name]
            return _run(argv, fake)

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
        self.assertEqual(adf_to_text(fields["description"]), "Line one.\n\nAC1: works.")

    def test_empty_summary_is_refused_before_any_request(self) -> None:
        fake = self._fake("Epic")
        with tempfile.NamedTemporaryFile("w") as description:
            argv = ["create", "--parent", "CHB-27", "--summary", "  ", "--description-file", description.name]
            code, _, err = _run(argv, fake)

        self.assertEqual(code, 1)
        self.assertIn("summary is empty", err)
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


class ReadCredentialsTest(unittest.TestCase):
    def test_email_and_token_come_from_the_same_1password_item(self) -> None:
        commands: list[list[str]] = []

        def reader(command: list[str]) -> str:
            commands.append(command)
            return "agent@example.com" if "label=username" in command else "tok"

        self.assertEqual(read_credentials(reader), ("agent@example.com", "tok"))
        username, token = commands
        self.assertEqual(username, ["op", "item", "get", "chb-jira-claude.api-token", "--fields", "label=username"])
        self.assertEqual(token[:5], username[:5])
        self.assertIn("label=credential", token)

    def test_a_failed_username_read_stops_before_the_token(self) -> None:
        commands: list[list[str]] = []

        def reader(command: list[str]) -> str:
            commands.append(command)
            raise CredentialError("locked")

        with self.assertRaises(CredentialError):
            read_credentials(reader)
        self.assertEqual(len(commands), 1)


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
                "assignee": {"displayName": "Claude (CHB agent)"},
                "parent": {"key": "CHB-27"},
                "description": doc,
                "comment": {"comments": [{"author": {"displayName": "Owner"}, "created": "t", "body": doc}]},
            },
        }
        path = "/rest/api/3/issue/CHB-12?fields=summary,status,assignee,issuetype,parent,description,comment"
        code, out, _ = _run(["view", "CHB-12"], FakeJira({("GET", path): issue}))

        self.assertEqual(code, 0)
        for text in ("CHB-12 [Task] Title", "Status: In Review", "Assignee: Claude (CHB agent)", "Parent: CHB-27", "--- Owner"):
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


class ReadTokenTest(unittest.TestCase):
    def test_returns_the_stripped_output(self) -> None:
        self.assertEqual(read_secret(["echo", "tok"]), "tok")

    def test_failing_or_empty_command_is_a_credential_error(self) -> None:
        for command in (["false"], ["true"], ["/nonexistent/op"]):
            with self.assertRaises(CredentialError):
                read_secret(command)

    def test_empty_field_is_named_in_the_error(self) -> None:
        with self.assertRaisesRegex(CredentialError, "field label=username is empty"):
            read_secret(["true", "--fields", "label=username"])


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

    def test_two_matching_headings_exit_1_without_a_write(self) -> None:
        current = _doc(_heading(HEADING), _para("a"), _heading(HEADING), _para("b"))
        fake = FakeJira({("GET", DESCRIPTION_PATH): {"fields": {"description": current}}})
        code, _, err = self._section(fake)

        self.assertEqual(code, 1)
        self.assertIn("2 sections", err)
        self.assertEqual(fake.writes(), [])


if __name__ == "__main__":
    unittest.main()
