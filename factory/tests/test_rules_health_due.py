"""CHB-167: the SessionStart hook that says when the rules health check is due."""

import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import rules_health_due as due

NOW = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def issue(summary: str, created: str) -> dict:
    return {"key": "CHB-1", "fields": {"summary": summary, "created": created}}


class LastRun(unittest.TestCase):
    def test_no_ticket_means_never(self):
        self.assertIsNone(due.last_run([]))

    def test_newest_ticket_whose_summary_starts_with_the_prefix(self):
        issues = [
            issue("Weekly rules health check: duplicates", "2026-11-19T10:00:00.000+0100"),
            issue("Rules health check 2026-11-01", "2026-11-01T10:00:00.000+0100"),
            issue("rules health check 2026-11-12", "2026-11-12T10:30:00.000+0100"),
        ]
        self.assertEqual(due.last_run(issues), datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc))

    def test_unparseable_date_raises(self):
        with self.assertRaises(ValueError):
            due.last_run([issue("Rules health check x", "yesterday")])


class DueLine(unittest.TestCase):
    def test_never_run_is_due(self):
        self.assertIn("never ran", due.due_line(None, NOW))

    def test_seven_days_is_due_and_names_date_and_procedure(self):
        line = due.due_line(datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc), NOW)
        self.assertIn("2026-11-13", line)
        self.assertIn("7 days", line)
        self.assertIn("docs/process/rules-health.md", line)

    def test_six_days_is_not_due(self):
        self.assertIsNone(due.due_line(datetime(2026, 11, 14, 9, 30, tzinfo=timezone.utc), NOW))


class Main(unittest.TestCase):
    def run_hook(self, event: str, issues, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        code = due.main(io.StringIO(event), now, lambda: issues, out, err)
        return code, out.getvalue(), err.getvalue()

    def test_due_prints_session_start_context(self):
        code, out, _ = self.run_hook('{"source": "startup"}', [])
        self.assertEqual(code, 0)
        body = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "SessionStart")
        self.assertIn("Rules health check is due", body["additionalContext"])

    def test_not_due_prints_nothing(self):
        recent = [issue("Rules health check 2026-11-18", "2026-11-18T10:00:00.000+0000")]
        self.assertEqual(self.run_hook('{"source": "startup"}', recent)[:2], (0, ""))

    def test_other_sources_print_nothing_and_read_nothing(self):
        def boom():
            raise AssertionError("read")

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(due.main(io.StringIO('{"source": "resume"}'), NOW, boom, out, err), 0)
        self.assertEqual(out.getvalue(), "")

    def test_reader_failure_fails_open_with_one_line(self):
        def fail():
            raise RuntimeError("secret detail")

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(due.main(io.StringIO("{}"), NOW, fail, out, err), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "Rules health check skipped: RuntimeError\n")

    def test_bad_stdin_still_exits_0(self):
        self.assertEqual(self.run_hook("not json", [])[0], 0)


if __name__ == "__main__":
    unittest.main()
