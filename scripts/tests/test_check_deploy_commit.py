"""The deploy gate refuses a commit that is not on main (CHB-173)."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_deploy_commit import main
from git_sandbox import isolate_from_outer_repo

DEPLOY_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "deploy.yml"
MAIN_REF = "refs/remotes/origin/main"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)  # noqa: S603,S607
    return result.stdout.strip()


class DeployCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        isolate_from_outer_repo(self)
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "test")
        self.first = self.commit("first")
        self.second = self.commit("second")
        git(self.repo, "update-ref", MAIN_REF, self.second)
        git(self.repo, "switch", "-q", "-c", "side", self.first)
        self.side = self.commit("side")

    def commit(self, message: str) -> str:
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def run_check(self, sha: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--repo", str(self.repo), f"--sha={sha}", "--main-ref", MAIN_REF])
        return code, err.getvalue()

    def test_the_head_of_main_passes(self) -> None:
        self.assertEqual(self.run_check(self.second)[0], 0)

    def test_an_older_commit_of_main_passes(self) -> None:
        self.assertEqual(self.run_check(self.first)[0], 0)

    def test_a_commit_off_main_is_refused(self) -> None:
        code, err = self.run_check(self.side)
        self.assertEqual(code, 1)
        self.assertIn("not on", err)

    def test_an_unknown_commit_is_refused(self) -> None:
        self.assertEqual(self.run_check("0" * 40)[0], 1)

    def test_a_sha_that_is_not_40_hex_is_refused(self) -> None:
        for sha in ("main", self.second[:12], self.second.upper(), f"{self.second} ", "--help"):
            with self.subTest(sha=sha):
                self.assertEqual(self.run_check(sha)[0], 1)

    def test_a_missing_main_ref_is_refused(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--repo", str(self.repo), "--sha", self.second, "--main-ref", "refs/remotes/origin/nothing"])
        self.assertEqual(code, 1)


def jobs(text: str) -> dict[str, str]:
    """Each top-level job of a workflow and its body, read by indentation."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


class DeployWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        self.jobs = jobs(self.text)

    def test_the_gate_holds_no_credentials(self) -> None:
        gate = self.jobs["gate"]
        self.assertNotIn("id-token", gate)
        self.assertNotIn("secrets.", gate)
        self.assertNotIn("environment:", gate)

    def test_the_gate_requires_the_real_ci_workflow(self) -> None:
        self.assertIn("github.event.workflow_run.path == '.github/workflows/ci.yml'", self.jobs["gate"])

    def test_the_gate_runs_the_check_from_main_never_from_the_deployed_commit(self) -> None:
        gate = self.jobs["gate"]
        self.assertIn(f"git show {MAIN_REF}:scripts/check_deploy_commit.py", gate)
        self.assertNotIn("ref: ${{ github.event.workflow_run.head_sha }}", gate)

    def test_every_other_job_needs_a_successful_gate(self) -> None:
        others = {name: body for name, body in self.jobs.items() if name != "gate"}
        self.assertTrue(others)
        for name, body in others.items():
            with self.subTest(job=name):
                self.assertRegex(body, r"needs: \[?[^\n]*\bgate\b")
                self.assertIn("needs.gate.result == 'success'", body)


if __name__ == "__main__":
    unittest.main()
