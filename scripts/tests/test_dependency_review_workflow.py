"""Structure of .github/workflows/dependency-review.yml (CHB-188).

The tests read the file as text: `python3 -I` has no YAML parser, and the checks need none.
"""

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/dependency-review.yml"


def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


class DependencyReviewWorkflowTest(unittest.TestCase):
    def test_runs_on_every_pull_request(self):
        self.assertRegex(text(), re.compile(r"^on:\n  pull_request:\n", re.MULTILINE))

    def test_workflow_token_reads_contents_only(self):
        body = text()
        self.assertRegex(body, re.compile(r"^permissions: \{\}$", re.MULTILINE))
        job_permissions = re.findall(r"^    permissions:\n((?:      .*\n)+)", body, re.MULTILINE)
        self.assertEqual(job_permissions, ["      contents: read\n"])

    def test_every_action_is_pinned_to_a_full_commit_sha_with_its_version(self):
        uses = re.findall(r"uses: (\S+)(.*)", text())
        self.assertIn("actions/dependency-review-action", [ref.split("@")[0] for ref, _ in uses])
        for ref, rest in uses:
            with self.subTest(ref=ref):
                self.assertRegex(ref, r"@[0-9a-f]{40}$")
                self.assertRegex(rest, r"^ # v\d+\.\d+\.\d+$")

    def test_fails_on_high_severity_and_above(self):
        self.assertRegex(text(), re.compile(r"^          fail-on-severity: high$", re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
