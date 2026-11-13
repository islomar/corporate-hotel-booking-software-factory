import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_supply_chain
from check_supply_chain import (
    Evidence,
    changed_files,
    decide,
    gather,
    github_get,
    is_dependabot_commit,
    is_supply_chain_file,
    main,
    owner_approved_head,
    parse_updated_dependencies,
    pr_update_risks,
    release_age_change,
    update_risks,
)
from git_sandbox import isolate_from_outer_repo

HEAD = "a" * 40
OLD = "b" * 40
OWNER = "islomar"


# Commit messages as Dependabot wrote them on PRs #2 and #3 (2026-11-05).
PR2_REQUIREMENT_UPDATE = (
    (
        "Update uvicorn[standard] requirement from >=0.40 to >=0.54.0 in /backend\n\n"
        "Updates the requirements on [uvicorn[standard]](https://github.com/Kludex/uvicorn)"
        " to permit the latest version.\n"
    )
    + """- [Release notes](https://github.com/Kludex/uvicorn/releases)
- [Changelog](https://github.com/Kludex/uvicorn/blob/main/docs/release-notes.md)
- [Commits](https://github.com/Kludex/uvicorn/compare/0.40.0...0.54.0)

---
updated-dependencies:
- dependency-name: uvicorn[standard]
  dependency-version: 0.54.0
  dependency-type: direct:production
...

Signed-off-by: dependabot[bot] <support@github.com>"""
)

PR3_MAJOR = """Bump jsdom from 29.1.1 to 30.1.3 in /frontend

Bumps [jsdom](https://github.com/jsdom/jsdom) from 29.1.1 to 30.1.3.
- [Release notes](https://github.com/jsdom/jsdom/releases)
- [Commits](https://github.com/jsdom/jsdom/compare/v29.1.1...v30.1.3)

---
updated-dependencies:
- dependency-name: jsdom
  dependency-version: 30.1.3
  dependency-type: direct:development
  update-type: version-update:semver-major
...

Signed-off-by: dependabot[bot] <support@github.com>"""

# PR #3's message with a safe update type: what a minor bump looks like.
SAFE_MINOR = PR3_MAJOR.replace("30.1.3", "29.2.0").replace("semver-major", "semver-minor")

# A grouped update: one entry per dependency, with the group name.
GROUP = """Bump the vite group in /frontend with 2 updates

---
updated-dependencies:
- dependency-name: vite
  dependency-version: 8.4.0
  dependency-type: direct:development
  update-type: version-update:semver-minor
  dependency-group: vite
- dependency-name: "@vitejs/plugin-react"
  dependency-version: 6.0.2
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: vite
...

Signed-off-by: dependabot[bot] <support@github.com>"""


def dependabot_commit(sha=HEAD, message=SAFE_MINOR):
    """A commit as GitHub records it for Dependabot (PRs #1 to #4, 2026-11-05)."""
    return {
        "sha": sha,
        "author": {"login": "dependabot[bot]", "type": "Bot"},
        "committer": {"login": "web-flow", "type": "User"},
        "commit": {"message": message, "verification": {"verified": True, "reason": "valid"}},
    }


def review(state, login=OWNER, commit_id=HEAD):
    return {"user": {"login": login}, "state": state, "commit_id": commit_id}


def evidence(**overrides):
    values = {
        "changed_files": ["frontend/package-lock.json"],
        "release_age_files": [],
        "pr_author": "dependabot[bot]",
        "pr_head_ref": "dependabot/npm_and_yarn/frontend/jsdom-29.2.0",
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
            "backend/uv.toml",
            "pip.conf",
            "pnpm-workspace.yaml",
            ".yarnrc.yml",
        ]:
            with self.subTest(path=path):
                self.assertTrue(is_supply_chain_file(path))

    def test_image_and_compose_files_match_in_any_case(self):
        for path in [
            "app.Containerfile",
            "Compose.YAML",
            "backend/Docker-Compose.yml",
            "DOCKERFILE",
            "backend/dockerfile",
            "app.Dockerfile",
            "Dockerfile.dev",
            "Containerfile",
            "Containerfile.dev",
            "backend/containerfile",
        ]:
            with self.subTest(path=path):
                self.assertTrue(is_supply_chain_file(path))

    def test_source_docs_and_harness_files_are_not_supply_chain(self):
        for path in [
            "backend/src/app.py",
            "docs/decisions/ADR-ebe6-unattended-dependency-updates.md",
            ".github/workflows/ci.yml",
            "scripts/check_release_age.py",
            "docs/dockerfile-notes.md",
        ]:
            with self.subTest(path=path):
                self.assertFalse(is_supply_chain_file(path))


