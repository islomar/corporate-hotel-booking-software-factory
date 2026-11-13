import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rerun_failed_supply_chain
from rerun_failed_supply_chain import failed_runs_to_rerun, main

HEAD = "a" * 40
OTHER = "b" * 40


def run(run_id, conclusion="failure", head=HEAD, event="pull_request", status="completed"):
    return {
        "id": run_id,
        "conclusion": conclusion,
        "head_sha": head,
        "event": event,
        "status": status,
    }


class SelectionTest(unittest.TestCase):
    def test_picks_failed_runs_of_the_head_except_the_current_run(self):
        runs = [
            run(1),  # failed pull_request run: the case of PR #103
            run(2, event="pull_request_review"),  # the current run
            run(3, event="pull_request_review"),  # failed review run (earlier dismissal)
        ]
        self.assertEqual(failed_runs_to_rerun(runs, HEAD, current_run_id=2), [1, 3])

    def test_ignores_other_heads_and_non_failures(self):
        runs = [
            run(1, head=OTHER),
            run(2, conclusion="success"),
            run(3, conclusion="cancelled"),
            run(4, conclusion=None, status="in_progress"),
            run(5, event="push"),
        ]
        self.assertEqual(failed_runs_to_rerun(runs, HEAD, current_run_id=9), [])


class MainTest(unittest.TestCase):
    def call(self, runs, post_error=None):
        posted = []

        def post(path, token):
            posted.append(path)
            if post_error:
                raise OSError(post_error)

        argv = ["--repo", "o/r", "--head", HEAD, "--run-id", "2"]
        with (
            mock.patch.dict("os.environ", {"GH_TOKEN": "t"}),
            mock.patch.object(
                rerun_failed_supply_chain, "github_get", lambda path, token: {"workflow_runs": runs}
            ),
            mock.patch.object(rerun_failed_supply_chain, "github_post", post),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = main(argv)
        return code, posted

    def test_reruns_the_failed_pull_request_run(self):
        code, posted = self.call([run(1), run(2, event="pull_request_review")])
        self.assertEqual(code, 0)
        self.assertEqual(posted, ["/repos/o/r/actions/runs/1/rerun-failed-jobs"])

    def test_nothing_to_rerun(self):
        code, posted = self.call([run(2, event="pull_request_review")])
        self.assertEqual((code, posted), (0, []))

    def test_a_rerun_error_warns_and_does_not_fail(self):
        code, posted = self.call([run(1)], post_error="Resource not accessible by integration")
        self.assertEqual((code, len(posted)), (0, 1))

    def test_missing_token_is_a_configuration_error(self):
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = main(["--repo", "o/r", "--head", HEAD, "--run-id", "2"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
