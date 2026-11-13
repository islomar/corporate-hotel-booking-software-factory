import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import route_to_sandbox as hook

DENIED = [
    "make check",
    "make test",
    "make -C /home/u/repo check",
    "make check-backend",
    "make format",
    "cd backend && uv run --locked pytest tests/test_x.py",
    "uv sync --locked",
    "uvx ruff check .",
    "cd frontend && npm ci",
    "npm install left-pad",
    "npm test -- src/App.test.tsx",
    "npx vitest run src/x.test.ts",
    "pytest tests/test_x.py",
    "python3 -m pytest -k slow",
    "python3 -m pip install requests",
    "pip install requests",
    "rtk npm ci",
    "FOO=1 uv run pytest x.py",
    "echo start; npm ci",
    "git status && make check",
    "timeout 600 npm ci",
    "timeout -s KILL 10m make check",
    "nice -n 5 npm ci",
    "env -u HOME npm ci",
    "stdbuf -oL make check",
    "xargs -n 1 npm install < pkgs.txt",
    "sudo -u agent npm ci",
    "setsid uv sync",
    "/tmp/x/.devcontainer/run npm ci",
    "other/.devcontainer/run make check",
]

ALLOWED = [
    ".devcontainer/run make check",
    "./.devcontainer/run make check",
    "timeout 900 .devcontainer/run make check",
    "/home/u/repo/.devcontainer/run bash -c 'cd frontend && npm ci && npm test'",
    "rtk .devcontainer/run uv run pytest tests/test_x.py",
    "make",
    "make help",
    "make dev",
    "make e2e",
    "make clean-dev",
    "python3 -I -m unittest discover -s factory/tests",
    "python3 -I scripts/check_release_age.py backend/uv.lock",
    "git commit -m 'make check passes'",
    "echo npm ci",
    "grep -n 'uv sync' Makefile",
    "factory/bin/as-agent git push",
    "terraform -chdir=infra/gcp plan",
]


class ReasonTest(unittest.TestCase):
    def test_third_party_code_on_the_host_is_denied_with_the_sandbox_command(self):
        for command in DENIED:
            with self.subTest(command=command):
                why = hook.reason(command, top="/home/u/repo")
                self.assertIsNotNone(why)
                self.assertIn(".devcontainer/run", why)

    def test_the_sandbox_launcher_and_host_only_commands_are_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(hook.reason(command, top="/home/u/repo"))

    def test_an_absolute_launcher_path_needs_a_known_checkout(self):
        self.assertIsNotNone(hook.reason("/home/u/repo/.devcontainer/run npm ci", top=None))


class MainTest(unittest.TestCase):
    def run_hook(self, payload: str) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stderr(err):
            code = hook.main()
        return code, err.getvalue()

    def test_denied_command_exits_2_with_the_reason(self):
        code, err = self.run_hook(json.dumps({"tool_input": {"command": "npm ci"}}))
        self.assertEqual(code, 2)
        self.assertIn(".devcontainer/run npm ci", err)

    def test_allowed_command_exits_0_silently(self):
        code, err = self.run_hook(json.dumps({"tool_input": {"command": "git status"}}))
        self.assertEqual((code, err), (0, ""))

    def test_input_that_is_not_json_is_denied(self):
        code, _ = self.run_hook("not json")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
