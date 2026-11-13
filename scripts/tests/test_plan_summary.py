import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plan_summary import MARKER, StateError, error_lines, failure_comment, main, state_variables, summarize

ROOT = Path(__file__).resolve().parents[2]
CANARY = "canary-value-9f3a"
EMAIL = "owner@example.org"


def change(address, *actions, before=None, after=None):
    return {
        "address": address,
        "type": address.split(".")[0],
        "change": {"actions": list(actions), "before": before, "after": after},
    }


PLAN = {
    "format_version": "1.2",
    "resource_changes": [
        change("google_project_service.run", "no-op"),
        change("google_service_account.new", "create", after={"display_name": CANARY}),
        change("google_cloud_run_v2_service.chb", "update", before={"x": CANARY}, after={"x": "other"}),
        change("google_billing_budget.monthly", "delete", before={"billing_account": CANARY}),
        change("google_monitoring_notification_channel.email", "delete", "create", before={"labels": {"e": EMAIL}}),
        change('google_project_iam_member.planner["roles/viewer"]', "create"),
        change("data.google_project.p", "read"),
    ],
    "output_changes": {"leak": {"actions": ["create"], "after": CANARY}},
    "variables": {"alert_email": {"value": EMAIL}},
    "prior_state": {"values": {"root_module": {"resources": [{"values": {"billing_account": CANARY}}]}}},
}
STATE = {
    "values": {
        "root_module": {
            "resources": [
                {"address": "google_billing_budget.monthly", "values": {"billing_account": "AAAAAA-BBBBBB-CCCCCC"}},
                {
                    "address": "google_monitoring_notification_channel.email",
                    "values": {"labels": {"email_address": EMAIL}},
                },
            ]
        }
    }
}


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self.text = summarize(PLAN, sha="abc1234", run_url="https://example.invalid/run/1")

    def test_counts_follow_terraform_with_a_replace_in_add_and_destroy(self):
        self.assertIn("3 to add, 1 to change, 2 to destroy", self.text)

    def test_every_changed_address_is_listed_with_its_action(self):
        self.assertIn("`google_service_account.new` | create", self.text)
        self.assertIn("`google_cloud_run_v2_service.chb` | update", self.text)
        self.assertIn("`google_billing_budget.monthly` | delete", self.text)
        self.assertIn("`google_monitoring_notification_channel.email` | replace", self.text)
        self.assertIn('`google_project_iam_member.planner["roles/viewer"]` | create', self.text)

    def test_no_op_and_read_are_not_listed(self):
        self.assertNotIn("google_project_service.run", self.text)
        self.assertNotIn("data.google_project.p", self.text)

    def test_no_value_from_the_plan_reaches_the_summary(self):
        self.assertNotIn(CANARY, self.text)
        self.assertNotIn(EMAIL, self.text)
        self.assertNotIn("leak", self.text)

    def test_the_comment_starts_with_the_marker_that_finds_it_again(self):
        self.assertTrue(self.text.startswith(MARKER))

    def test_names_the_commit_and_the_run(self):
        self.assertIn("abc1234", self.text)
        self.assertIn("https://example.invalid/run/1", self.text)

    def test_a_plan_without_changes_says_so(self):
        text = summarize({"resource_changes": [change("a.b", "no-op")]}, sha="abc1234", run_url="u")
        self.assertIn("0 to add, 0 to change, 0 to destroy", text)
        self.assertIn("No changes", text)

    def test_a_plan_without_resource_changes_is_empty(self):
        self.assertIn("0 to add", summarize({}, sha="abc1234", run_url="u"))

    def test_an_unknown_action_is_shown_as_it_is_and_counted_nowhere(self):
        text = summarize({"resource_changes": [change("a.b", "forget")]}, sha="s", run_url="u")
        self.assertIn("`a.b` | forget", text)
        self.assertIn("0 to add, 0 to change, 0 to destroy", text)

    def test_an_address_cannot_break_the_markdown_table(self):
        text = summarize({"resource_changes": [change("a.b|c`d\nx", "create")]}, sha="s", run_url="u")
        self.assertNotIn("a.b|c", text)
        self.assertNotIn("`d", text)

    def test_a_long_list_is_cut_and_says_how_many_are_left(self):
        many = [change(f"a.r{i}", "create") for i in range(150)]
        text = summarize({"resource_changes": many}, sha="s", run_url="u")
        self.assertIn("150 to add", text)
        self.assertIn("50 more", text)
        self.assertNotIn("a.r149", text)


