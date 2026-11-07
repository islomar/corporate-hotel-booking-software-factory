"""Git-based tests never touch the repository a git hook runs in.

A commit from a linked worktree runs its hooks with GIT_DIR and GIT_INDEX_FILE set.
`git -C <tmp> init` then reinitialises the outer repository instead of <tmp> (CHB-124).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_sandbox import isolate_from_outer_repo

TESTS = Path(__file__).resolve().parent


def clean_git(*args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], check=True, capture_output=True, env=env)  # noqa: S603,S607


class HookEnvironmentTest(unittest.TestCase):
    """Runs the git-based test modules as a worktree pre-commit hook would."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.outer = Path(tmp.name) / "outer"
        clean_git("init", "-q", "-b", "main", str(self.outer))
        self.config = (self.outer / ".git/config").read_bytes()

    def run_module(self, tests, pattern):
        env = {
            **os.environ,
            "GIT_DIR": str(self.outer / ".git"),
            "GIT_INDEX_FILE": str(self.outer / ".git/index"),
        }
        command = [sys.executable, "-I", "-m", "unittest", "discover", "-s", str(tests), "-p", pattern]
        return subprocess.run(command, capture_output=True, text=True, env=env, check=False)  # noqa: S603

    def test_supply_chain_tests_leave_the_outer_repo_alone(self):
        result = self.run_module(TESTS, "test_check_supply_chain.py")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.outer / ".git/config").read_bytes(), self.config)

    def test_review_plan_tests_read_their_own_repo(self):
        result = self.run_module(TESTS.parents[1] / "factory/tests", "test_review_plan.py")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.outer / ".git/config").read_bytes(), self.config)


class IsolateFromOuterRepoTest(unittest.TestCase):
    def test_removes_repository_variables_for_the_test_only(self):
        outer = {"GIT_DIR": "/elsewhere/.git", "GIT_INDEX_FILE": "/elsewhere/.git/index", "GIT_WORK_TREE": "/elsewhere"}
        with mock.patch.dict(os.environ, outer):
            case = unittest.TestCase()
            isolate_from_outer_repo(case)

            self.assertFalse(set(outer) & set(os.environ))
            case.doCleanups()
            self.assertEqual({k: os.environ[k] for k in outer}, outer)

    def test_ignores_global_and_system_config(self):
        case = unittest.TestCase()
        isolate_from_outer_repo(case)
        self.addCleanup(case.doCleanups)

        self.assertEqual((os.environ["GIT_CONFIG_GLOBAL"], os.environ["GIT_CONFIG_NOSYSTEM"]), (os.devnull, "1"))


if __name__ == "__main__":
    unittest.main()