class ReleaseAgeTest(unittest.TestCase):
    PYPROJECT = (
        '[project]\ndependencies = ["fastapi>=0.120"]\n\n[tool.uv]\nexclude-newer = "7 days"\n'
    )

    def test_a_dependency_floor_bump_keeps_the_settings(self):
        head = self.PYPROJECT.replace("0.120", "0.121")
        self.assertIsNone(release_age_change("backend/pyproject.toml", self.PYPROJECT, head))

    def test_changed_window_or_new_exemption_in_pyproject(self):
        for head in [
            self.PYPROJECT.replace("7 days", "1 day"),
            self.PYPROJECT + "exclude-newer-package = { fastapi = false }\n",
            self.PYPROJECT.replace('exclude-newer = "7 days"\n', ""),
            "not toml [",
        ]:
            with self.subTest(head=head):
                self.assertIsNotNone(
                    release_age_change("backend/pyproject.toml", self.PYPROJECT, head)
                )

    def test_pyproject_added_with_settings_counts_as_a_change(self):
        self.assertIsNotNone(release_age_change("new/pyproject.toml", None, self.PYPROJECT))
        self.assertIsNone(release_age_change("new/pyproject.toml", None, "[project]\n"))

    def test_npmrc_window_and_exemptions(self):
        base = "# comment\nmin-release-age=7\n"
        self.assertIsNone(release_age_change("frontend/.npmrc", base, base + "# more\n"))
        self.assertIsNotNone(release_age_change("frontend/.npmrc", base, "min-release-age=1\n"))
        self.assertIsNotNone(
            release_age_change("frontend/.npmrc", base, base + "min-release-age-exclude[]=vite\n")
        )
        self.assertIsNotNone(release_age_change("frontend/.npmrc", base, None))
        self.assertIsNotNone(release_age_change("new/.npmrc", None, ""))

    def test_uv_toml_compares_top_level_settings(self):
        base = 'exclude-newer = "7 days"\n'
        self.assertIsNone(release_age_change("backend/uv.toml", base, base + "# note\n"))
        self.assertIsNotNone(
            release_age_change("backend/uv.toml", base, 'exclude-newer = "1 day"\n')
        )
        self.assertIsNotNone(release_age_change("uv.toml", None, base))
        self.assertIsNotNone(
            release_age_change("uv.toml", base, base + "[exclude-newer-package]\nvite = false\n")
        )

    def test_unexpected_pyproject_shape_counts_as_a_change(self):
        for head in ["tool = 1\n", "[tool]\nuv = 1\n"]:
            with self.subTest(head=head):
                self.assertIsNotNone(
                    release_age_change("backend/pyproject.toml", self.PYPROJECT, head)
                )

    def test_any_change_to_pnpm_yarn_or_bun_settings_counts(self):
        for path in ["pnpm-workspace.yaml", "tools/.yarnrc.yml", "backend/bunfig.toml"]:
            with self.subTest(path=path):
                self.assertIsNone(release_age_change(path, "a: 1\n", "a: 1\n"))
                self.assertIsNotNone(release_age_change(path, "a: 1\n", "a: 2\n"))
                self.assertIsNotNone(release_age_change(path, None, "a: 1\n"))
                self.assertIsNotNone(release_age_change(path, "a: 1\n", None))

    def test_other_files_never_change_release_age(self):
        self.assertIsNone(release_age_change("frontend/package.json", "{}", '{"a": 1}'))


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


