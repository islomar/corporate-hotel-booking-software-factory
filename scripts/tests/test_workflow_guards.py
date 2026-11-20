"""Workflows that must not run in forks match the repository ID, not its name (CHB-218).

CHB-204 moves the repository to the organization `islomar-software-garden`. A guard on the
full name would skip these jobs after the move. The ID stays the same across a transfer.
GitHub documents `github.repository_id` as a string, so the guard compares it with a string.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"
GUARDED = ("pr-updates.yml", "dependabot-auto-merge.yml")
GUARD = "github.repository_id == '1447565814'"


class RepositoryGuardTest(unittest.TestCase):
    def test_guards_match_the_repository_id(self):
        for name in GUARDED:
            with self.subTest(workflow=name):
                self.assertIn(GUARD, (WORKFLOWS / name).read_text())

    def test_no_guard_matches_the_full_name(self):
        for name in GUARDED:
            with self.subTest(workflow=name):
                self.assertIsNone(re.search(r"github\.repository\s*==", (WORKFLOWS / name).read_text()))


def _one(pattern: str, path: Path) -> str:
    found = set(re.findall(pattern, path.read_text(), re.MULTILINE))
    if len(found) != 1:
        raise AssertionError(f"{path.relative_to(ROOT)}: expected one value for {pattern!r}, found {sorted(found)}")
    return found.pop()


class CopiesAgreeTest(unittest.TestCase):
    """CHB-218 review: the human's login and the repository ID are copied across files; they must not drift."""

    def test_the_human_login_is_the_same_everywhere(self):
        workflow = _one(r"^\s*OWNER:\s*(\S+)\s*$", WORKFLOWS / "supply-chain.yml")
        script = _one(r'^HUMAN_LOGIN = "([^"]+)"', ROOT / "factory/review/agreement.py")
        codeowners = _one(r"@([A-Za-z0-9-]+)", ROOT / ".github/CODEOWNERS")

        self.assertEqual({workflow, codeowners}, {script})

    def test_the_repository_id_is_the_same_in_every_guard(self):
        ids = {_one(r"github\.repository_id == '(\d+)'", WORKFLOWS / name) for name in GUARDED}

        self.assertEqual(ids, {"1447565814"})


if __name__ == "__main__":
    unittest.main()
