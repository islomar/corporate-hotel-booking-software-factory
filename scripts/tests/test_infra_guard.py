from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra_guard import Refused, Summary, apply, check_destroys, check_git, main, plan, summarise

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MIXED = json.loads((FIXTURES / "plan_mixed.json").read_text())
NOOP = json.loads((FIXTURES / "plan_noop.json").read_text())
COMMIT = "c0ffee" + "0" * 34
MIXED_DESTRUCTIVE = (
    'terraform_data.env["staging"],terraform_data.gone,terraform_data.rep,terraform_data.rep_cbd'
)


class FakeRepo:
    """Answers the git commands of check_git, and terraform commands for `plan` and `apply`."""

    def __init__(self, branch="main", status="", ahead=0, behind=0, fetch_code=0, plans=None):
        self.git = {
            ("rev-parse", "--abbrev-ref", "HEAD"): (0, branch),
            ("status", "--porcelain", "--untracked-files=all", "--", "infra", "Makefile", "scripts"): (0, status),
            ("fetch", "--quiet", "origin", "main"): (fetch_code, ""),
            ("rev-list", "--left-right", "--count", "HEAD...origin/main"): (0, f"{ahead}\t{behind}"),
            ("rev-parse", "HEAD"): (0, COMMIT),
        }
        self.plans = plans or {"gcp": NOOP, "github": NOOP}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], capture: bool) -> tuple[int, str]:
        self.calls.append(args)
        if args[0] == "git":
            return self.git[tuple(args[1:])]
        directory = args[1].removeprefix("-chdir=infra/")
        if args[2] == "plan":
            Path(args[-1].removeprefix("-out=")).write_bytes(f"plan of {directory}".encode())
        if args[2] == "show":
            return 0, json.dumps(self.plans[directory])
        return 0, ""

    def terraform_calls(self) -> list[str]:
        return [" ".join(c[1:3]) for c in self.calls if c[0] == "terraform"]


class CheckGitTest(unittest.TestCase):
    def refusal(self, repo: FakeRepo) -> str:
        with self.assertRaises(Refused) as caught:
            check_git(repo)
        return str(caught.exception)

    def test_main_equal_to_origin_main_passes_and_returns_the_commit(self):
        self.assertEqual(check_git(FakeRepo()), COMMIT)

    def test_feature_branch_is_refused_with_the_switch_command(self):
        message = self.refusal(FakeRepo(branch="CHB-10-old"))
        self.assertIn("on `CHB-10-old`", message)
        self.assertIn("Fix: git switch main && git pull --ff-only", message)

    def test_detached_head_is_refused(self):
        self.assertIn("on `HEAD`", self.refusal(FakeRepo(branch="HEAD")))

    def test_local_changes_in_watched_paths_are_refused_and_listed(self):
        message = self.refusal(FakeRepo(status=" M infra/github/environments.tf\n?? infra/gcp/new.tf"))
        self.assertIn("infra/gcp/new.tf", message)
        self.assertIn("git stash push --include-untracked", message)

    def test_local_changes_are_checked_before_fetching(self):
        repo = FakeRepo(status=" M Makefile")
        self.refusal(repo)
        self.assertNotIn(["git", "fetch", "--quiet", "origin", "main"], repo.calls)

    def test_behind_origin_is_refused_with_pull(self):
        message = self.refusal(FakeRepo(behind=3))
        self.assertIn("3 commit(s) behind", message)
        self.assertIn("Fix: git pull --ff-only", message)

    def test_ahead_of_origin_is_refused_with_a_backup_before_reset(self):
        message = self.refusal(FakeRepo(ahead=1))
        self.assertIn("1 commit(s) that are not on `origin/main`", message)
        self.assertIn("git log origin/main..main", message)
        self.assertIn("git branch main-backup && git reset --keep origin/main", message)

    def test_diverged_is_refused_as_ahead(self):
        self.assertIn("and is 2 behind", self.refusal(FakeRepo(ahead=1, behind=2)))

    def test_failed_fetch_is_refused(self):
        self.assertIn("`git fetch --quiet origin main` failed", self.refusal(FakeRepo(fetch_code=128)))


class SummariseTest(unittest.TestCase):
    def test_mixed_plan_is_classified_by_actions(self):
        summary = summarise(MIXED)
        self.assertEqual(summary.add, ["terraform_data.new"])
        self.assertEqual(summary.change, ["terraform_data.upd"])
        self.assertEqual(summary.destroy, ['terraform_data.env["staging"]', "terraform_data.gone"])
        # ["delete", "create"] and create_before_destroy's ["create", "delete"]
        self.assertEqual(summary.replace, ["terraform_data.rep", "terraform_data.rep_cbd"])
        self.assertEqual(summary.counts(), "1 to add, 1 to change, 2 to destroy, 2 to replace, 0 to forget")

    def test_noop_plan_has_nothing_to_do(self):
        self.assertEqual(summarise(NOOP), Summary())

    def test_forget_is_reported_and_not_destructive(self):
        plan = {"resource_changes": [{"address": "a.b", "change": {"actions": ["forget"]}}]}
        summary = summarise(plan)
        self.assertEqual(summary.forget, ["a.b"])
        self.assertEqual(summary.destructive(), [])

    def test_unknown_actions_are_refused(self):
        plan = {"resource_changes": [{"address": "a.b", "change": {"actions": ["teleport"]}}]}
        with self.assertRaises(Refused):
            summarise(plan)