class UpdatedDependenciesTest(unittest.TestCase):
    def test_parses_the_trailer_of_real_dependabot_commits(self):
        self.assertEqual(
            parse_updated_dependencies(PR3_MAJOR),
            [
                {
                    "dependency-name": "jsdom",
                    "dependency-version": "30.1.3",
                    "dependency-type": "direct:development",
                    "update-type": "version-update:semver-major",
                }
            ],
        )
        self.assertEqual(
            parse_updated_dependencies(PR2_REQUIREMENT_UPDATE)[0]["dependency-name"],
            "uvicorn[standard]",
        )

    def test_parses_groups_and_quoted_names(self):
        names = [d["dependency-name"] for d in parse_updated_dependencies(GROUP)]
        self.assertEqual(names, ["vite", "@vitejs/plugin-react"])

    def test_a_message_without_a_valid_trailer_gives_none(self):
        for message in [
            "Bump x",
            "---\nupdated-dependencies:\n...\n",
            "---\nupdated-dependencies:\n- dependency-name: x\n  oops\n...\n",
            "---\nupdated-dependencies:\n- dependency-name: x\n",
        ]:
            with self.subTest(message=message):
                self.assertIsNone(parse_updated_dependencies(message))


class UpdateRisksTest(unittest.TestCase):
    def entry(self, update_type, version="2.3.4", name="lib"):
        entry = {"dependency-name": name, "dependency-version": version}
        if update_type:
            entry["update-type"] = f"version-update:semver-{update_type}"
        return entry

    def test_patch_and_minor_from_one_up_are_safe(self):
        self.assertEqual(
            update_risks([self.entry("patch"), self.entry("minor")], "npm_and_yarn"), []
        )
        self.assertEqual(update_risks(parse_updated_dependencies(GROUP), "npm_and_yarn"), [])

    def test_a_patch_of_a_zero_x_version_is_safe(self):
        self.assertEqual(update_risks([self.entry("patch", "0.54.1")], "uv"), [])

    def test_majors_zero_x_minors_playwright_and_unknown_types_need_a_human(self):
        cases = {
            "major": [self.entry("major")],
            "0.x minor": [self.entry("minor", "0.55.0")],
            "playwright": [self.entry("patch", name="@playwright/test")],
            "no update type": parse_updated_dependencies(PR2_REQUIREMENT_UPDATE),
            "real major": parse_updated_dependencies(PR3_MAJOR),
            "no dependencies": [],
            "unparsed": None,
        }
        for name, dependencies in cases.items():
            with self.subTest(name):
                self.assertNotEqual(update_risks(dependencies, "npm_and_yarn"), [])

    def test_github_actions_bumps_without_a_type_are_safe(self):
        self.assertEqual(update_risks([self.entry(None)], "github_actions"), [])
        self.assertNotEqual(update_risks([self.entry(None)], "npm_and_yarn"), [])


class DependabotUpdateTypeTest(unittest.TestCase):
    def test_a_dependabot_major_needs_the_owner(self):
        verdict = decide(evidence(commits=[dependabot_commit(message=PR3_MAJOR)]))
        self.assertFalse(verdict.ok)
        self.assertIn("jsdom: major update", verdict.message)

    def test_owner_approval_lets_a_major_through(self):
        verdict = decide(
            evidence(commits=[dependabot_commit(message=PR3_MAJOR)], reviews=[review("APPROVED")])
        )
        self.assertTrue(verdict.ok)

    def test_commits_that_disagree_need_the_owner(self):
        commits = [dependabot_commit(OLD), dependabot_commit(message=GROUP)]
        self.assertFalse(decide(evidence(commits=commits, pr_commit_count=2)).ok)

    def test_ecosystem_comes_from_the_dependabot_branch(self):
        actions = dependabot_commit(
            message=SAFE_MINOR.replace("  update-type: version-update:semver-minor\n", "")
        )
        self.assertFalse(decide(evidence(commits=[actions])).ok)
        ref = "dependabot/github_actions/actions/checkout-7.1.0"
        self.assertTrue(decide(evidence(commits=[actions], pr_head_ref=ref)).ok)

    def test_no_commits_is_a_risk_not_a_crash(self):
        # Defensive: decide() stops at _dependabot_problems before this when commits is empty.
        self.assertEqual(pr_update_risks(evidence(commits=[])), ["no commits"])


