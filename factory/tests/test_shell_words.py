import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from shell_words import positionals, simple_commands


class SimpleCommandsTest(unittest.TestCase):
    def test_separators_split_commands(self) -> None:
        self.assertEqual(
            simple_commands("cd x && make check; git status | tail\nls & echo"),
            [["cd", "x"], ["make", "check"], ["git", "status"], ["tail"], ["ls"], ["echo"]],
        )

    def test_quoted_separators_stay_in_their_word(self) -> None:
        self.assertEqual(simple_commands("git commit -m 'a; b && c'"), [["git", "commit", "-m", "a; b && c"]])

    def test_redirections_leave_nothing_behind(self) -> None:
        cases = {
            "git push origin x 2>&1": [["git", "push", "origin", "x"]],
            "git push origin x > /tmp/log": [["git", "push", "origin", "x"]],
            "git push origin x >/tmp/log": [["git", "push", "origin", "x"]],
            "git push origin x 2>/dev/null": [["git", "push", "origin", "x"]],
            "git push origin x &>/tmp/log": [["git", "push", "origin", "x"]],
            "git push origin x >> /tmp/log 2>&1 | tail -5": [["git", "push", "origin", "x"], ["tail", "-5"]],
            "pytest < /dev/null": [["pytest"]],
            "cat <<'EOF' > /tmp/m\nhello\nEOF": [["cat"], ["hello"], ["EOF"]],
        }
        for command, expected in cases.items():
            with self.subTest(command):
                self.assertEqual(simple_commands(command), expected)

    def test_process_substitution_is_read_as_a_command(self) -> None:
        self.assertIn(["git", "checkout", "-b", "bad"], simple_commands("diff <(git checkout -b bad) x"))

    def test_launchers_are_stripped(self) -> None:
        cases = {
            "rtk git status": ["git", "status"],
            "rtk proxy git status": ["git", "status"],
            "factory/bin/as-agent git push": ["git", "push"],
            "env A=1 B=2 git log": ["git", "log"],
            "A=1 pytest": ["pytest"],
            "uv run --locked --project backend pytest -q": ["pytest", "-q"],
            "npx --yes vitest run": ["vitest", "run"],
            "python3 -I -m pytest -k x": ["pytest", "-k", "x"],
            "timeout 30 pytest": ["pytest"],
            "timeout -s KILL 30 pytest": ["pytest"],
            "nice -n 10 pytest": ["pytest"],
        }
        for command, expected in cases.items():
            with self.subTest(command):
                self.assertEqual(simple_commands(command), [expected])

    def test_unbalanced_quotes_fall_back_to_a_plain_split(self) -> None:
        self.assertEqual(
            simple_commands("git checkout -b x && cat <<'EOF' > /tmp/m\ndon't\nEOF"),
            [["git", "checkout", "-b", "x"], ["cat"], ["don't"], ["EOF"]],
        )


class PositionalsTest(unittest.TestCase):
    def test_options_and_their_values_are_skipped_and_everything_after_dashdash_counts(self) -> None:
        self.assertEqual(positionals(["-m", "slow", "-q", "a", "--", "-t", "b"], {"-m"}), ["a", "-t", "b"])


HOOKS = Path(__file__).resolve().parents[1] / "hooks"


class IsolatedRunTest(unittest.TestCase):
    """Run each hook as settings.json does, `python3 -I <script>`, so a broken import shows up here."""

    def _run(self, hook: str, event: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(HOOKS / hook)], input=json.dumps(event), capture_output=True, text=True, timeout=30
        )

    def test_the_branch_name_hook_allows_and_denies(self) -> None:
        ok = self._run("deny_bad_branch_names.py", {"tool_input": {"command": "git push -u origin CHB-54-x 2>&1"}})
        bad = self._run("deny_bad_branch_names.py", {"tool_input": {"command": "git checkout -b bad"}})

        self.assertEqual((ok.returncode, ok.stderr), (0, ""))
        self.assertEqual(bad.returncode, 2)
        self.assertIn("CHB-<n>-<slug>", bad.stderr)

    def test_the_related_tests_hook_allows_and_denies(self) -> None:
        ok = self._run("deny_full_test_suite.py", {"tool_input": {"command": "make check"}})
        bad = self._run("deny_full_test_suite.py", {"tool_input": {"command": "uv run pytest"}})

        self.assertEqual((ok.returncode, ok.stderr), (0, ""))
        self.assertEqual(bad.returncode, 2)
        self.assertIn("full suite", bad.stderr)

    def test_the_loop_detector_runs_and_never_blocks(self) -> None:
        run = self._run("warn_edit_loops.py", {"tool_input": {"file_path": "/x"}})

        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
