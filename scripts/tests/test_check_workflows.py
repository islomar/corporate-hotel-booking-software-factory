"""CHB-189: `make check-workflows` runs zizmor and fails on findings of medium severity or above.

The tests call the make target, not zizmor itself, so they prove the gate. They need uv and the
backend's dev dependencies: the sandbox (`.devcontainer/run make check`) and CI have both.
"""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "scripts" / "tests" / "fixtures" / "zizmor"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def check_workflows(path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "--no-print-directory", "-C", str(ROOT), "check-workflows", f"WORKFLOWS={path}"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def plain(result: subprocess.CompletedProcess[str]) -> str:
    """The output without color codes, so assertions hold whatever zizmor decides about color."""
    return ANSI.sub("", result.stdout + result.stderr)


# Outside CI a missing uv skips the gate tests. In CI (CI is set) they run, and a missing uv fails them.
@unittest.skipUnless(shutil.which("uv") or os.environ.get("CI"), "needs uv: run it with .devcontainer/run make check")
class CheckWorkflowsTest(unittest.TestCase):
    def test_a_pull_request_target_workflow_that_runs_the_pr_head_fails_the_check(self) -> None:
        result = check_workflows(FIXTURES / "pull-request-target.yml")
        output = plain(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("dangerous-triggers", output)

    def test_a_medium_severity_finding_fails_the_check(self) -> None:
        result = check_workflows(FIXTURES / "medium-only.yml")
        output = plain(result)

        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("artipacked", output)

    def test_a_low_severity_finding_passes_the_check(self) -> None:
        result = check_workflows(FIXTURES / "low-only.yml")
        output = plain(result)

        self.assertEqual(result.returncode, 0, output)
        self.assertNotRegex(output, r"(?m)^(error|warning)\[")
        self.assertIn("1 ignored", output)  # the low finding exists, below the threshold

    def test_a_workflows_variable_in_the_environment_does_not_move_the_scan(self) -> None:
        """Only the command line may point the scan elsewhere: a stray shell variable must not."""
        result = subprocess.run(
            ["make", "--no-print-directory", "-C", str(ROOT), "-n", "check-workflows"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "WORKFLOWS": str(FIXTURES / "low-only.yml")},
        )

        self.assertIn(f'"{ROOT / ".github" / "workflows"}"', result.stdout)

    def test_the_check_prints_no_color_codes_on_github_actions(self) -> None:
        """CHB-189: on GitHub Actions zizmor colored its summary and broke a text assertion."""
        result = check_workflows(FIXTURES / "low-only.yml", GITHUB_ACTIONS="true", CI="true")

        self.assertNotIn("\x1b[", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