class MainTest(unittest.TestCase):
    ARGS = ("--repo", "o/r", "--pr", "7", "--base", OLD, "--head", HEAD, "--owner", OWNER)

    def run_main(self, gather_result=None, error=None, token="t", later=()):  # noqa: S107
        results = [gather_result, *later]

        def fake_gather(args, token):
            if error:
                raise error
            return results.pop(0) if len(results) > 1 else results[0]

        env = {"GH_TOKEN": token} if token else {}
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(check_supply_chain, "gather", fake_gather),
            contextlib.redirect_stdout(out),
        ):
            code = main(list(self.ARGS))
        return code, out.getvalue()

    def test_pass_exits_zero(self):
        self.assertEqual(self.run_main(evidence())[0], 0)

    def test_a_dismissal_during_the_run_fails_it(self):
        # CHB-184: a re-run that read an approval must not finish green after a dismissal.
        approved = evidence(pr_author="someone", reviews=[review("APPROVED")])
        dismissed = evidence(pr_author="someone", reviews=[review("APPROVED"), review("DISMISSED")])
        self.assertEqual(self.run_main(approved)[0], 0)
        code, out = self.run_main(approved, later=[dismissed])
        self.assertEqual(code, 1)
        self.assertIn("::error::supply-chain:", out)

    def test_failing_verdict_exits_one_with_an_error_annotation(self):
        code, out = self.run_main(evidence(pr_author="someone"))
        self.assertEqual(code, 1)
        self.assertIn("::error::supply-chain:", out)

    def test_missing_token_exits_two(self):
        code, out = self.run_main(evidence(), token=None)
        self.assertEqual(code, 2)
        self.assertIn("GH_TOKEN", out)

    def test_git_and_api_errors_exit_two(self):
        for error in [
            check_supply_chain.CheckError("git diff failed"),
            OSError("network down"),
            KeyError("head"),
            ValueError("bad JSON"),
        ]:
            with self.subTest(error=repr(error)):
                code, out = self.run_main(error=error)
                self.assertEqual(code, 2)
                self.assertIn("could not run", out)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S603,S607


