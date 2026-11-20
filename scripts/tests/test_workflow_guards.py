"""Workflows that must not run in forks match the repository ID, not its name (CHB-218).

CHB-204 moves the repository to the organization `islomar-software-garden`. A guard on the
full name would skip these jobs after the move. The ID stays the same across a transfer.
GitHub documents `github.repository_id` as a string, so the guard compares it with a string.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"
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


if __name__ == "__main__":
    unittest.main()
