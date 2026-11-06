import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_supply_chain import (
    Evidence,
    decide,
    is_dependabot_commit,
    is_supply_chain_file,
    owner_approved_head,
    release_age_changed,
)

HEAD = "a" * 40
OLD = "b" * 40
OWNER = "islomar"


def dependabot_commit(sha=HEAD):
    """A commit as GitHub records it for Dependabot (PRs #1 to #4, 2026-11-05)."""
    return {
        "sha": sha,
        "author": {"login": "dependabot[bot]", "type": "Bot"},
        "committer": {"login": "web-flow", "type": "User"},
        "commit": {"verification": {"verified": True, "reason": "valid"}},
    }


def review(state, login=OWNER, commit_id=HEAD):
    return {"user": {"login": login}, "state": state, "commit_id": commit_id}


def evidence(**overrides):
    values = {
        "changed_files": ["frontend/package-lock.json"],
        "release_age_files": [],
        "pr_author": "dependabot[bot]",
        "pr_head_sha": HEAD,
        "pr_commit_count": 1,
        "commits": [dependabot_commit()],
        "reviews": [],
        "owner": OWNER,
        "head_sha": HEAD,
    }
    return Evidence(**(values | overrides))


class SupplyChainFileTest(unittest.TestCase):
    def test_manifests_lockfiles_images_and_pins_in_any_directory(self):
        for path in [
            "backend/pyproject.toml",
            "backend/uv.lock",
            "frontend/package.json",
            "e2e/package-lock.json",
            "new-app/package.json",
            "frontend/.npmrc",
            "Dockerfile",
            "compose.yaml",
            ".nvmrc",
            ".python-version",
            "tools/pnpm-lock.yaml",
            "backend/requirements-dev.txt",
        ]:
            with self.subTest(path=path):
                self.assertTrue(is_supply_chain_file(path))

    def test_source_docs_and_harness_files_are_not_supply_chain(self):
        for path in [
            "backend/src/app.py",
            "docs/decisions/ADR-ebe6-unattended-dependency-updates.md",
            ".github/workflows/ci.yml",
            "scripts/check_release_age.py",
        ]:
            with self.subTest(path=path):
                self.assertFalse(is_supply_chain_file(path))


class ReleaseAgeTest(unittest.TestCase):
    PYPROJECT = (
        '[project]\ndependencies = ["fastapi>=0.120"]\n\n[tool.uv]\nexclude-newer = "7 days"\n'
    )

    def test_a_dependency_floor_bump_keeps_the_settings(self):
        head = self.PYPROJECT.replace("0.120", "0.121")
        self.assertFalse(release_age_changed("backend/pyproject.toml", self.PYPROJECT, head))

    def test_changed_window_or_new_exemption_in_pyproject(self):
        for head in [
            self.PYPROJECT.replace("7 days", "1 day"),
            self.PYPROJECT + "exclude-newer-package = { fastapi = false }\n",
            self.PYPROJECT.replace('exclude-newer = "7 days"\n', ""),
            "not toml [",
        ]:
            with self.subTest(head=head):
                self.assertTrue(release_age_changed("backend/pyproject.toml", self.PYPROJECT, head))

    def test_pyproject_added_with_settings_counts_as_a_change(self):
        self.assertTrue(release_age_changed("new/pyproject.toml", None, self.PYPROJECT))
        self.assertFalse(release_age_changed("new/pyproject.toml", None, "[project]\n"))

    def test_npmrc_window_and_exemptions(self):
        base = "# comment\nmin-release-age=7\n"
        self.assertFalse(release_age_changed("frontend/.npmrc", base, base + "# more\n"))
        self.assertTrue(release_age_changed("frontend/.npmrc", base, "min-release-age=1\n"))
        self.assertTrue(
            release_age_changed("frontend/.npmrc", base, base + "min-release-age-exclude[]=vite\n")
        )
        self.assertTrue(release_age_changed("frontend/.npmrc", base, None))
        self.assertTrue(release_age_changed("new/.npmrc", None, ""))

    def test_other_files_never_change_release_age(self):
        self.assertFalse(release_age_changed("frontend/package.json", "{}", '{"a": 1}'))


