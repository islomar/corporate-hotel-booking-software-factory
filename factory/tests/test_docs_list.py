"""The docs allowlist is shared by the reviewer and CI, and CI skips its test jobs only for docs (CHB-117)."""

import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REVIEW = Path(__file__).resolve().parents[1] / "review"
sys.path.insert(0, str(REVIEW))

import docs_list  # noqa: E402
import plan  # noqa: E402
from docs_list import ci_docs_only, main  # noqa: E402

CI = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
TEST_JOBS = ("backend", "frontend", "e2e")


class OneSourceOfTruthTest(unittest.TestCase):
    def test_the_reviewer_uses_the_same_allowlist(self) -> None:
        self.assertIs(plan.is_doc, docs_list.is_doc)
        self.assertIs(plan.docs_only, docs_list.docs_only)
        self.assertIs(plan.is_governance, docs_list.is_governance)

    def test_the_module_imports_nothing_from_the_repository(self) -> None:
        # ci.yml runs a copy of the file alone, from $RUNNER_TEMP.
        source = (REVIEW / "docs_list.py").read_text()
        imports = re.findall(r"^(?:from|import) (\S+)", source, re.MULTILINE)
        for module in imports:
            with self.subTest(module):
                self.assertIn(module, {"__future__", "argparse", "re", "subprocess", "sys", "collections.abc", "pathlib"})


class CiDocsOnlyTest(unittest.TestCase):
    def test_docs_and_readmes_skip_the_test_jobs(self) -> None:
        self.assertTrue(ci_docs_only(["docs/specs/a.md", "README.md", "backend/README.md", "docs/specs/flow.png"]))

    def test_any_code_or_governance_path_runs_them(self) -> None:
        for path in ("backend/src/chb/app.py", "CLAUDE.md", "factory/review/docs_list.py", ".github/workflows/ci.yml"):
            with self.subTest(path):
                self.assertFalse(ci_docs_only(["docs/specs/a.md", path]))

    def test_session_exports_run_them(self) -> None:
        # The backend job's tests check docs/sessions/ for unredacted exports (CHB-122).
        self.assertTrue(docs_list.docs_only(["docs/sessions/2026-11-06.md"]))
        self.assertFalse(ci_docs_only(["docs/sessions/2026-11-06.md"]))
        self.assertFalse(ci_docs_only(["docs/specs/a.md", "docs/sessions/README.md"]))

    def test_no_changed_files_runs_them(self) -> None:
        self.assertFalse(ci_docs_only([]))


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        local = subprocess.run(["git", "rev-parse", "--local-env-vars"], check=True, capture_output=True, text=True)  # noqa: S603,S607
        for name in local.stdout.split():
            os.environ.pop(name, None)
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        self.dir = repo.name
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

    def _main(self, event: str = "pull_request", base: str | None = None, head: str | None = None) -> tuple[int, str]:
        argv = ["--repo", self.dir, "--base", base or self.base, "--head", head or self._rev(), "--event", event]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, stdout.getvalue()

    def test_docs_only_pr(self) -> None:
        self._commit("docs/specs/a.md", "spec\n")

        self.assertEqual(self._main(), (0, "docs_only=true\n"))

    def test_code_pr(self) -> None:
        self._commit("docs/specs/a.md", "spec\n")
        self._commit("backend/a.py", "x = 1\n")

        self.assertEqual(self._main(), (0, "docs_only=false\n"))

    def test_moving_code_into_docs_counts_the_deleted_path(self) -> None:
        self._commit("backend/a.md", "x\n")
        base = self._rev()
        (Path(self.dir) / "docs").mkdir()
        self._git("mv", "backend/a.md", "docs/a.md")
        self._git("commit", "-q", "-m", "move")

        self.assertEqual(self._main(base=base), (0, "docs_only=false\n"))

    def test_session_export_pr(self) -> None:
        self._commit("docs/sessions/2026-11-06.md", "export\n")

        self.assertEqual(self._main(), (0, "docs_only=false\n"))

    def test_push_runs_every_job(self) -> None:
        self._commit("docs/specs/a.md", "spec\n")

        self.assertEqual(self._main(event="push"), (0, "docs_only=false\n"))

    def test_malformed_sha_runs_every_job(self) -> None:
        self._commit("docs/specs/a.md", "spec\n")

        self.assertEqual(self._main(base="HEAD~1"), (0, "docs_only=false\n"))

    def test_unknown_commit_runs_every_job(self) -> None:
        self._commit("docs/specs/a.md", "spec\n")

        self.assertEqual(self._main(base="f" * 40), (0, "docs_only=false\n"))

    def test_no_change_runs_every_job(self) -> None:
        self.assertEqual(self._main(), (0, "docs_only=false\n"))


def _job(text: str, name: str) -> str:
    """The lines of one top-level job of ci.yml."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z0-9-]+:\n|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"job {name} not found in ci.yml"
    return match.group(1)


class CiWorkflowTest(unittest.TestCase):
    """Text checks on ci.yml: a later edit must not drop the fail-safe."""

    def setUp(self) -> None:
        self.text = CI.read_text()

    def test_no_workflow_level_path_filter(self) -> None:
        # A workflow skipped by path filtering leaves its required checks Pending.
        triggers = self.text[self.text.index("\non:") : self.text.index("\npermissions:")]
        self.assertNotRegex(triggers, r"paths(-ignore)?:")

    def test_each_test_job_needs_changes_and_runs_unless_docs_only_is_true(self) -> None:
        for name in TEST_JOBS:
            with self.subTest(name):
                job = _job(self.text, name)
                needs = re.search(r"^    needs: (.*)$", job, re.MULTILINE)
                self.assertIsNotNone(needs)
                self.assertIn("changes", needs.group(1))  # type: ignore[union-attr]
                condition = re.search(r"^    if: (.*?)(?=^    [a-z-]+:)", job, re.MULTILINE | re.DOTALL)
                self.assertIsNotNone(condition)
                self.assertIn("!cancelled()", condition.group(1))  # type: ignore[union-attr]
                self.assertIn("needs.changes.outputs.docs_only != 'true'", condition.group(1))  # type: ignore[union-attr]

    def test_e2e_still_waits_for_backend_and_frontend(self) -> None:
        job = _job(self.text, "e2e")
        self.assertIn("needs.backend.result == 'success'", job)
        self.assertIn("needs.frontend.result == 'success'", job)

    def test_test_jobs_keep_their_names(self) -> None:
        # The required contexts in infra/github/ruleset.tf are the job ids.
        for name in TEST_JOBS:
            with self.subTest(name):
                self.assertNotRegex(_job(self.text, name), r"(?m)^    name:")

    def test_changes_runs_the_base_commit_copy_of_the_allowlist(self) -> None:
        job = _job(self.text, "changes")
        self.assertIn('git show "$BASE_SHA:factory/review/docs_list.py"', job)
        self.assertIn('python3 -I "$script"', job)
        self.assertIn('echo "docs_only=false"', job)


if __name__ == "__main__":
    unittest.main()
