"""CHB-162: the Stop hook that sends the agent back to a board below its minimums."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
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


def issue(key: str, status: str, *, kind: str = "Task", priority: str = "Medium", blocked_by: str | None = None) -> dict:
    links = [{"type": {"name": "Blocks"}, "inwardIssue": {"key": blocked_by}}] if blocked_by else []
    return {
        "key": key,
        "fields": {
            "status": {"id": status},
            "issuetype": {"name": kind},
            "priority": {"name": priority},
            "issuelinks": links,
            "description": None,
        },
    }


def board(issues: list[dict]):
    return lambda: (list(COLUMNS), issues)


def run(message="Done.", read=None, state="", session="s1", active=False):
    event = {"last_assistant_message": message, "session_id": session, "stop_hook_active": active}
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main(io.StringIO(json.dumps(event)), read=read, state_dir=state)
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
        issues = [issue("CHB-7", "2", blocked_by="CHB-8"), issue("CHB-8", "1"), issue("CHB-9", "2", kind="Epic")]
        code, _, err = run(message="Ready is low, nothing in Backlog has a trigger.", read=board(issues), state=self.state)
        self.assertEqual((code, err), (0, ""))

    def test_allows_when_started_work_is_at_its_maximum(self):
        issues = [issue("CHB-1", "4"), issue("CHB-2", "4"), issue("CHB-3", "4"), issue("CHB-7", "2"), issue("CHB-8", "2"), issue("CHB-9", "2")]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 0)

    def test_epics_do_not_count_as_in_progress(self):
        issues = [issue("CHB-1", "3", kind="Epic")] + [issue(f"CHB-{n}", "2") for n in (7, 8, 9)]
        self.assertEqual(run(read=board(issues), state=self.state)[0], 2)


class ReadyBelowMinimum(StateCase):
    ISSUES = [issue("CHB-1", "3"), issue("CHB-7", "2")]

    def test_refuses_a_reply_without_the_scan(self):
        code, _, err = run(read=board(self.ISSUES), state=self.state)
        self.assertEqual(code, 2)
        self.assertIn("Ready is below its minimum (1/3)", err)
        self.assertIn("Ready is low", err)

    def test_allows_a_reply_that_reports_the_scan(self):
        message = "Ready is low (1/3): CHB-50 has a trigger and moved to Ready."
        self.assertEqual(run(message=message, read=board(self.ISSUES), state=self.state)[0], 0)

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


class Wiring(unittest.TestCase):
    def test_settings_run_the_hook_on_stop(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        commands = [h["command"] for group in settings["hooks"]["Stop"] for h in group["hooks"]]
        self.assertTrue(any("check_wip_minimums.py" in c for c in commands))

    def test_script_runs_isolated_and_fails_open_on_bad_input(self):
        result = subprocess.run([sys.executable, "-I", str(HOOK)], input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)

    def test_kanban_names_the_hook_in_the_min_rules(self):
        text = (ROOT / "docs" / "process" / "kanban.md").read_text()
        self.assertGreaterEqual(text.count("check_wip_minimums.py"), 2)


if __name__ == "__main__":
    unittest.main()
