"""CHB-191: `make help` and the README list every described target; `make test` runs only tests."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESCRIBED = re.compile(r"^([a-zA-Z0-9_-]+):.*## ", re.MULTILINE)
CHECKS = ("ruff", "pyright", "format:check", "lint", "typecheck", "check_")


def _make(*args: str) -> list[str]:
    result = subprocess.run(["make", "--no-print-directory", "-C", str(ROOT), *args], capture_output=True, text=True, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def described_targets() -> set[str]:
    """Every target of the Makefile with a `## ` description, read without help's own pattern."""
    return set(DESCRIBED.findall((ROOT / "Makefile").read_text()))


def readme_targets() -> set[str]:
    """The targets in the `make <target>` lines of the README's quick start: the code block that starts with `make setup`."""
    blocks = re.findall(r"^```sh\n(.*?)^```", (ROOT / "README.md").read_text(), re.MULTILINE | re.DOTALL)
    quick_start = next(block for block in blocks if block.startswith("make setup"))
    return set(re.findall(r"^make ([a-zA-Z0-9_-]+)\b", quick_start, re.MULTILINE))


class HelpTest(unittest.TestCase):
    def test_make_help_lists_every_described_target(self) -> None:
        listed = {line.split()[0] for line in _make("help")}

        self.assertIn("e2e", listed)
        self.assertEqual(described_targets() - listed, set())

    def test_the_readme_names_every_target_make_help_prints(self) -> None:
        printed = {line.split()[0] for line in _make("help")}

        self.assertEqual(printed - readme_targets(), set())


class TestTargetTest(unittest.TestCase):
    def test_make_test_runs_only_the_tests(self) -> None:
        commands = _make("-n", "test")
        text = "\n".join(commands)

        self.assertIn("pytest", text)
        self.assertIn("npm test", text)
        self.assertIn("unittest discover -s scripts/tests", text)
        self.assertIn("unittest discover -s factory/tests", text)
        self.assertEqual([c for c in commands if any(word in c for word in CHECKS)], [])

    def test_make_check_still_runs_everything_make_test_runs(self) -> None:
        tests = set(_make("-n", "test"))
        check = set(_make("-n", "check"))

        self.assertEqual(tests - check, set())
        for word in CHECKS:
            self.assertTrue(any(word in c for c in check), word)


if __name__ == "__main__":
    unittest.main()