def rev(repo, ref):
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "rev-parse", ref],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class GitRepoTest(unittest.TestCase):
    """A real repository: base commit on main, head commit on a branch."""

    def setUp(self):
        # Ignore the developer's git config (global hooks would run on every test commit)
        # and the GIT_DIR of a worktree hook (git would write to the outer repository).
        isolate_from_outer_repo(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "backend").mkdir()
        (self.repo / "backend/pyproject.toml").write_text('[tool.uv]\nexclude-newer = "7 days"\n')
        (self.repo / "README.md").write_text("x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = rev(self.repo, "HEAD")
        git(self.repo, "checkout", "-q", "-b", "pr")

    def commit_head(self, files):
        for path, text in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "head")
        return rev(self.repo, "HEAD")

    def test_non_ascii_and_quoted_paths_are_listed_unquoted(self):
        head = self.commit_head({"dép/package.json": "{}", 'we"ird/Dockerfile': "FROM x\n"})
        with contextlib.chdir(self.repo):
            files = changed_files(self.base, head)
        self.assertEqual(sorted(files), ["dép/package.json", 'we"ird/Dockerfile'])
        self.assertTrue(all(is_supply_chain_file(f) for f in files))

    def gather_dependabot_pr(self, head, ref="dependabot/uv/backend/fastapi-0.121.0"):
        """gather() for a Dependabot PR with one safe commit at head."""
        responses = {
            "/repos/o/r/pulls/7": {
                "user": {"login": "dependabot[bot]"},
                "head": {"sha": head, "ref": ref},
                "commits": 1,
            },
            "/repos/o/r/pulls/7/commits?per_page=100": [dependabot_commit(head)],
            "/repos/o/r/pulls/7/reviews?per_page=100": [],
        }
        args = argparse.Namespace(repo="o/r", pr=7, base=self.base, head=head, owner=OWNER)
        with (
            contextlib.chdir(self.repo),
            contextlib.redirect_stdout(io.StringIO()),
            mock.patch.object(
                check_supply_chain, "github_get", lambda path, token: responses[path]
            ),
        ):
            return gather(args, token="t")  # noqa: S106

    def test_gather_reads_git_and_the_api(self):
        head = self.commit_head(
            {
                "dép/package.json": "{}",
                "backend/pyproject.toml": '[tool.uv]\nexclude-newer = "1 day"\n',
            }
        )
        found = self.gather_dependabot_pr(head)
        self.assertEqual(
            sorted(found.changed_files), ["backend/pyproject.toml", "dép/package.json"]
        )
        self.assertEqual(found.release_age_files, ["backend/pyproject.toml"])
        self.assertEqual(found.pr_author, "dependabot[bot]")
        self.assertEqual(found.pr_head_ref, "dependabot/uv/backend/fastapi-0.121.0")
        verdict = decide(found)
        self.assertFalse(verdict.ok)
        self.assertIn("Release-age settings changed", verdict.message)

    def test_gather_skips_the_content_of_binary_files(self):
        # CHB-161: a PNG in a PR made the check crash with a UnicodeDecodeError.
        (self.repo / "docs").mkdir()
        (self.repo / "docs/avatar.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
        head = self.commit_head({})
        found = self.gather_dependabot_pr(head)
        self.assertEqual(found.changed_files, ["docs/avatar.png"])
        self.assertEqual(found.release_age_files, [])

    def test_a_dependabot_pr_that_touches_pnpm_yarn_or_bun_settings_needs_the_owner(self):
        for path in ["pnpm-workspace.yaml", "e2e/.yarnrc.yml", "bunfig.toml"]:
            with self.subTest(path=path):
                git(self.repo, "checkout", "-q", "-B", "pr", self.base)
                head = self.commit_head({path: f"# {path}\n"})
                found = self.gather_dependabot_pr(head, "dependabot/npm_and_yarn/vite-8.4.0")
                self.assertEqual(found.release_age_files, [path])
                verdict = decide(found)
                self.assertFalse(verdict.ok)
                self.assertIn(f"Release-age settings changed in {path}", verdict.message)


class FakeResponse(io.BytesIO):
    def __init__(self, body, link=""):
        super().__init__(json.dumps(body).encode())
        self.headers = {"Link": link} if link else {}


class GithubGetTest(unittest.TestCase):
    def test_follows_next_links_across_pages(self):
        pages = {
            "https://api.github.com/x?per_page=1": FakeResponse(
                [1],
                '<https://api.github.com/x?page=2>; rel="next", '
                '<https://api.github.com/x?page=3>; rel="last"',
            ),
            "https://api.github.com/x?page=2": FakeResponse(
                [2], '<https://api.github.com/x?page=3>; rel="next"'
            ),
            "https://api.github.com/x?page=3": FakeResponse([3]),
        }
        opened = []

        def urlopen(request, timeout):
            opened.append(request.full_url)
            return pages[request.full_url]

        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(github_get("/x?per_page=1", "t"), [1, 2, 3])
        self.assertEqual(len(opened), 3)

    def test_returns_an_object_as_is(self):
        with mock.patch("urllib.request.urlopen", lambda request, timeout: FakeResponse({"a": 1})):
            self.assertEqual(github_get("/y", "t"), {"a": 1})


if __name__ == "__main__":
    unittest.main()
