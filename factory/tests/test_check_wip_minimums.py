"""CHB-162: the Stop hook that sends the agent back to a board below its minimums."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_wip_minimums as hook
import jira

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "factory" / "hooks" / "check_wip_minimums.py"

COLUMNS = (
    jira.Column("Backlog", frozenset({"1"})),
    jira.Column("Ready", frozenset({"2"}), min=3, max=5),
    jira.Column("In Progress", frozenset({"3"}), min=1, max=3),
    jira.Column("In Review", frozenset({"4"}), max=3),
    jira.Column("Validating", frozenset({"5"}), max=3),
)


WHY = "## Why it matters\n\nThe owner waits for the thing."
CRITERIA = "## Acceptance criteria\n\n- A test shows the thing."
ALL_BOXES = tuple(box.name for box in jira.DOR_BOXES)
# A Ready ticket the start accepts (CHB-187): Why it matters, every judgment box ticked, acceptance criteria.
COMPLETE = jira.text_to_adf(f"{WHY}\n\n## Definition of Ready\n\n{jira.checklist_markdown(ALL_BOXES)}\n\n{CRITERIA}")


def issue(
    key: str, status: str, *, kind: str = "Task", priority: str = "Medium", blocked_by: str | None = None, description: object = COMPLETE
) -> dict:
    links = [{"type": {"name": "Blocks"}, "inwardIssue": {"key": blocked_by}}] if blocked_by else []
    return {
        "key": key,
        "fields": {
            "status": {"id": status},
            "issuetype": {"name": kind},
            "priority": {"name": priority},
            "issuelinks": links,
            "description": description,
        },
    }


def board(issues: list[dict]):
    return lambda: (list(COLUMNS), issues)


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)  # 13:00 in Madrid, day 2026-11-10
SECTION = "Acceptance criteria (Validating)"


def validating_issue(key: str, *whens: str, section: bool = True, status: str = "5", kind: str = "Task") -> dict:
    """A Validating ticket whose section holds one "When:" line per argument."""
    content = [{"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Why it matters"}]}]
    if section:
        items = [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"When: {w}"}]}]}
            for w in whens
        ]
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": SECTION}]})
        content.append({"type": "bulletList", "content": items})
    found = issue(key, status, kind=kind)
    found["fields"]["description"] = {"type": "doc", "version": 1, "content": content}
    return found


def comments_at(*times: str):
    """read_comments seam: the agent account's comment times for every ticket, and the keys asked about."""
    asked: list[str] = []

    def read(key: str):
        asked.append(key)
        return [datetime.fromisoformat(t) for t in times]

    read.asked = asked  # type: ignore[attr-defined]
    return read


NO_PULLS = {"data": {"repository": {"merged": {"nodes": []}, "open": {"nodes": []}}}}


def pulls(merged=(), opened=()):
    """read_pulls seam: merged PRs as (number, branch) and open PRs as (number, branch)."""
    return lambda: {
        "data": {
            "repository": {
                "merged": {"nodes": [{"number": n, "title": f"PR {n}", "headRefName": b, "mergedAt": "2026-11-11T09:00:00Z"} for n, b in merged]},
                "open": {"nodes": [{"number": n, "title": f"PR {n}", "headRefName": b} for n, b in opened]},
            }
        }
    }


def run(message="Done.", read=None, state="", session="s1", active=False, read_comments=None, now=NOW, read_pulls=lambda: NO_PULLS):
    event = {"last_assistant_message": message, "session_id": session, "stop_hook_active": active}
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main(io.StringIO(json.dumps(event)), read=read, state_dir=state, read_comments=read_comments, now=now, read_pulls=read_pulls)
    return code, out.getvalue(), err.getvalue()


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")


class InProgressBelowMinimum(StateCase):
    def test_refuses_and_names_the_ticket_to_start(self):
        issues = [issue("CHB-9", "2", priority="Low"), issue("CHB-7", "2", priority="High")] + [issue(f"CHB-{n}", "2") for n in (20, 21)]
        code, _, err = run(read=board(issues), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("In Progress is below its minimum (0/1)", err)
        self.assertIn("CHB-7", err)
        self.assertIn('factory/bin/jira transition CHB-7 "In Progress"', err)

    def test_allows_when_in_progress_has_its_minimum(self):
        issues = [issue("CHB-1", "3")] + [issue(f"CHB-{n}", "2") for n in (7, 8, 9)]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 0)

    def test_allows_when_ready_has_nothing_startable(self):
        issues = [issue(f"CHB-{n}", "2", blocked_by="CHB-8") for n in (7, 10, 11)] + [issue("CHB-8", "1"), issue("CHB-9", "2", kind="Epic")]
        code, _, err = run(read=board(issues), state=self.state)
        self.assertEqual((code, err), (0, ""))

    def test_allows_when_started_work_is_at_its_maximum(self):
        issues = [issue("CHB-1", "4"), issue("CHB-2", "4"), issue("CHB-3", "4"), issue("CHB-7", "2"), issue("CHB-8", "2"), issue("CHB-9", "2")]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 0)

    def test_epics_do_not_count_as_in_progress(self):
        issues = [issue("CHB-1", "3", kind="Epic")] + [issue(f"CHB-{n}", "2") for n in (7, 8, 9)]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 2)


