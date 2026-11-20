"""The CHB-204 dry run still passes on the files of this branch (CHB-218 review).

The dry run reads workflow, Terraform and factory files to seed its model. A change to one of
those files can break its seed without anyone noticing, as fa60388 did. This test runs it
offline: `gh` is hidden, so every GitHub read uses its documented fallback.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRY_RUN = ROOT / "docs/plans/CHB-204/dry-run.sh"


class DryRunTest(unittest.TestCase):
    def test_the_dry_run_passes_without_github(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            fake_gh = Path(bin_dir) / "gh"
            fake_gh.write_text("#!/bin/sh\nexit 1\n")
            fake_gh.chmod(0o755)
            result = subprocess.run(
                ["bash", str(DRY_RUN)],
                capture_output=True,
                text=True,
                env={"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "HOME": bin_dir},
                cwd=ROOT,
                timeout=120,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-1000:])
        self.assertIn("DRY RUN PASSED", result.stdout)


if __name__ == "__main__":
    unittest.main()