class FailureCommentTest(unittest.TestCase):
    def test_says_the_plan_failed_and_points_to_the_run_without_details(self):
        text = failure_comment(sha="abc1234", run_url="https://example.invalid/run/1")
        self.assertTrue(text.startswith(MARKER))
        self.assertIn("failed", text)
        self.assertIn("https://example.invalid/run/1", text)


class ErrorLinesTest(unittest.TestCase):
    def test_keeps_error_lines_with_and_without_the_box_border(self):
        log = "Refreshing state... [id=secret-id]\n╷\n│ Error: Failed to get existing workspaces\n│ \n│   with google_x.y,\n╵\nError: boom\n"
        self.assertEqual(error_lines(log), ["Error: Failed to get existing workspaces", "Error: boom"])

    def test_plan_text_is_not_an_error_line(self):
        log = '  # google_x.y will be created\n  + resource "google_x" "y" {\n      + name = "n"\n    }\nPlan: 1 to add, 0 to change, 0 to destroy.\n'
        self.assertEqual(error_lines(log), [])

    def test_a_long_error_line_is_cut(self):
        self.assertLessEqual(len(error_lines("Error: " + "x" * 5000)[0]), 400)


class StateVariablesTest(unittest.TestCase):
    def test_reads_the_billing_account_and_the_alert_address_from_the_state(self):
        self.assertEqual(
            state_variables(STATE),
            {"TF_VAR_billing_account": "AAAAAA-BBBBBB-CCCCCC", "TF_VAR_alert_email": EMAIL},
        )

    def test_a_state_without_them_is_an_error_that_names_the_address_not_a_value(self):
        with self.assertRaises(StateError) as caught:
            state_variables({"values": {"root_module": {"resources": []}}})
        self.assertIn("google_billing_budget.monthly", str(caught.exception))

    def test_an_empty_state_is_an_error(self):
        with self.assertRaises(StateError):
            state_variables({})


class MainTest(unittest.TestCase):
    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_summary_writes_the_body_and_prints_only_the_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp, "plan.json")
            plan.write_text(json.dumps(PLAN))
            body = Path(tmp, "body.md")
            code, printed = self.run_main("summary", str(plan), str(body), "--sha", "abc1234", "--run-url", "u")
            self.assertEqual(code, 0)
            self.assertEqual(body.read_text(), printed.rstrip("\n") + "\n" if printed else "")
            self.assertNotIn(CANARY, printed)
            self.assertNotIn(CANARY, body.read_text())

    def test_summary_of_unreadable_json_prints_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp, "plan.json")
            plan.write_text("{" + CANARY)
            code, printed = self.run_main("summary", str(plan), str(Path(tmp, "b.md")), "--sha", "s", "--run-url", "u")
            self.assertEqual(code, 1)
            self.assertNotIn(CANARY, printed)

    def test_state_vars_masks_each_value_and_writes_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "state.json")
            state.write_text(json.dumps(STATE))
            env = Path(tmp, "env")
            code, printed = self.run_main("state-vars", str(state), str(env))
            self.assertEqual(code, 0)
            self.assertIn("::add-mask::AAAAAA-BBBBBB-CCCCCC", printed)
            self.assertIn(f"::add-mask::{EMAIL}", printed)
            self.assertEqual(
                env.read_text().splitlines(),
                ["TF_VAR_billing_account=AAAAAA-BBBBBB-CCCCCC", f"TF_VAR_alert_email={EMAIL}"],
            )

    def test_state_vars_refuses_a_value_with_a_line_break(self):
        state = json.loads(json.dumps(STATE))
        state["values"]["root_module"]["resources"][0]["values"]["billing_account"] = "A\nB"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            path.write_text(json.dumps(state))
            code, _ = self.run_main("state-vars", str(path), str(Path(tmp, "env")))
            self.assertEqual(code, 1)

    def test_errors_prints_only_error_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "plan.log")
            log.write_text(f"  + name = \"{CANARY}\"\n│ Error: nope\n")
            code, printed = self.run_main("errors", str(log))
            self.assertEqual(code, 0)
            self.assertEqual(printed.strip(), "Error: nope")

    def test_failure_writes_the_failure_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp, "body.md")
            code, _ = self.run_main("failure", str(body), "--sha", "abc1234", "--run-url", "u")
            self.assertEqual(code, 0)
            self.assertIn("failed", body.read_text())