class StartAcceptance(StateCase):
    """CHB-187: the hook names only a ticket the start accepts, and lists what the others lack."""

    def test_a_top_ticket_without_acceptance_criteria_is_not_named_as_startable(self):
        bare = jira.text_to_adf(f"{WHY}\n\n## Definition of Ready\n\n{jira.checklist_markdown(ALL_BOXES)}")
        issues = [issue("CHB-44", "2", priority="High", description=bare)] + [issue(f"CHB-{n}", "2") for n in (20, 21)]
        code, _, err = run(read=board(issues), state=self.state)
        self.assertEqual(code, 2)
        self.assertNotIn("has a ticket that can start", err)
        self.assertNotIn("Start it now", err)
        self.assertIn("no Ready ticket can start", err)
        self.assertIn("CHB-44 lacks acceptance criteria", err)

    def test_the_second_ticket_is_not_named_while_the_top_one_has_a_gap(self):
        """A start takes the top ticket only, so a complete ticket below a gapped one cannot start."""
        bare = jira.text_to_adf(WHY)
        issues = [issue("CHB-44", "2", priority="High", description=bare), issue("CHB-45", "2")] + [issue(f"CHB-{n}", "2") for n in (20, 21)]
        _, _, err = run(read=board(issues), state=self.state)
        self.assertNotIn("Start it now", err)
        self.assertIn("CHB-44 lacks acceptance criteria and Definition of Ready (no section)", err)
        self.assertNotIn("CHB-45 lacks", err)

    def test_unticked_boxes_are_named(self):
        some = jira.text_to_adf(f"{WHY}\n\n## Definition of Ready\n\n{jira.checklist_markdown(('why', 'unblocked', 'acceptance'))}\n\n{CRITERIA}")
        issues = [issue("CHB-44", "2", priority="High", description=some)] + [issue(f"CHB-{n}", "2") for n in (20, 21)]
        _, _, err = run(read=board(issues), state=self.state)
        self.assertIn("CHB-44 lacks Definition of Ready (unticked: trigger, decision, small)", err)

    def test_a_complete_top_ticket_is_still_named(self):
        issues = [issue("CHB-7", "2", priority="High")] + [issue(f"CHB-{n}", "2") for n in (20, 21)]
        code, _, err = run(read=board(issues), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn('Start it now: factory/bin/jira transition CHB-7 "In Progress"', err)

    def test_the_hook_and_the_start_agree(self):
        """Every ticket the hook calls startable passes start_gaps, the check the start makes."""
        for description in (COMPLETE, jira.text_to_adf(WHY), None):
            with self.subTest(description=description):
                issues = [issue("CHB-7", "2", description=description)]
                top, _ = jira.startable(issues, COLUMNS)
                self.assertEqual(top == "CHB-7", jira.start_gaps("CHB-7", description) == [])


class ReadyBelowMinimum(StateCase):
    """CHB-164: the Ready minimum is a quota. No reply text passes it."""

    ISSUES = [issue("CHB-1", "3"), issue("CHB-7", "2")]

    def test_refuses_and_lists_the_candidates(self):
        issues = self.ISSUES + [issue("CHB-30", "1", priority="Low"), issue("CHB-31", "1", priority="High")]
        code, _, err = run(read=board(issues), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("Ready is below its minimum (1/3)", err)
        self.assertIn('factory/bin/jira ready CHB-<n> --trigger "5. Ready is below its minimum (1/3)"', err)
        self.assertLess(err.index("CHB-31 (High)"), err.index("CHB-30 (Low)"))

    def test_ready_is_low_in_the_reply_no_longer_passes(self):
        messages = ("Ready is low, nothing in Backlog has a trigger.", "Ready is low (1/3): CHB-50 moved to Ready.")
        for session, message in enumerate(messages):
            with self.subTest(message):
                code, _, err = run(message=message, read=board(self.ISSUES), state=self.state, session=str(session))
                self.assertEqual(code, 2)
                self.assertIn("Ready is below its minimum (1/3)", err)

    def test_says_so_when_backlog_has_no_candidate(self):
        _, _, err = run(read=board(self.ISSUES), state=self.state)
        self.assertIn("no Backlog ticket can meet the Definition of Ready", err)

    def test_allows_the_second_stop_for_the_same_board(self):
        self.assertEqual(run(read=board(self.ISSUES), state=self.state)[0], 2)
        code, _, err = run(read=board(self.ISSUES), state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("same board", err)

    def test_allows_once_ready_is_back_at_its_minimum(self):
        issues = self.ISSUES + [issue("CHB-8", "2"), issue("CHB-9", "2")]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 0)

    def test_allows_when_ready_has_its_minimum(self):
        issues = [issue("CHB-1", "3")] + [issue(f"CHB-{n}", "2") for n in (7, 8, 9)]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 0)

    def test_both_findings_in_one_reason(self):
        issues = [issue("CHB-7", "2")]
        _, _, err = run(read=board(issues), state=self.state)
        self.assertIn("In Progress is below", err)
        self.assertIn("Ready is below", err)

    def test_columns_without_a_minimum_are_never_checked(self):
        columns = [jira.Column(c.name, c.status_ids, None, c.max) for c in COLUMNS]
        self.assertEqual(run(read=lambda: (columns, []), state=self.state)[0], 0)


class FailOpen(StateCase):
    def test_unreachable_jira_allows_with_one_line(self):
        def read():
            raise jira.JiraError("HTTP 503 from Jira")

        code, out, err = run(read=read, state=self.state)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("HTTP 503 from Jira", err)

    def test_missing_credentials_allow(self):
        def read():
            raise jira.CredentialError("no service account token in the keyring")

        code, _, err = run(read=read, state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("no service account token", err)

    def test_unexpected_error_names_only_its_type(self):
        def read():
            raise RuntimeError("token abc123")

        code, _, err = run(read=read, state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("RuntimeError", err)
        self.assertNotIn("abc123", err)

    def test_bad_input_allows(self):
        for raw in ("", "not json", "[]"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(hook.main(io.StringIO(raw), read=board([]), state_dir=self.state), 0)


class NoLoop(StateCase):
    ISSUES = [issue("CHB-7", "2")]

    def test_second_refusal_for_the_same_board_is_allowed(self):
        self.assertEqual(run(read=board(self.ISSUES), state=self.state)[0], 2)
        code, _, err = run(read=board(self.ISSUES), state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("same board", err)

    def test_a_changed_board_is_refused_again(self):
        self.assertEqual(run(read=board(self.ISSUES), state=self.state)[0], 2)
        changed = [issue("CHB-7", "2"), issue("CHB-8", "2")]
        self.assertEqual(run(read=board(changed), state=self.state)[0], 2)

    def test_sessions_do_not_share_the_count(self):
        self.assertEqual(run(read=board(self.ISSUES), state=self.state, session="a")[0], 2)
        self.assertEqual(run(read=board(self.ISSUES), state=self.state, session="b")[0], 2)

    def test_a_clean_board_forgets_the_refusal(self):
        self.assertEqual(run(read=board(self.ISSUES), state=self.state)[0], 2)
        clean = [issue("CHB-1", "3")] + [issue(f"CHB-{n}", "2") for n in (7, 8, 9)]
        self.assertEqual(run(read=board(clean), state=self.state)[0], 0)
        self.assertEqual(run(read=board(self.ISSUES), state=self.state)[0], 2)

    def test_unwritable_state_folder_allows(self):
        blocker = os.path.join(self.tmp.name, "file")
        Path(blocker).write_text("x")
        code, _, err = run(read=board(self.ISSUES), state=os.path.join(blocker, "sub"))
        self.assertEqual(code, 0)
        self.assertEqual(err.count("\n"), 1)


class ValidatingDue(StateCase):
    """CHB-163: a Validating ticket with a due item and no agent comment today blocks the stop."""

    OK = [issue(f"CHB-{n}", "2") for n in (20, 21, 22)] + [issue("CHB-8", "3")]  # no WIP finding

    def test_refuses_and_names_the_ticket_and_the_due_item(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 the Dependabot run shows green", "2026-12-01 later")]
        code, _, err = run(read=board(issues), read_comments=comments_at(), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("CHB-140", err)
        self.assertIn("2026-11-09 the Dependabot run shows green", err)
        self.assertNotIn("2026-12-01", err)

    def test_each_ticket_and_each_due_item_is_named(self):
        issues = self.OK + [
            validating_issue("CHB-140", "2026-11-09 first", "2026-11-10 second"),
            validating_issue("CHB-141", "2026-11-08 third"),
        ]
        code, _, err = run(read=board(issues), read_comments=comments_at(), state=self.state)
        self.assertEqual(code, 2)
        for text in ("CHB-140", "CHB-141", "first", "second", "third"):
            self.assertIn(text, err)

    def test_a_comment_by_the_agent_today_passes(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 due")]
        code, _, err = run(read=board(issues), read_comments=comments_at("2026-11-10T09:30:00+01:00"), state=self.state)
        self.assertEqual((code, err), (0, ""))

    def test_the_day_is_the_madrid_day(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 due")]
        late = NOW.replace(hour=23, minute=30)  # 00:30 on 2026-11-11 in Madrid
        yesterday_evening = "2026-11-10T22:00:00+00:00"  # 23:00 on 2026-11-10 in Madrid
        code, _, _ = run(read=board(issues), read_comments=comments_at(yesterday_evening), state=self.state, now=late)
        self.assertEqual(code, 2)
        code, _, _ = run(read=board(issues), read_comments=comments_at(yesterday_evening), state=self.state, now=NOW, session="s2")
        self.assertEqual(code, 0)

    def test_a_comment_from_an_earlier_day_does_not_pass(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 due")]
        code, _, _ = run(read=board(issues), read_comments=comments_at("2026-11-09T23:00:00+01:00"), state=self.state)
        self.assertEqual(code, 2)

    def test_items_not_yet_due_never_block(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-11 tomorrow", "2026-11-10 14:00 Europe/Madrid this afternoon")]
        comments = comments_at()
        code, _, err = run(read=board(issues), read_comments=comments, state=self.state)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(comments.asked, [])

    def test_an_item_with_no_date_is_not_due(self):
        issues = self.OK + [validating_issue("CHB-140", "after the next deploy")]
        code, _, _ = run(read=board(issues), read_comments=comments_at(), state=self.state)
        self.assertEqual(code, 0)

    def test_tickets_without_a_section_and_other_columns_are_ignored(self):
        issues = self.OK + [
            validating_issue("CHB-140", section=False),
            validating_issue("CHB-141", "2026-11-09 due", status="3"),
            validating_issue("CHB-142", "2026-11-09 due", kind="Epic"),
        ]
        code, _, _ = run(read=board(issues), read_comments=comments_at(), state=self.state)
        self.assertEqual(code, 0)

    def test_comments_are_read_only_for_tickets_with_a_due_item(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 due"), validating_issue("CHB-141", "2026-12-01 later")]
        comments = comments_at()
        run(read=board(issues), read_comments=comments, state=self.state)
        self.assertEqual(comments.asked, ["CHB-140"])

    def test_the_second_stop_for_the_same_state_is_allowed(self):
        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 due")]
        first = run(read=board(issues), read_comments=comments_at(), state=self.state)
        second = run(read=board(issues), read_comments=comments_at(), state=self.state)
        self.assertEqual(first[0], 2)
        self.assertEqual(second[0], 0)
        self.assertIn("allowing the stop", second[2])

    def test_a_new_due_item_is_refused_again(self):
        one = self.OK + [validating_issue("CHB-140", "2026-11-09 due")]
        two = one + [validating_issue("CHB-141", "2026-11-08 due")]
        run(read=board(one), read_comments=comments_at(), state=self.state)
        self.assertEqual(run(read=board(two), read_comments=comments_at(), state=self.state)[0], 2)

    def test_comment_read_failure_allows_with_one_line(self):
        def broken(key):
            raise jira.JiraError("HTTP 503 from Jira")

        issues = self.OK + [validating_issue("CHB-140", "2026-11-09 due")]
        code, _, err = run(read=board(issues), read_comments=broken, state=self.state)
        self.assertEqual(code, 0)
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("HTTP 503 from Jira", err)

    def test_comment_read_failure_keeps_the_wip_finding(self):
        def broken(key):
            raise jira.JiraError("HTTP 503 from Jira")

        issues = [validating_issue("CHB-140", "2026-11-09 due")]  # Ready and In Progress are empty
        code, _, err = run(read=board(issues), read_comments=broken, state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("Ready is below its minimum", err)

    def test_both_findings_in_one_reason(self):
        issues = [validating_issue("CHB-140", "2026-11-09 due")]
        code, _, err = run(read=board(issues), read_comments=comments_at(), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("Ready is below its minimum", err)
        self.assertIn("CHB-140", err)


class InReviewMerged(StateCase):
    """CHB-179: an In Review ticket whose PR is merged blocks the stop once."""

    BASE = [issue("CHB-1", "3")] + [issue(f"CHB-{n}", "2") for n in (7, 8, 9)]

    def test_refuses_and_names_the_ticket_and_the_pr(self):
        issues = self.BASE + [issue("CHB-170", "4")]
        code, _, err = run(read=board(issues), read_pulls=pulls(merged=[(105, "CHB-170-contributing")]), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("CHB-170 is in In Review but PR #105 merged into main", err)
        self.assertIn('factory/bin/jira transition CHB-170 "Validating"', err)

    def test_the_second_stop_is_allowed(self):
        issues = self.BASE + [issue("CHB-170", "4")]
        read_pulls = pulls(merged=[(105, "CHB-170-contributing")])
        self.assertEqual(run(read=board(issues), read_pulls=read_pulls, state=self.state)[0], 2)
        code, _, err = run(read=board(issues), read_pulls=read_pulls, state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("allowing the stop", err)

    def test_an_open_pr_never_blocks(self):
        issues = self.BASE + [issue("CHB-170", "4")]
        read_pulls = pulls(merged=[(105, "CHB-170-a")], opened=[(110, "CHB-170-b")])
        self.assertEqual(run(read=board(issues), read_pulls=read_pulls, state=self.state)[0], 0)

    def test_no_pr_found_never_blocks(self):
        issues = self.BASE + [issue("CHB-170", "4")]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 0)

    def test_a_merged_pr_of_a_ticket_in_another_column_never_blocks(self):
        issues = self.BASE + [issue("CHB-170", "5")]
        self.assertEqual(run(read=board(issues), read_pulls=pulls(merged=[(105, "CHB-170-a")]), state=self.state)[0], 0)

    def test_an_epic_in_review_is_ignored(self):
        issues = self.BASE + [issue("CHB-170", "4", kind="Epic")]
        self.assertEqual(run(read=board(issues), read_pulls=pulls(merged=[(105, "CHB-170-a")]), state=self.state)[0], 0)

    def test_github_is_not_read_without_an_in_review_ticket(self):
        def boom():
            raise AssertionError("GitHub must not be read")

        self.assertEqual(run(read=board(self.BASE), read_pulls=boom, state=self.state)[0], 0)

    def test_a_github_failure_prints_one_line_and_allows(self):
        def fail():
            raise hook.validating_sweep.SweepError("gh exited 4")

        issues = self.BASE + [issue("CHB-170", "4")]
        code, _, err = run(read=board(issues), read_pulls=fail, state=self.state)
        self.assertEqual(code, 0)
        self.assertIn("In Review check skipped: gh exited 4", err)


class AgentCommentTimes(unittest.TestCase):
    """The real read: Jira.agent_comment_times keeps only the agent account's comments."""

    def test_filters_by_author_and_parses_times(self):
        answers = {
            "/rest/api/3/myself": {"accountId": "me"},
            "/rest/api/3/issue/CHB-1/comment?orderBy=-created&maxResults=100": {
                "comments": [
                    {"author": {"accountId": "me"}, "created": "2026-11-10T16:30:04.324+0100"},
                    {"author": {"accountId": "owner"}, "created": "2026-11-10T17:00:00.000+0100"},
                ]
            },
        }

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self):
                return json.dumps(self.body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        def opener(request):
            return Response(answers[request.full_url.removeprefix(jira.SITE)])

        times = jira.Jira("a@example.com", "t", opener).agent_comment_times("CHB-1")
        self.assertEqual(times, [datetime.fromisoformat("2026-11-10T16:30:04.324+01:00")])


class Wiring(unittest.TestCase):
    def test_settings_run_the_hook_on_stop(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        commands = [h["command"] for group in settings["hooks"]["Stop"] for h in group["hooks"]]
        self.assertTrue(any("check_wip_minimums.py" in c for c in commands))

    def test_script_runs_isolated_and_fails_open_on_bad_input(self):
        result = subprocess.run([sys.executable, "-I", str(HOOK)], input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_kanban_and_readme_describe_the_validating_check(self):
        for path in (ROOT / "docs" / "process" / "kanban.md", ROOT / "factory" / "README.md"):
            self.assertIn("CHB-163", path.read_text(), str(path))

    def test_kanban_names_the_hook_in_the_min_rules(self):
        text = (ROOT / "docs" / "process" / "kanban.md").read_text()
        self.assertGreaterEqual(text.count("check_wip_minimums.py"), 2)


if __name__ == "__main__":
    unittest.main()