class DependabotCommitTest(unittest.TestCase):
    def test_dependabot_commit_signed_by_github(self):
        self.assertTrue(is_dependabot_commit(dependabot_commit()))

    def test_rejects_other_authors_committers_and_unverified_signatures(self):
        cases = {
            "person": {"author": {"login": "islomar"}},
            "unknown author": {"author": None},
            "pushed with git": {"committer": {"login": "dependabot[bot]"}},
            "unsigned": {"commit": {"verification": {"verified": False}}},
            "no verification": {"commit": {}},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertFalse(is_dependabot_commit(dependabot_commit() | change))


class OwnerApprovalTest(unittest.TestCase):
    def test_approval_on_the_head_commit(self):
        self.assertTrue(owner_approved_head([review("APPROVED")], OWNER, HEAD))

    def test_approval_on_an_older_commit_does_not_count(self):
        self.assertFalse(owner_approved_head([review("APPROVED", commit_id=OLD)], OWNER, HEAD))

    def test_later_comment_keeps_the_approval(self):
        reviews = [review("APPROVED"), review("COMMENTED")]
        self.assertTrue(owner_approved_head(reviews, OWNER, HEAD))

    def test_later_change_request_or_dismissal_cancels_it(self):
        for state in ["CHANGES_REQUESTED", "DISMISSED"]:
            with self.subTest(state):
                self.assertFalse(
                    owner_approved_head([review("APPROVED"), review(state)], OWNER, HEAD)
                )

    def test_approvals_by_others_do_not_count(self):
        reviews = [review("APPROVED", login="chb-factory-agent[bot]")]
        self.assertFalse(owner_approved_head(reviews, OWNER, HEAD))


class DecideTest(unittest.TestCase):
    def test_passes_when_no_supply_chain_file_changed(self):
        verdict = decide(evidence(changed_files=["backend/src/app.py"], commits=[], pr_author="x"))
        self.assertTrue(verdict.ok)

    def test_passes_a_dependabot_only_pr(self):
        self.assertTrue(decide(evidence()).ok)

    def test_fails_when_someone_else_pushed_a_commit(self):
        commits = [
            dependabot_commit(OLD),
            dependabot_commit() | {"author": {"login": "chb-factory-agent[bot]"}},
        ]
        verdict = decide(evidence(commits=commits, pr_commit_count=2))
        self.assertFalse(verdict.ok)
        self.assertIn("chb-factory-agent[bot]", verdict.message)

    def test_fails_when_the_pr_author_is_not_dependabot(self):
        self.assertFalse(decide(evidence(pr_author="chb-factory-agent[bot]")).ok)

    def test_owner_approval_on_head_passes_any_pr(self):
        verdict = decide(
            evidence(
                pr_author="chb-factory-agent[bot]",
                commits=[dependabot_commit() | {"author": {"login": "chb-factory-agent[bot]"}}],
                release_age_files=["frontend/.npmrc"],
                reviews=[review("APPROVED")],
            )
        )
        self.assertTrue(verdict.ok)

    def test_release_age_change_needs_the_owner_even_from_dependabot(self):
        verdict = decide(evidence(release_age_files=["backend/pyproject.toml"]))
        self.assertFalse(verdict.ok)
        self.assertIn("backend/pyproject.toml", verdict.message)

    def test_fails_closed_when_the_commit_list_is_incomplete_or_stale(self):
        self.assertFalse(decide(evidence(pr_commit_count=300)).ok)
        self.assertFalse(decide(evidence(pr_head_sha=OLD)).ok)
        self.assertFalse(decide(evidence(commits=[])).ok)


if __name__ == "__main__":
    unittest.main()
