import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import harness_bash_writes
from harness_bash_writes import write_targets

CWD = "/repo"


def targets(command: str, cwd: str = CWD) -> list[tuple[str, str]]:
    """Possible write targets that name a harness folder, as (path, working directory)."""
    return write_targets(command, cwd)


def paths(command: str, cwd: str = CWD) -> list[str]:
    return [path for path, _ in targets(command, cwd)]


class WritingFormsTest(unittest.TestCase):
    """Each form that writes, creates, moves or deletes a file gives that file as a target."""

    def test_each_writing_form_gives_its_harness_target(self) -> None:
        cases = {
            "echo x > factory/a": "factory/a",
            "echo x >> .github/w.yml": ".github/w.yml",
            "echo x >| .claude/s": ".claude/s",
            "echo x &> factory/a": "factory/a",
            "echo x 2> factory/err": "factory/err",
            'echo x > "factory/a b"': "factory/a b",
            "cat > factory/a <<'EOF'\nx\nEOF": "factory/a",
            "cat <<EOF > .claude/settings.json\n{}\nEOF": ".claude/settings.json",
            "echo x | tee factory/a": "factory/a",
            "echo x | tee -a /tmp/log .github/a": ".github/a",
            "sed -i 's/a/b/' factory/a": "factory/a",
            "sed -i.bak -e 's/a/b/' factory/a": "factory/a",
            "sed -E -i 's/a/b/' factory/a": "factory/a",
            "sed --in-place 's/a/b/' .github/a": ".github/a",
            "perl -pi -e 's/a/b/' factory/a": "factory/a",
            "cp /tmp/a factory/a": "factory/a",
            "cp -r /tmp/a /tmp/b .claude/": ".claude/",
            "cp -t factory /tmp/a": "factory",
            "cp --target-directory=factory /tmp/a": "factory",
            "mv /tmp/a .github/a": ".github/a",
            "mv factory/a /tmp/a": "factory/a",
            "install -m 755 /tmp/a factory/bin/a": "factory/bin/a",
            "ln -s /tmp/a factory/a": "factory/a",
            "rsync -a /tmp/a/ .claude/": ".claude/",
            "rm -rf factory/a": "factory/a",
            "touch .claude/x": ".claude/x",
            "mkdir -p factory/new": "factory/new",
            "truncate -s 0 .github/a": ".github/a",
            "dd if=/tmp/a of=factory/a": "factory/a",
        }
        for command, expected in cases.items():
            with self.subTest(command):
                self.assertIn(expected, paths(command))

    def test_launchers_and_assignments_do_not_hide_the_command(self) -> None:
        for command in ["rtk sed -i s/a/b/ factory/a", "env X=1 tee factory/a", "X=1 cp /tmp/a factory/a", "timeout 5 mv /tmp/a factory/a"]:
            with self.subTest(command):
                self.assertIn("factory/a", paths(command))

    def test_every_command_of_a_chain_counts(self) -> None:
        self.assertEqual(paths("ls && echo a > .claude/a; echo b > factory/b"), [".claude/a", "factory/b"])

    def test_a_cd_changes_the_directory_of_later_commands(self) -> None:
        self.assertEqual(targets("cd /repo/factory && echo x > a"), [("a", "/repo/factory")])
        self.assertEqual(targets("cd hooks && echo x > a", "/repo/factory"), [("a", "/repo/factory/hooks")])

    def test_shell_c_strings_and_here_documents_are_read(self) -> None:
        for command in [
            "bash -c 'echo x > factory/a'",
            'sh -lc "cp /tmp/a factory/a"',
            "bash <<'EOF'\necho x > factory/a\nEOF",
            "bash -s <<EOF\ntee factory/a\nEOF",
        ]:
            with self.subTest(command):
                self.assertIn("factory/a", paths(command))

    def test_interpreter_code_that_writes_gives_the_harness_paths_it_mentions(self) -> None:
        cases = {
            "python3 -c \"open('factory/a', 'w').write('x')\"": "factory/a",
            "python3 -c \"open('factory/a','a').write('x')\"": "factory/a",
            "python3 -c 'from pathlib import Path; Path(\".github/a\").write_text(\"x\")'": ".github/a",
            "python3 - <<'EOF'\nfrom pathlib import Path\nPath('.github/a').write_text('x')\nEOF": ".github/a",
            "python3 -c \"import shutil; shutil.copy('/tmp/a', 'factory/a')\"": "factory/a",
            "python3 -c \"import os; os.remove('.claude/a')\"": ".claude/a",
            "node -e \"require('fs').writeFileSync('factory/a', 'x')\"": "factory/a",
            "ruby -e \"File.write('.claude/a', 'x')\"": ".claude/a",
        }
        for command, expected in cases.items():
            with self.subTest(command):
                self.assertIn(expected, paths(command))


class ReadOnlyFormsTest(unittest.TestCase):
    """A command that only mentions a harness path gives no target."""

    def test_read_only_commands_give_nothing(self) -> None:
        cases = [
            "cat factory/a",
            "cat factory/a .github/b | head",
            "grep -rn x factory/ .github",
            "git diff -- factory",
            "git diff origin/main -- .claude/settings.json",
            "git show HEAD:factory/a",
            "git log --oneline -- .github",
            "ls -la .claude",
            "head -5 factory/a",
            "sed -n 1,5p factory/a",
            "sed 's/a/b/' factory/a",
            "cp factory/a /tmp/a",
            "cp -r .claude/ /tmp/backup",
            "echo x > /tmp/a",
            "cat factory/a > /tmp/b",
            "sed 's/a/b/' factory/a > /tmp/b",
            "echo x > docs/factory.md",
            "git commit -F /tmp/msg.txt",
            "make check 2>&1 | tail -5",
            "ls > /dev/null 2>&1",
            "cd factory && ls",
            "tee /tmp/a < factory/a",
            "python3 -I -m unittest discover -s factory/tests",
            "python3 -c \"print(open('factory/a').read())\"",
            "python3 -c \"from pathlib import Path; print(Path('.github/a').read_text())\"",
            "python3 - <<'EOF'\nprint(open('factory/a').read())\nEOF",
            "python3 -c \"open('/tmp/a', 'w').write('x')\"",
            "node -e \"console.log(require('fs').readFileSync('factory/a', 'utf8'))\"",
            "rtk grep foo factory",
            "echo ok",
            "",
        ]
        for command in cases:
            with self.subTest(command):
                self.assertEqual(targets(command), [])

    def test_text_in_a_heredoc_body_is_data(self) -> None:
        for command in [
            "cat > /tmp/note.md <<'EOF'\n> factory/a is read-only\ncp x factory/y\nEOF",
            "git commit -F - <<'EOF'\nrm -rf .github\nEOF",
        ]:
            with self.subTest(command):
                self.assertEqual(targets(command), [])

    def test_a_command_without_a_harness_word_is_not_parsed(self) -> None:
        with mock.patch.object(harness_bash_writes, "_scan") as scan:
            self.assertEqual(targets("echo x > backend/a"), [])

        scan.assert_not_called()

    def test_a_shell_that_sits_in_a_harness_folder_writes_there(self) -> None:
        self.assertEqual(targets("echo x > a", "/repo/factory"), [("a", "/repo/factory")])
        self.assertEqual(targets("echo x > /tmp/a", "/repo/factory"), [])

    def test_a_command_the_lexer_cannot_read_does_not_crash(self) -> None:
        self.assertEqual(paths("cat <<EOF\nit's > factory/a\nEOF\nrm 'x"), [])


if __name__ == "__main__":
    unittest.main()