class WorkflowTest(unittest.TestCase):
    """The workflow file keeps the properties the ticket promises (CHB-13)."""

    @classmethod
    def setUpClass(cls):
        full = (ROOT / ".github/workflows/terraform-plan.yml").read_text(encoding="utf-8")
        cls.text = "\n".join(line for line in full.splitlines() if not line.lstrip().startswith("#")) + "\n"

    def test_runs_on_pull_requests_that_change_infra_gcp_only(self):
        self.assertIn("pull_request:", self.text)
        self.assertRegex(self.text, r"paths:\s*\n\s*- infra/gcp/\*\*")
        self.assertNotIn("push:", self.text)
        self.assertNotIn("pull_request_target", self.text)

    def test_plans_without_a_lock_and_never_applies(self):
        self.assertIn("plan -lock=false", self.text)
        self.assertNotRegex(self.text, r"terraform (apply|destroy|import|state (rm|mv|push))")

    def test_the_plan_goes_to_a_file_and_nothing_prints_it(self):
        self.assertIn("-out=", self.text)
        for line in re.findall(r"terraform .*", self.text):
            if "terraform show" in line or "terraform plan" in line:
                self.assertRegex(line, r"> \"\$out/", line)
        self.assertNotRegex(self.text, r"terraform show(?! -json)")

    def test_every_action_is_one_that_ci_and_deploy_already_use(self):
        used = set()
        for name in ("ci.yml", "deploy.yml"):
            used |= set(re.findall(r"uses:\s*(\S+)@", (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")))
        mine = set(re.findall(r"uses:\s*(\S+)@", self.text))
        self.assertTrue(mine)
        self.assertLessEqual(mine, used)
        for line in re.findall(r"uses:\s*(\S+)", self.text):
            self.assertRegex(line, r"@[0-9a-f]{40}$")

    def test_terraform_is_checked_by_the_same_sha256_as_ci(self):
        ci = re.search(r"TERRAFORM_SHA256: ([0-9a-f]{64})", (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        mine = re.search(r"TERRAFORM_SHA256: ([0-9a-f]{64})", self.text)
        self.assertTrue(ci and mine)
        self.assertEqual(ci.group(1), mine.group(1))
        self.assertIn("sha256sum -c", self.text)
        self.assertIn("infra/gcp/.terraform-version", self.text)

    def test_the_job_asks_for_the_least_it_needs(self):
        perms = re.search(r"\n    permissions:\n((?:      .*\n)+)", self.text)
        self.assertTrue(perms)
        self.assertEqual(
            sorted(line.split("#")[0].strip() for line in perms.group(1).splitlines()),
            ["contents: read", "id-token: write", "pull-requests: write"],
        )
        self.assertRegex(self.text, r"\npermissions: \{\}\n")

    def test_forks_are_skipped(self):
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", self.text)

    def test_authenticates_as_the_planner_through_the_pr_pool(self):
        self.assertIn("workloadIdentityPools/github-pr/providers/github-pr", self.text)
        self.assertIn("github-planner@", self.text)

    def test_the_checkout_keeps_no_token(self):
        self.assertIn("persist-credentials: false", self.text)

    def test_the_comment_uses_the_workflow_token(self):
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.text)


if __name__ == "__main__":
    unittest.main()
