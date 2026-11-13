import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy_event import author_type, build_event, main

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/deploy.yml"
SHA = "a" * 40
PR = {"number": 42, "merged_at": "2026-11-10T10:00:00Z", "merge_commit_sha": SHA, "user": {"login": "chb-factory-agent[bot]"}}
COMMITS = [
    {"commit": {"author": {"date": "2026-11-09T09:00:00Z"}, "committer": {"date": "2026-11-10T08:00:00Z"}}},
    {"commit": {"author": {"date": "2026-11-08T07:30:00Z"}, "committer": {"date": "2026-11-10T08:05:00Z"}}},
]
ENV = {
    "GITHUB_REPOSITORY": "owner/repo",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "99",
    "COMMIT_SHA": SHA,
    "DEPLOY_START": "2026-11-10T10:05:00Z",
    "DEPLOY_END": "2026-11-10T10:08:00Z",
    "DEPLOY_RESULT": "success",
    "SMOKE_RESULT": "success",
    "GCP_PROJECT": "proj",
}


class AuthorTypeTest(unittest.TestCase):
    def test_the_factory_agent_is_agent(self):
        self.assertEqual(author_type("chb-factory-agent[bot]"), "agent")

    def test_dependabot_is_dependabot(self):
        self.assertEqual(author_type("dependabot[bot]"), "dependabot")

    def test_anyone_else_is_human(self):
        self.assertEqual(author_type("islomar"), "human")

    def test_no_login_is_unknown(self):
        self.assertEqual(author_type(None), "unknown")


class BuildEventTest(unittest.TestCase):
    def test_event_carries_every_field(self):
        event = build_event(ENV, PR, COMMITS)
        self.assertEqual(
            event,
            {
                "event": "deployment",
                "schema": 1,
                "environment": "production",
                "commit_sha": SHA,
                "pr_number": 42,
                "first_commit_time": "2026-11-08T07:30:00Z",
                "merge_time": "2026-11-10T10:00:00Z",
                "deploy_start": "2026-11-10T10:05:00Z",
                "deploy_end": "2026-11-10T10:08:00Z",
                "deploy_result": "success",
                "smoke_result": "success",
                "author_type": "agent",
                "run_url": "https://github.com/owner/repo/actions/runs/99",
            },
        )

    def test_without_a_pull_request_the_pr_fields_are_null(self):
        event = build_event(ENV, None, [])
        self.assertIsNone(event["pr_number"])
        self.assertIsNone(event["first_commit_time"])
        self.assertIsNone(event["merge_time"])
        self.assertEqual(event["author_type"], "unknown")

    def test_empty_results_become_skipped(self):
        event = build_event({**ENV, "SMOKE_RESULT": ""}, PR, COMMITS)
        self.assertEqual(event["smoke_result"], "skipped")


def target(args):
    """The API path of a `gh api` call: the argument before `--input -`, else the last one."""
    return args[args.index("--input") - 1] if "--input" in args else args[-1]


class Gh:
    """Fake command runner. Records every call; `fail` names a command word that raises."""

    def __init__(self, fail=None, pulls=None):
        self.calls = []
        self.fail = fail
        self.pulls = [PR] if pulls is None else pulls

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        if self.fail and self.fail in args:
            raise subprocess.CalledProcessError(1, args, stderr="boom")
        joined = target(args)
        if joined.endswith(f"commits/{SHA}/pulls"):
            return json.dumps(self.pulls)
        if "pulls/42/commits" in joined:
            return json.dumps([COMMITS])
        if joined.endswith("/deployments"):
            return json.dumps({"id": 7})
        return ""


class MainTest(unittest.TestCase):
    def run_main(self, runner, env=ENV):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(env, runner)
        return code, out.getvalue()

    def test_writes_one_log_entry_and_one_deployment_status(self):
        gh = Gh()
        code, out = self.run_main(gh)
        self.assertEqual(code, 0)
        self.assertNotIn("::warning", out)
        [log] = [c for c in gh.calls if c[0][0] == "gcloud"]
        self.assertEqual(log[0][:4], ["gcloud", "logging", "write", "chb-deploy-events"])
        self.assertIn("--project=proj", log[0])
        self.assertIn("--payload-type=json", log[0])
        self.assertEqual(json.loads(log[0][4])["pr_number"], 42)
        [create] = [c for c in gh.calls if target(c[0]).endswith("/deployments")]
        body = json.loads(create[1])
        self.assertEqual((body["ref"], body["environment"], body["required_contexts"]), (SHA, "production", []))
        [status] = [c for c in gh.calls if target(c[0]).endswith("/deployments/7/statuses")]
        status_body = json.loads(status[1])
        self.assertEqual(status_body["state"], "success")
        self.assertFalse(status_body["auto_inactive"])
        self.assertLessEqual(len(status_body["description"]), 140)

    def test_failed_smoke_is_a_failure_status(self):
        gh = Gh()
        self.run_main(gh, {**ENV, "SMOKE_RESULT": "failure"})
        [status] = [c for c in gh.calls if target(c[0]).endswith("/statuses")]
        self.assertEqual(json.loads(status[1])["state"], "failure")

    def test_failed_log_write_warns_once_and_exits_zero(self):
        gh = Gh(fail="gcloud")
        code, out = self.run_main(gh)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("::warning"), 1)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(any(target(c[0]).endswith("/statuses") for c in gh.calls), "GitHub write still runs")

    def test_both_writes_failing_still_prints_one_warning_line(self):
        def always(args, stdin=None):
            joined = " ".join(args)
            if joined.endswith("/pulls"):
                return json.dumps([PR])
            if "pulls/42/commits" in joined:
                return json.dumps([COMMITS])
            raise subprocess.CalledProcessError(1, args, stderr="boom")

        code, out = self.run_main(always)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_unexpected_errors_never_fail_the_deploy(self):
        def explode(args, stdin=None):
            raise RuntimeError("anything")

        code, out = self.run_main(explode)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertTrue(out.startswith("::warning"))

    def test_missing_variables_warn_instead_of_failing(self):
        code, out = self.run_main(Gh(), {})
        self.assertEqual(code, 0)
        self.assertEqual(out.count("::warning"), 1)


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.before, self.job = self.text.split("\n  record:\n")

    def test_workflow_only_calls_the_script(self):
        self.assertIn("python3 -I scripts/deploy_event.py", self.job)

    def test_the_event_step_cannot_fail_the_deploy(self):
        self.assertIn("continue-on-error: true", self.job)

    def test_the_record_job_runs_after_deploy_and_smoke_even_when_they_fail(self):
        self.assertIn("needs: [gate, deploy, smoke]", self.job)
        self.assertIn("always()", self.job)

    def test_only_the_record_job_can_write_deployments(self):
        self.assertIn("deployments: write", self.job)
        self.assertIn("id-token: write", self.job)
        self.assertNotIn("deployments: write", self.before)


if __name__ == "__main__":
    unittest.main()
