"""CHB-209: the Stop hook that sends the agent back to an agent PR with untriaged review findings."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory" / "hooks"))
sys.path.insert(0, str(ROOT / "factory"))

import check_untriaged_reviews as hook
import triage_review as tr

SHA = "a" * 40
FINDING = "- **improvement** (Tests) backend/tests/test\\_x.py:1: The assertion is loose. Fix: Assert 400.\n"
COMMENTS_PATH = f"repos/{tr.REPOSITORY}/issues/{{n}}/comments?per_page=100"
PULLS_PATH = f"repos/{tr.REPOSITORY}/pulls?state=open&per_page=100"


def review(sha: str = SHA, findings: str = FINDING) -> dict:
    body = f"**Fail**\n\nSummary: x.\n\n{findings}\n<!-- chb-review reviewed:{sha} -->\n"
    return {"id": 1, "user": {"login": tr.REVIEWER}, "body": body}


def triage(sha: str = SHA) -> dict:
    return {"id": 2, "user": {"login": tr.AGENT}, "body": f"Triage of review {sha}\n\n<!-- chb-triage reviewed:{sha} -->\n"}


def pull(number: int, *, login: str = tr.AGENT, state: str = "open", sha: str = SHA) -> dict:
    return {"number": number, "state": state, "user": {"login": login}, "head": {"sha": sha}}


def fake_gh(pulls: list[dict], comments: dict[int, list[dict]]):
    """A `gh` that answers `gh api --paginate <path> --jq .[]` with one JSON item per line."""
    calls: list[list[str]] = []

    def gh(args: list[str]) -> str:
        calls.append(args)
        path = next(a for a in args if a.startswith("repos/"))
        if path == PULLS_PATH:
            items = pulls
        else:
            number = int(path.split("/issues/")[1].split("/")[0])
            items = comments.get(number, [])
        return "".join(json.dumps(item) + "\n" for item in items)

    gh.calls = calls  # type: ignore[attr-defined]
    return gh


def run(gh, state_dir: str, session: str = "s1") -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = hook.main(io.StringIO(json.dumps({"session_id": session})), run_gh=gh, state_folder=state_dir)
    return code, err.getvalue()


class UntriagedReviewsTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.state_dir = str(Path(self.state.name) / "state")

    def test_agent_pr_with_one_untriaged_finding_blocks_naming_the_pr_and_the_command(self):
        gh = fake_gh([pull(140)], {140: [review()]})
        code, err = run(gh, self.state_dir)
        self.assertEqual(code, 2)
        self.assertIn("PR #140", err)
        self.assertIn("factory/bin/triage-review 140", err)
        self.assertIn("factory/bin/triage-review 140 --file FILE", err)
        self.assertIn("1 finding", err)

    def test_triage_comment_for_the_head_sha_does_not_block(self):
        gh = fake_gh([pull(140)], {140: [review(), triage()]})
        self.assertEqual(run(gh, self.state_dir), (0, ""))

    def test_review_without_findings_does_not_block(self):
        gh = fake_gh([pull(140)], {140: [review(findings="")]})
        self.assertEqual(run(gh, self.state_dir), (0, ""))

    def test_pr_by_another_author_does_not_block_and_is_not_read(self):
        gh = fake_gh([pull(141, login="islomar"), pull(142, login="dependabot[bot]")], {141: [review()], 142: [review()]})
        self.assertEqual(run(gh, self.state_dir), (0, ""))
        self.assertEqual(len(gh.calls), 1)

    def test_closed_pr_does_not_block(self):
        gh = fake_gh([pull(140, state="closed")], {140: [review()]})
        self.assertEqual(run(gh, self.state_dir), (0, ""))

    def test_pr_without_review_does_not_block(self):
        gh = fake_gh([pull(140)], {140: []})
        self.assertEqual(run(gh, self.state_dir), (0, ""))

    def test_lists_every_untriaged_pr(self):
        gh = fake_gh([pull(140), pull(139), pull(138)], {140: [review()], 139: [review()], 138: [review(), triage()]})
        code, err = run(gh, self.state_dir)
        self.assertEqual(code, 2)
        self.assertIn("PR #139", err)
        self.assertIn("PR #140", err)
        self.assertNotIn("PR #138", err)

    def test_reads_the_repository_by_path_not_by_the_current_folder(self):
        gh = fake_gh([pull(140)], {140: [review()]})
        run(gh, self.state_dir)
        self.assertIn(PULLS_PATH, gh.calls[0])
        self.assertIn(COMMENTS_PATH.format(n=140), gh.calls[1])

    def test_api_error_lets_the_turn_end_with_a_warning(self):
        errors = [
            subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
            subprocess.TimeoutExpired(["gh"], 6),
            FileNotFoundError("gh"),
            ValueError("not JSON"),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):

                def gh(args: list[str], error=error) -> str:
                    raise error

                code, err = run(gh, self.state_dir)
                self.assertEqual(code, 0)
                self.assertIn("Untriaged review check skipped", err)

    def test_api_error_on_one_pr_comments_lets_the_turn_end(self):
        def gh(args: list[str]) -> str:
            if PULLS_PATH in args:
                return json.dumps(pull(140)) + "\n"
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

        code, err = run(gh, self.state_dir)
        self.assertEqual(code, 0)
        self.assertIn("Untriaged review check skipped", err)

    def test_second_stop_with_the_same_findings_passes(self):
        gh = fake_gh([pull(140)], {140: [review()]})
        self.assertEqual(run(gh, self.state_dir)[0], 2)
        code, err = run(gh, self.state_dir)
        self.assertEqual(code, 0)
        self.assertIn("allowing the stop", err)

    def test_a_new_review_refuses_again(self):
        self.assertEqual(run(fake_gh([pull(140)], {140: [review()]}), self.state_dir)[0], 2)
        newer = fake_gh([pull(140, sha="c" * 40)], {140: [review(), review(sha="c" * 40)]})
        self.assertEqual(run(newer, self.state_dir)[0], 2)

    def test_input_that_is_not_json_allows_the_stop(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = hook.main(io.StringIO("not json"), run_gh=fake_gh([pull(140)], {140: [review()]}), state_folder=self.state_dir)
        self.assertEqual(code, 0)


    def test_review_without_a_readable_sha_blocks_like_the_check(self):
        unreadable = {"id": 1, "user": {"login": tr.REVIEWER}, "body": f"{FINDING}<!-- chb-review reviewed:not-a-sha -->"}
        code, err = run(fake_gh([pull(140)], {140: [unreadable]}), self.state_dir)
        self.assertEqual(code, 2)
        self.assertIn("no readable reviewed sha", err)

    def test_refuse_then_triage_then_new_findings_refuses_again(self):
        self.assertEqual(run(fake_gh([pull(140)], {140: [review()]}), self.state_dir)[0], 2)
        self.assertEqual(run(fake_gh([pull(140)], {140: [review(), triage()]}), self.state_dir), (0, ""))
        self.assertEqual(run(fake_gh([pull(140)], {140: [review()]}), self.state_dir)[0], 2)

    def test_unusable_state_folder_lets_the_turn_end_with_a_warning(self):
        blocker = Path(self.state.name) / "a-file"
        blocker.write_text("")
        code, err = run(fake_gh([pull(140)], {140: [review()]}), str(blocker))
        self.assertEqual(code, 0)
        self.assertIn("state folder unusable", err)

    def test_the_env_var_sets_the_state_folder(self):
        folder = str(Path(self.state.name) / "from-env")
        with unittest.mock.patch.dict(os.environ, {hook.STATE_DIR_ENV: folder}):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = hook.main(io.StringIO(json.dumps({"session_id": "s9"})), run_gh=fake_gh([pull(140)], {140: [review()]}))
        self.assertEqual(code, 2)
        self.assertTrue((Path(folder) / "s9.json").is_file())


class DeadlineTest(unittest.TestCase):
    def test_a_call_after_the_deadline_raises_without_running_gh(self):
        now = [100.0]
        gh = hook.make_gh(total=20, clock=lambda: now[0])
        now[0] = 120.0
        with unittest.mock.patch.object(hook.subprocess, "run") as run_mock:
            with self.assertRaises(TimeoutError):
                gh(["api", "x"])
        run_mock.assert_not_called()

    def test_a_call_gets_at_most_the_time_left(self):
        now = [100.0]
        gh = hook.make_gh(total=20, clock=lambda: now[0])
        now[0] = 117.0
        with unittest.mock.patch.object(hook.subprocess, "run") as run_mock:
            run_mock.return_value.stdout = ""
            gh(["api", "x"])
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 3.0)

    def test_the_deadline_error_lets_the_turn_end(self):
        with tempfile.TemporaryDirectory() as folder:
            code, err = run(hook.make_gh(total=0), folder)
        self.assertEqual(code, 0)
        self.assertIn("TimeoutError", err)


class SettingsTest(unittest.TestCase):
    def test_settings_register_the_hook_on_stop(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        commands = [h["command"] for group in settings["hooks"]["Stop"] for h in group["hooks"]]
        self.assertTrue(any("factory/hooks/check_untriaged_reviews.py" in c for c in commands))


if __name__ == "__main__":
    unittest.main()
