import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_full_test_suite as hook

ROOT = Path(__file__).resolve().parents[2]

DENIED = [
    "pytest",
    "pytest -q",
    "pytest -x -vv",
    "pytest .",
    "pytest -m slow",
    "py.test",
    "uv run pytest",
    "uv run --locked pytest -q",
    "cd backend && uv run --locked pytest",
    "cd /repo/backend && uv run --project . pytest --tb=short",
    "python -m pytest",
    "python3 -I -m pytest -q",
    "rtk pytest",
    "rtk proxy uv run pytest",
    "npx vitest run",
    "npx vitest",
    "npx --yes vitest run --reporter verbose",
    "npm exec vitest run",
    "vitest run",
    "npm test",
    "npm t",
    "npm run test",
    "npm run -s test",
    "npm --prefix frontend test",
    "cd frontend && npm test -- --reporter verbose",
    "python3 -m unittest discover",
    "python3 -I -m unittest discover -v",
    "python3 -m unittest discover -s .",
    "python3 -m unittest",
    "python3 -m unittest -v",
    "git status && uv run pytest",
]

ALLOWED = [
    "make check",
    "make test",
    "make -C /repo check",
    "pytest tests/test_booking.py",
    "pytest tests/test_booking.py::test_books_a_room",
    "pytest -q tests/",
    "pytest -k booking",
    "pytest -kbooking",
    "pytest --lf",
    "uv run pytest tests/test_booking.py -q",
    "cd backend && uv run --locked pytest tests/acceptance",
    "python -m pytest -k 'books and not cancels'",
    "rtk pytest tests/test_x.py",
    "npx vitest run src/App.test.tsx",
    "npx vitest run -t 'shows the hotel'",
    "npx vitest run --changed",
    "npm test -- src/App.test.tsx",
    "npm run test -- src/App.test.tsx",
    "npm test -- -t 'shows the hotel'",
    "python3 -m unittest discover -s factory/tests",
    "python3 -I -m unittest discover -s factory/tests -p 'test_deny_*.py'",
    "python3 -m unittest discover -p 'test_jira.py'",
    "python3 -m unittest discover factory/tests",
    "python3 -m unittest factory/tests/test_jira.py",
    "python3 -m unittest test_jira.JiraTest",
    "python3 -m unittest -k transition",
    "python3 factory/tests/test_jira.py",
    "npm run lint",
    "npm run test:watch",
    "npm ci",
    "uv run ruff check .",
    "echo pytest",
    "git commit -m 'run pytest before merging'",
    "cat <<'EOF' > /tmp/msg\ndon't run pytest\nEOF",
    "grep -rn pytest backend/pyproject.toml",
    "factory/bin/as-agent git push -u origin CHB-54-guardrail-hooks",
    "gh pr checks 67 --watch",
]


class MatcherTest(unittest.TestCase):
    def test_bare_full_suite_runs_are_denied(self) -> None:
        for command in DENIED:
            with self.subTest(command):
                self.assertIsNotNone(hook.reason(command))

    def test_related_test_runs_and_other_commands_are_allowed(self) -> None:
        for command in ALLOWED:
            with self.subTest(command):
                self.assertIsNone(hook.reason(command))

    def test_reason_names_the_related_test_form_and_make_check(self) -> None:
        why = hook.reason("uv run pytest") or ""

        self.assertIn("full suite", why)
        self.assertIn("pytest tests/test_x.py", why)
        self.assertIn("-k", why)
        self.assertIn("npx vitest run src/x.test.ts", why)
        self.assertIn("-p 'test_x.py'", why)
        self.assertIn("make check", why)


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


def _event(command: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}})


class ContractTest(unittest.TestCase):
    """The Claude Code hook contract: exit 2 with the reason on stderr denies the tool call."""

    def test_denied_command_exits_2_with_reason_on_stderr(self) -> None:
        code, out, err = _main(_event("uv run pytest"))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Blocked", err)

    def test_allowed_command_exits_0_silently(self) -> None:
        self.assertEqual(_main(_event("make check")), (0, "", ""))

    def test_input_without_a_command_is_left_alone(self) -> None:
        self.assertEqual(_main(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}))[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_unreadable_input_is_denied(self) -> None:
        self.assertEqual(_main("not json")[0], 2)

    def test_an_error_in_the_matcher_denies(self) -> None:
        with mock.patch.object(hook, "reason", side_effect=IndexError("boom")):
            code, _, err = _main(_event("ls"))

        self.assertEqual(code, 2)
        self.assertIn("hook failed (IndexError)", err)

    def test_project_settings_register_the_hook_on_bash(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        (group,) = [g for g in settings["hooks"]["PreToolUse"] if "deny_full_test_suite.py" in g["hooks"][0]["command"]]

        self.assertEqual(group["matcher"], "Bash")
        self.assertIn("python3 -I", group["hooks"][0]["command"])
        self.assertIn("factory/hooks/deny_full_test_suite.py", group["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
