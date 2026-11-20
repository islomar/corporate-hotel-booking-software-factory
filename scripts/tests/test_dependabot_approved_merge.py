"""CHB-213: the human's approval of the head of a needs-human Dependabot PR turns auto-merge on."""

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import dependabot_approved_merge
from dependabot_approved_merge import REQUIRED_CHECKS, decide, main, missing_checks

ROOT = Path(__file__).resolve().parents[2]
HUMAN = "islomar"
HEAD = "a" * 40
OLDER = "b" * 40
REPO = "owner/repo"


def pr(author="dependabot[bot]", head=HEAD, labels=("dependencies:needs-human",), state="open", draft=False):
    return {
        "number": 7,
        "state": state,
        "draft": draft,
        "user": {"login": author},
        "head": {"sha": head, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
        "labels": [{"name": name} for name in labels],
        "commits": 1,
    }


def commit(author="dependabot[bot]", committer="web-flow", verified=True):
    return {
        "sha": HEAD,
        "author": {"login": author},
        "committer": {"login": committer},
        "commit": {"verification": {"verified": verified}},
    }


def review(state="APPROVED", user=HUMAN, commit_id=HEAD):
    return {"state": state, "user": {"login": user}, "commit_id": commit_id}


def check(pull=None, commits=None, reviews=None, head=HEAD):
    return decide(
        pull or pr(),
        [commit()] if commits is None else commits,
        [review()] if reviews is None else reviews,
        head_sha=head,
        human=HUMAN,
        repo=REPO,
    )


class DecideTest(unittest.TestCase):
    def test_the_humans_approval_of_the_head_of_a_needs_human_dependabot_pr_enables_auto_merge(self):
        self.assertTrue(check().enable)

    def test_an_approval_of_an_older_commit_does_not(self):
        self.assertFalse(check(reviews=[review(commit_id=OLDER)]).enable)

    def test_an_approval_by_someone_else_does_not(self):
        self.assertFalse(check(reviews=[review(user="chb-factory-agent[bot]")]).enable)

    def test_a_comment_by_the_human_does_not(self):
        self.assertFalse(check(reviews=[review(state="COMMENTED")]).enable)

    def test_a_non_dependabot_pr_does_not(self):
        self.assertFalse(check(pull=pr(author="chb-factory-agent[bot]")).enable)

    def test_a_dismissed_approval_does_not(self):
        self.assertFalse(check(reviews=[review(), review(state="DISMISSED")]).enable)

    def test_a_change_request_after_the_approval_does_not(self):
        self.assertFalse(check(reviews=[review(), review(state="CHANGES_REQUESTED")]).enable)

    def test_a_comment_after_the_approval_keeps_it(self):
        # Same rule as the `supply-chain` check: comments do not change a decision.
        self.assertTrue(check(reviews=[review(), review(state="COMMENTED")]).enable)

    def test_a_dependabot_pr_without_the_needs_human_label_does_not(self):
        # Safe updates go through dependabot-auto-merge.yml; this workflow does not touch them.
        self.assertFalse(check(pull=pr(labels=("dependencies:safe",))).enable)

    def test_a_pr_whose_head_moved_does_not(self):
        self.assertFalse(check(pull=pr(head=OLDER), reviews=[review(commit_id=OLDER)]).enable)

    def test_a_closed_or_draft_pr_does_not(self):
        self.assertFalse(check(pull=pr(state="closed")).enable)
        self.assertFalse(check(pull=pr(draft=True)).enable)

    def test_a_commit_that_is_not_dependabots_does_not(self):
        self.assertFalse(check(commits=[commit(), commit(author=HUMAN, verified=False)]).enable)

    def test_a_missing_commit_listing_does_not(self):
        self.assertFalse(check(commits=[]).enable)

    def test_a_pr_from_another_repository_does_not(self):
        pull = pr()
        pull["head"]["repo"] = {"full_name": "fork/repo"}
        self.assertFalse(check(pull=pull).enable)

    def test_a_pr_into_another_branch_does_not(self):
        pull = pr()
        pull["base"]["ref"] = "release"
        self.assertFalse(check(pull=pull).enable)

    def test_the_reason_names_what_failed(self):
        self.assertIn("dependencies:needs-human", check(pull=pr(labels=())).reason)


def ruleset(*contexts):
    return [
        {"type": "pull_request"},
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
        },
    ]


class RulesetTest(unittest.TestCase):
    def test_nothing_is_missing_when_main_requires_every_check(self):
        self.assertEqual(missing_checks(ruleset(*REQUIRED_CHECKS, "triage")), [])

    def test_names_the_checks_main_does_not_require(self):
        self.assertEqual(missing_checks(ruleset("backend", "frontend")), ["e2e", "supply-chain"])

    def test_the_list_matches_the_safe_update_path(self):
        """dependabot-auto-merge.yml checks the same list before it enables auto-merge. Change both together."""
        text = (ROOT / ".github/workflows/dependabot-auto-merge.yml").read_text()
        match = re.search(r"for check in ([a-z0-9 -]+); do", text)
        assert match, "the ruleset loop is missing in dependabot-auto-merge.yml"
        self.assertEqual(set(match.group(1).split()), set(REQUIRED_CHECKS))


class MainTest(unittest.TestCase):
    def run_main(self, responses):
        def fake_get(path, token):
            for prefix, body in responses.items():
                if path.startswith(prefix):
                    return body
            raise AssertionError(f"unexpected GET {path}")

        out = io.StringIO()
        with (
            mock.patch.object(dependabot_approved_merge, "github_get", side_effect=fake_get),
            mock.patch.dict("os.environ", {"GH_TOKEN": "t"}),
            contextlib.redirect_stdout(out),
        ):
            code = main(["--repo", REPO, "--head-sha", HEAD, "--human", HUMAN])
        return code, out.getvalue()

    def responses(self, reviews=None, rules=None):
        return {
            f"/repos/{REPO}/commits/{HEAD}/pulls": [{"number": 7}],
            f"/repos/{REPO}/pulls/7/commits": [commit()],
            f"/repos/{REPO}/pulls/7/reviews": [review()] if reviews is None else reviews,
            f"/repos/{REPO}/pulls/7": pr(),
            f"/repos/{REPO}/rules/branches/main": ruleset(*REQUIRED_CHECKS) if rules is None else rules,
        }

    def test_prints_the_pr_number_when_auto_merge_may_turn_on(self):
        code, out = self.run_main(self.responses())
        self.assertEqual(code, 0)
        self.assertIn("number=7", out.splitlines())

    def test_prints_no_number_without_the_humans_approval(self):
        code, out = self.run_main(self.responses(reviews=[review(user="someone")]))
        self.assertEqual(code, 0)
        self.assertNotIn("number=", out)

    def test_prints_no_number_when_main_does_not_require_ci(self):
        code, out = self.run_main(self.responses(rules=ruleset("backend")))
        self.assertEqual(code, 0)
        self.assertNotIn("number=", out)
        self.assertIn("supply-chain", out)

    def test_rejects_a_head_that_is_not_a_commit_sha(self):
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict("os.environ", {"GH_TOKEN": "t"}):
            self.assertEqual(main(["--repo", REPO, "--head-sha", "main", "--human", HUMAN]), 2)

    def test_an_api_error_exits_2(self):
        out = io.StringIO()
        with (
            mock.patch.object(dependabot_approved_merge, "github_get", side_effect=OSError("down")),
            mock.patch.dict("os.environ", {"GH_TOKEN": "t"}),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(main(["--repo", REPO, "--head-sha", HEAD, "--human", HUMAN]), 2)


if __name__ == "__main__":
    unittest.main()
