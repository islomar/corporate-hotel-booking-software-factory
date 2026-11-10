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


COMMENT_POST = 'gh api -X POST "repos/$REPO/issues/$PR_NUMBER/comments" --input "$comment.json"'
COMMENT_JSON = re.compile(r"^\s*(jq -n --rawfile body .*)$", re.MULTILINE)


class ReviewCommentTest(unittest.TestCase):
    """CHB-141: the owner reacts to a PR comment; factory/review/agreement.py counts the reactions."""

    def setUp(self) -> None:
        self.text = WORKFLOW.read_text()

    def test_gate_writes_the_comment_and_a_later_step_posts_it(self) -> None:
        self.assertIn('--comment-out "$RUNNER_TEMP/comment.md"', self.text)
        self.assertEqual(self.text.count(COMMENT_POST), 1)
        self.assertLess(self.text.index(POST), self.text.index(COMMENT_POST))
        self.assertIn('comment="$RUNNER_TEMP/comment.md"', self.text)

    def test_the_comment_never_blocks_the_check(self) -> None:
        token_step = self.text[self.text.index("- id: comment-token") :]
        self.assertIn("continue-on-error: true", token_step.split("- name: Post the review comment")[0])
        self.assertIn("permission-pull-requests: write", token_step)
        post_step = self.text[self.text.index("- name: Post the review comment") :]
        self.assertIn("continue-on-error: true", post_step)
        # The check-run token keeps pull requests read.
        check_token = self.text[self.text.index("- id: app-token") : self.text.index("- id: comment-token")]
        self.assertIn("permission-pull-requests: read", check_token)

    def test_the_comment_follows_a_posted_check_of_a_model_review(self) -> None:
        self.assertIn("- id: gate\n        name: Gate and post the review check", self.text)
        token_step = self.text[self.text.index("- id: comment-token") :].split("\n")[1]
        self.assertIn("steps.gate.outcome == 'success'", token_step)
        self.assertIn("steps.plan.outputs.action == 'review'", token_step)
        post_step = self.text[self.text.index("- name: Post the review comment") :].split("\n")[1]
        self.assertIn("steps.comment-token.outputs.token != ''", post_step)

    @unittest.skipUnless(shutil.which("jq"), "jq is not installed")
    def test_the_jq_line_sends_the_file_as_the_body(self) -> None:
        line = COMMENT_JSON.search(self.text).group(1)  # type: ignore[union-attr]
        body = 'Pass "quoted" `code` $HOME\n\nOwner: react 👍\n<!-- chb-review reviewed:abc -->\n'
        with tempfile.TemporaryDirectory() as tmp:
            comment = Path(tmp) / "comment.md"
            comment.write_text(body, encoding="utf-8")
            env = {"comment": str(comment), "PATH": "/usr/bin:/bin"}
            subprocess.run(["bash", "-euc", line], check=True, env=env)  # noqa: S603,S607
            result = json.loads(Path(f"{comment}.json").read_text(encoding="utf-8"))

        self.assertEqual(result, {"body": body})


if __name__ == "__main__":
    unittest.main()
