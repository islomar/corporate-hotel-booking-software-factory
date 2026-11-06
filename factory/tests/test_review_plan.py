import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))

from plan import (
    LARGE_DIFF_LINES,
    OPUS,
    SONNET,
    PriorVerdict,
    changed_lines,
    choose_model,
    decide,
    docs_only,
    is_doc,
    last_verdict,
    main,
)

HEAD = "c" * 40
OLD = "a" * 40
APP = {"slug": "chb-factory-reviewer"}


def _run(**fields: object) -> dict:
    run = {
        "name": "review",
        "app": APP,
        "status": "completed",
        "conclusion": "failure",
        "head_sha": OLD,
        "external_id": "reviewed:" + OLD,
        "completed_at": "2026-11-06T10:00:00Z",
        "output": {"title": "Fail: Merge Safety 7.00", "summary": "old summary"},
    }
    return run | fields


class DocsAllowlistTest(unittest.TestCase):
    def test_markdown_and_images_under_docs_and_readmes_are_docs(self) -> None:
        for path in (
            "docs/specs/booking.md",
            "docs/sessions/2026-11-06.md",
            "docs/specs/flow.png",
            "README.md",
            "backend/README.md",
        ):
            with self.subTest(path):
                self.assertTrue(is_doc(path))

    def test_governance_paths_are_never_docs(self) -> None:
        for path in (
            "CLAUDE.md",
            "AGENTS.md",
            "docs/specs/CLAUDE.md",
            "docs/specs/AGENTS.md",
            "docs/specs/CLAUDE.local.md",
            "factory/README.md",
            "factory/review/prompt.md",
            "docs/decisions/ADR-ea8c-automated-pr-review.md",
            "docs/process/kanban.md",
            "docs/knowledge/gates-trust-exit-codes.md",
            ".github/README.md",
            ".claude/README.md",
            "infra/README.md",
            "scripts/README.md",
        ):
            with self.subTest(path):
                self.assertFalse(is_doc(path))

    def test_dot_directories_manifests_and_other_types_are_not_docs(self) -> None:
        for path in (
            "docs/specs/.claude/settings.json",
            "docs/.hidden/notes.md",
            "docs/specs/run.sh",
            "docs/specs/diagram.svg",
            "docs/specs/package.json",
            "docs/specs/uv.lock",
            "docs/specs/requirements.txt",
            "frontend/src/notes.md",
            "backend/src/chb/app.py",
            "/etc/passwd",
            "docs//x.md",
        ):
            with self.subTest(path):
                self.assertFalse(is_doc(path))

    def test_docs_only_needs_at_least_one_file_and_all_docs(self) -> None:
        self.assertFalse(docs_only([]))
        self.assertTrue(docs_only(["README.md", "docs/specs/a.md"]))
        self.assertFalse(docs_only(["README.md", "backend/src/chb/app.py"]))


class ModelChoiceTest(unittest.TestCase):
    def test_small_feature_change_gets_sonnet(self) -> None:
        self.assertEqual(choose_model(["backend/src/chb/app.py"], 120), SONNET)

    def test_large_diff_gets_opus(self) -> None:
        self.assertEqual(choose_model(["backend/src/chb/app.py"], LARGE_DIFF_LINES), SONNET)
        self.assertEqual(choose_model(["backend/src/chb/app.py"], LARGE_DIFF_LINES + 1), OPUS)

    def test_binary_file_gets_opus(self) -> None:
        self.assertEqual(choose_model(["frontend/public/logo.png"], None), OPUS)

    def test_high_risk_paths_get_opus(self) -> None:
        for path in (
            ".github/workflows/ci.yml",
            "factory/review/gate.py",
            "infra/github/ruleset.tf",
            "CLAUDE.md",
            "AGENTS.md",
            "docs/process/kanban.md",
            "docs/decisions/ADR-x.md",
            "backend/uv.lock",
            "frontend/package.json",
            "Dockerfile",
        ):
            with self.subTest(path):
                self.assertEqual(choose_model(["backend/src/chb/app.py", path], 1), OPUS)


