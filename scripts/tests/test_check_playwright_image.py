import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_playwright_image import WORKFLOWS, errors

DIGEST = "@sha256:" + "a" * 64


class ErrorsTest(unittest.TestCase):
    def test_matching_pinned_image_passes(self):
        text = f"PLAYWRIGHT_IMAGE: mcr.microsoft.com/playwright:v1.65.0-noble{DIGEST}"
        self.assertEqual(errors("1.65.0", "ci.yml", text), [])

    def test_version_mismatch_fails(self):
        text = f"image: mcr.microsoft.com/playwright:v1.64.0-noble{DIGEST}"
        [error] = errors("1.65.0", "deploy.yml", text)
        self.assertIn("file=deploy.yml", error)
        self.assertIn("v1.64.0 does not match @playwright/test 1.65.0", error)

    def test_missing_digest_fails(self):
        [error] = errors("1.65.0", "ci.yml", "mcr.microsoft.com/playwright:v1.65.0-noble")
        self.assertIn("pin mcr.microsoft.com/playwright:v1.65.0-noble by digest", error)

    def test_workflow_without_image_fails(self):
        [error] = errors("1.65.0", "ci.yml", "npx playwright install --with-deps chromium")
        self.assertIn("file=ci.yml::no mcr.microsoft.com/playwright image found", error)

    def test_both_workflows_are_checked(self):
        self.assertEqual({w.name for w in WORKFLOWS}, {"ci.yml", "deploy.yml"})


if __name__ == "__main__":
    unittest.main()
