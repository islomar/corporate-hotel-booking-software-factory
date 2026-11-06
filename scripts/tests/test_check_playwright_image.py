import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_playwright_image import WORKFLOWS, errors, main

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

    def test_the_repository_workflows_pass(self):
        self.assertEqual({w.name for w in WORKFLOWS}, {"ci.yml", "deploy.yml"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)


class MainTest(unittest.TestCase):
    def run_main(self, *workflow_texts: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            lockfile = Path(tmp, "package-lock.json")
            lockfile.write_text(
                json.dumps({"packages": {"node_modules/@playwright/test": {"version": "1.65.0"}}})
            )
            workflows = []
            for i, text in enumerate(workflow_texts):
                workflows.append(Path(tmp, f"w{i}.yml"))
                workflows[-1].write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(lockfile, tuple(workflows))
            return code, out.getvalue()

    def test_same_pinned_image_everywhere_passes(self):
        image = f"mcr.microsoft.com/playwright:v1.65.0-noble{DIGEST}"
        self.assertEqual(self.run_main(image, image)[0], 0)

    def test_one_bad_workflow_among_good_ones_fails(self):
        good = f"mcr.microsoft.com/playwright:v1.65.0-noble{DIGEST}"
        code, out = self.run_main(good, "no image here")
        self.assertEqual(code, 1)
        self.assertIn("w1.yml::no mcr.microsoft.com/playwright image found", out)

    def test_different_digests_for_the_same_tag_fail(self):
        other = "@sha256:" + "b" * 64
        code, out = self.run_main(
            f"mcr.microsoft.com/playwright:v1.65.0-noble{DIGEST}",
            f"mcr.microsoft.com/playwright:v1.65.0-noble{other}",
        )
        self.assertEqual(code, 1)
        self.assertIn("pin different Playwright images", out)


if __name__ == "__main__":
    unittest.main()
