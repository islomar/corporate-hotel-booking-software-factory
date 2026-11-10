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


if __name__ == "__main__":
    unittest.main()
