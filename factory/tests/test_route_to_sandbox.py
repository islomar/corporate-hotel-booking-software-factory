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
    "make test-backend",
    "make test-frontend",
    "make lint-backend",
    "make test-scripts",
    "make lint-frontend",
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
    "python -mpytest tests/x.py",
    "python3 -I -mpip install x",
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
    "cd backend && .devcontainer/run npm ci",
    "cd $X && .devcontainer/run npm ci",
    "cd backend; ./.devcontainer/run make check",
    "env -S 'npm ci'",
    "exec -a x npm ci",
    "timeout --foreground 5 npm ci",
    "nice --unknown-option 5 make check",
]

ALLOWED = [
    ".devcontainer/run make check",
    "./.devcontainer/run make check",
    "timeout 900 .devcontainer/run make check",
    "cd /home/u/repo/.claude/worktrees/CHB-9 && .devcontainer/run make check",
    "cd .claude/worktrees/CHB-9 && ./.devcontainer/run make check",
    "/home/u/repo/.claude/worktrees/CHB-9/.devcontainer/run make check",
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


CWD = "/home/u/repo"
LAUNCHERS = {"/home/u/repo/.devcontainer/run", "/home/u/repo/.claude/worktrees/CHB-9/.devcontainer/run"}


class ReasonTest(unittest.TestCase):
    def test_third_party_code_on_the_host_is_denied_with_the_sandbox_command(self):
        for command in DENIED:
            with self.subTest(command=command):
                why = hook.reason(command, CWD, LAUNCHERS)
                self.assertIsNotNone(why)
                self.assertIn(".devcontainer/run", why)

    def test_the_sandbox_launcher_and_host_only_commands_are_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(hook.reason(command, CWD, LAUNCHERS))

    def test_a_launcher_is_trusted_only_in_a_worktree_of_this_repository(self):
        self.assertIsNotNone(hook.reason(".devcontainer/run npm ci", CWD, set()))

    def test_the_trusted_launchers_are_those_of_the_worktrees_git_lists(self):
        listing = "worktree /home/u/repo\nHEAD abc\n\nworktree /home/u/repo/.claude/worktrees/CHB-9\nHEAD def\n"
        self.assertEqual(hook.launchers_from_worktree_list(listing), LAUNCHERS)


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

    def test_the_launcher_of_this_checkout_passes_through_the_stdin_contract(self):
        top = str(Path(__file__).resolve().parents[2])
        payload = {"cwd": top, "tool_input": {"command": f"{top}/.devcontainer/run make check"}}
        self.assertEqual(self.run_hook(json.dumps(payload)), (0, ""))
        payload = {"cwd": f"{top}/backend", "tool_input": {"command": ".devcontainer/run make check"}}
        self.assertEqual(self.run_hook(json.dumps(payload))[0], 2)

    def test_input_that_is_not_json_is_denied(self):
        code, _ = self.run_hook("not json")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
