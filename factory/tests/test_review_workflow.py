"""The review check links to the workflow run that produced it (CHB-131).

Without `details_url`, GitHub's "View more details on chb-factory-reviewer" opens the App's
homepage, which is the repository. review.yml posts every review check (review, skip, carry,
fallback failure) through one `gh api -X POST .../check-runs`, so the URL is set right before it.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/review.yml"
POST = 'gh api -X POST "repos/$REPO/check-runs" --input "$payload"'
SET_URL = re.compile(r"^\s*(jq --arg url \"\$run_url\" .*)$", re.MULTILINE)


class DetailsUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()

    def test_the_url_is_set_right_before_the_only_post(self) -> None:
        self.assertEqual(self.text.count(POST), 1)
        set_url = SET_URL.search(self.text)
        self.assertIsNotNone(set_url, "review.yml must set details_url with jq")
        self.assertLess(set_url.start(), self.text.index(POST))
        self.assertIn('run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"', self.text)

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_the_jq_line_adds_the_run_url_and_keeps_the_payload(self) -> None:
        line = SET_URL.search(self.text).group(1)  # type: ignore[union-attr]
        payload = {"name": "review", "head_sha": "abc", "output": {"title": "Pass", "summary": "ok"}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "check-run.json"
            path.write_text(json.dumps(payload))
            env = {"payload": str(path), "run_url": "https://github.com/o/r/actions/runs/7", "PATH": "/usr/bin:/bin"}
            subprocess.run(["bash", "-euc", line], check=True, env=env)  # noqa: S603,S607
            result = json.loads(path.read_text())

        self.assertEqual(result, {**payload, "details_url": "https://github.com/o/r/actions/runs/7"})


if __name__ == "__main__":
    unittest.main()