class ChangedLinesTest(unittest.TestCase):
    def test_sums_added_and_deleted(self) -> None:
        self.assertEqual(changed_lines("3\t1\ta.py\x0010\t0\tb.py\x00"), 14)
        self.assertEqual(changed_lines(""), 0)

    def test_binary_outside_docs_is_unknown(self) -> None:
        self.assertIsNone(changed_lines("3\t1\ta.py\x00-\t-\tfrontend/public/logo.png\x00"))

    def test_images_under_docs_count_as_zero(self) -> None:
        self.assertEqual(changed_lines("3\t1\ta.py\x00-\t-\tdocs/specs/flow.png\x00"), 4)


class LastVerdictTest(unittest.TestCase):
    def test_newest_review_by_the_reviewer_app(self) -> None:
        newer = _run(external_id="reviewed:" + "b" * 40, conclusion="success", completed_at="2026-11-06T11:00:00Z")

        prior = last_verdict([_run(), newer])

        self.assertEqual(prior, PriorVerdict("b" * 40, "success", "Fail: Merge Safety 7.00", "old summary"))

    def test_ignores_other_apps_names_and_non_verdicts(self) -> None:
        runs = [
            _run(app={"slug": "github-actions"}),
            _run(name="backend"),
            _run(conclusion="neutral", external_id="skipped"),
            _run(external_id="carried:" + OLD),
            _run(status="in_progress"),
            _run(external_id="reviewed:main; rm -rf /"),
            "not a run",
        ]

        self.assertIsNone(last_verdict(runs))

    def test_errors_and_runs_without_a_reviewed_marker_are_not_verdicts(self) -> None:
        runs = [
            _run(external_id="error:" + OLD),
            _run(external_id=None),
            _run(external_id=""),
        ]

        self.assertIsNone(last_verdict(runs))


class DecideTest(unittest.TestCase):
    PRIOR = PriorVerdict(OLD, "failure", "Fail: Merge Safety 7.00", "old summary")

    def test_docs_only_pr_is_skipped_with_a_neutral_check(self) -> None:
        plan = decide(HEAD, ["docs/specs/a.md"], 5, None, None)

        self.assertEqual(plan.action, "skip")
        self.assertIsNone(plan.model)
        assert plan.payload is not None
        self.assertEqual(plan.payload["conclusion"], "neutral")
        self.assertEqual(plan.payload["name"], "review")
        self.assertEqual(plan.payload["head_sha"], HEAD)
        self.assertEqual(plan.payload["external_id"], "skipped")

    def test_docs_only_delta_carries_the_previous_verdict(self) -> None:
        plan = decide(HEAD, ["backend/src/chb/app.py", "docs/specs/a.md"], 50, self.PRIOR, ["docs/specs/a.md"])

        self.assertEqual(plan.action, "carry")
        assert plan.payload is not None
        output = plan.payload["output"]
        assert isinstance(output, dict)
        self.assertEqual(plan.payload["conclusion"], "failure")
        self.assertEqual(plan.payload["head_sha"], HEAD)
        self.assertEqual(plan.payload["external_id"], "carried:" + OLD)
        self.assertIn("old summary", output["summary"])
        self.assertTrue(output["title"].startswith("Carried: "))

    def test_code_in_the_delta_needs_a_new_review(self) -> None:
        plan = decide(HEAD, ["backend/src/chb/app.py"], 50, self.PRIOR, ["backend/src/chb/app.py"])

        self.assertEqual((plan.action, plan.model, plan.payload), ("review", SONNET, None))

    def test_governance_file_in_the_delta_needs_a_new_review(self) -> None:
        plan = decide(HEAD, ["backend/src/chb/app.py", "CLAUDE.md"], 50, self.PRIOR, ["CLAUDE.md"])

        self.assertEqual((plan.action, plan.model), ("review", OPUS))

    def test_unknown_delta_or_no_prior_review_needs_a_review(self) -> None:
        self.assertEqual(decide(HEAD, ["backend/a.py"], 1, self.PRIOR, None).action, "review")
        self.assertEqual(decide(HEAD, ["backend/a.py"], 1, None, ["docs/a.md"]).action, "review")
        self.assertEqual(decide(HEAD, ["backend/a.py"], 1, self.PRIOR, []).action, "review")

    def test_file_names_in_summaries_are_escaped(self) -> None:
        plan = decide(HEAD, ["docs/specs/@owner <b>.md"], 1, None, None)

        assert plan.payload is not None
        summary = plan.payload["output"]["summary"]  # type: ignore[index]
        self.assertNotIn("@owner", summary)
        self.assertNotIn("<b>", summary)