class CheckDestroysTest(unittest.TestCase):
    def refusal(self, allow: set[str]) -> str:
        with self.assertRaises(Refused) as caught:
            check_destroys({"github": summarise(MIXED)}, allow)
        return str(caught.exception)

    def test_no_destroys_and_no_override_passes(self):
        check_destroys({"gcp": summarise(NOOP)}, set())

    def test_destroys_without_override_are_refused_and_listed(self):
        message = self.refusal(set())
        self.assertIn("github: terraform_data.gone", message)
        self.assertIn("github: terraform_data.rep_cbd", message)
        self.assertIn(f"make infra-plan ALLOW_DESTROY='{MIXED_DESTRUCTIVE}'", message)

    def test_override_matching_exactly_passes(self):
        check_destroys({"github": summarise(MIXED)}, set(MIXED_DESTRUCTIVE.split(",")))

    def test_override_missing_one_address_is_refused(self):
        allow = set(MIXED_DESTRUCTIVE.split(",")) - {"terraform_data.gone"}
        self.assertIn("would be destroyed or replaced", self.refusal(allow))

    def test_override_with_an_extra_address_is_refused(self):
        allow = set(MIXED_DESTRUCTIVE.split(",")) | {"terraform_data.keep"}
        self.assertIn("do not destroy or replace:\n  terraform_data.keep", self.refusal(allow))

    def test_override_without_destroys_is_refused(self):
        with self.assertRaises(Refused) as caught:
            check_destroys({"gcp": summarise(NOOP)}, {"terraform_data.gone"})
        self.assertIn("run again without ALLOW_DESTROY", str(caught.exception))


class PlanAndApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plans = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_command(self, command, repo: FakeRepo, env: dict[str, str] | None = None) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            command(repo, env or {}, self.plans)

    def test_plan_saves_plan_and_summary_per_directory(self):
        repo = FakeRepo(plans={"gcp": NOOP, "github": MIXED})
        with self.assertRaises(Refused):
            self.run_command(plan, repo)
        record = json.loads((self.plans / "github.json").read_text())
        self.assertEqual(record["commit"], COMMIT)
        self.assertEqual(record["destroy"], ['terraform_data.env["staging"]', "terraform_data.gone"])
        self.assertTrue((self.plans / "gcp.tfplan").exists())
        self.assertIn(f"plan -input=false -out={self.plans}/gcp.tfplan", [" ".join(c[2:]) for c in repo.calls])

    def test_plan_from_a_branch_runs_no_terraform(self):
        repo = FakeRepo(branch="CHB-10-old")
        with self.assertRaises(Refused):
            self.run_command(plan, repo)
        self.assertEqual(repo.terraform_calls(), [])

    def test_apply_applies_the_saved_plans(self):
        repo = FakeRepo()
        self.run_command(plan, repo)
        self.run_command(apply, repo)
        self.assertEqual(repo.terraform_calls()[-2:], ["-chdir=infra/gcp apply", "-chdir=infra/github apply"])
        self.assertIn(str(self.plans / "github.tfplan"), repo.calls[-1])
        self.assertEqual(list(self.plans.iterdir()), [])

    def test_apply_without_saved_plans_is_refused(self):
        with self.assertRaisesRegex(Refused, "No saved plan for infra/gcp"):
            self.run_command(apply, FakeRepo())

    def test_apply_of_a_plan_from_an_older_commit_is_refused(self):
        repo = FakeRepo()
        self.run_command(plan, repo)
        repo.git[("rev-parse", "HEAD")] = (0, "beef" + "0" * 36)
        with self.assertRaisesRegex(Refused, "made from commit c0ffee000000"):
            self.run_command(apply, repo)
        self.assertNotIn("apply", " ".join(repo.terraform_calls()))

    def test_apply_of_a_changed_plan_file_is_refused(self):
        repo = FakeRepo()
        self.run_command(plan, repo)
        (self.plans / "github.tfplan").write_bytes(b"another plan")
        with self.assertRaisesRegex(Refused, "changed after infra-plan wrote it"):
            self.run_command(apply, repo)

    def test_apply_rechecks_destroys_in_the_saved_plan_and_applies_nothing(self):
        repo = FakeRepo(plans={"gcp": NOOP, "github": MIXED})
        allow = {"ALLOW_DESTROY": MIXED_DESTRUCTIVE}
        self.run_command(plan, repo, allow)
        with self.assertRaisesRegex(Refused, "terraform_data.gone"):
            self.run_command(apply, repo)
        self.assertNotIn("apply", " ".join(repo.terraform_calls()))
        self.run_command(apply, repo, allow)
        self.assertEqual(repo.terraform_calls()[-1], "-chdir=infra/github apply")


class MainTest(unittest.TestCase):
    def test_refusal_exits_1_with_the_reason_on_stderr(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["git"], FakeRepo(branch="CHB-1-x"), {})
        self.assertEqual(code, 1)
        self.assertIn("infra guard refused (git)", err.getvalue())

    def test_unknown_command_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["destroy"], FakeRepo(), {}), 2)


if __name__ == "__main__":
    unittest.main()