class CommandLineTest(unittest.TestCase):
    """Runs main() on a real git repository: a base commit, then PR commits."""

    def setUp(self) -> None:
        repo = tempfile.TemporaryDirectory()
        files = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        self.addCleanup(files.cleanup)
        self.dir = repo.name
        self.files = Path(files.name)
        self._git("init", "-q", "-b", "main")
        self._commit("README.md", "base\n")
        self.base = self._rev()

    def _git(self, *args: str) -> str:
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": self.dir,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        command = ["git", "-C", self.dir, *args]
        return subprocess.run(command, check=True, capture_output=True, text=True, env=env).stdout  # noqa: S603

    def _commit(self, path: str, text: str) -> None:
        target = Path(self.dir) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self._git("add", path)
        self._git("commit", "-q", "-m", path)

    def _rev(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def _main(self, runs: list, base: str | None = None) -> tuple[int, str, Path]:
        runs_file, out = self.files / "runs.json", self.files / "out.json"
        runs_file.write_text(json.dumps(runs))
        argv = ["--head-dir", self.dir, "--base-sha", base or self.base, "--head-sha", self._rev()]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main([*argv, "--prior-runs", str(runs_file), "--out", str(out)])
        return code, stdout.getvalue(), out

    def test_docs_only_pr_is_skipped(self) -> None:
        self._commit("docs/specs/a.md", "spec\n")

        code, stdout, out = self._main([])

        self.assertEqual((code, stdout), (0, "action=skip\n"))
        self.assertEqual(json.loads(out.read_text())["conclusion"], "neutral")

    def test_code_pr_is_reviewed_with_sonnet(self) -> None:
        self._commit("backend/a.py", "x = 1\n")

        self.assertEqual(self._main([])[:2], (0, f"action=review\nmodel={SONNET}\n"))

    def test_docs_push_after_a_review_carries_the_verdict(self) -> None:
        self._commit("backend/a.py", "x = 1\n")
        reviewed = self._rev()
        self._commit("docs/specs/a.md", "spec\n")

        code, stdout, out = self._main([_run(head_sha=reviewed, external_id="reviewed:" + reviewed, conclusion="success")])

        self.assertEqual((code, stdout), (0, "action=carry\n"))
        payload = json.loads(out.read_text())
        self.assertEqual((payload["conclusion"], payload["head_sha"]), ("success", self._rev()))

    def test_reviewed_commit_missing_from_history_needs_a_review(self) -> None:
        self._commit("backend/a.py", "x = 1\n")

        self.assertEqual(self._main([_run(external_id="reviewed:" + "e" * 40)])[1], f"action=review\nmodel={SONNET}\n")

    def test_failed_git_diff_falls_back_to_opus(self) -> None:
        self._commit("backend/a.py", "x = 1\n")

        self.assertEqual(self._main([], base="f" * 40)[:2], (0, f"action=review\nmodel={OPUS}\n"))

    def test_malformed_sha_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["--head-dir", self.dir, "--base-sha", "main", "--head-sha", self._rev(), "--prior-runs", "x", "--out", "y"])

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
