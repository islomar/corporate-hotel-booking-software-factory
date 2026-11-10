import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_secret_reads as hook

ROOT = Path(__file__).resolve().parents[2]
HOME = "/home/u"

SECRET_PATHS = [
    "/home/u/.ssh/id_ed25519",
    "/home/u/.ssh/config",
    "/home/u/.ssh",
    "/root/.ssh/id_rsa",
    "/repo/certs/server.pem",
    "/repo/tls/server.key",
    "/repo/.env",
    "/repo/backend/.env.local",
    "/repo/.env.production",
    "/home/u/.config/op/config",
    "/home/u/.config/op",
]

OTHER_PATHS = [
    "/repo/.env.example",
    "/repo/backend/.env.example",
    "/repo/.envrc",
    "/repo/docs/env.md",
    "/repo/src/keys.py",
    "/repo/src/monkey.ts",
    "/repo/.config/opt/x",
    "/repo/sshd.md",
    "/repo/.key",
    "/repo/.tls.key",  # known gap: a hidden key file reads like a jq filter such as `.foo.key`
]

DENIED = [
    "cat ~/.ssh/id_ed25519",
    "cat $HOME/.ssh/id_ed25519",
    "cat ${HOME}/.ssh/id_rsa",
    "cat /home/u/.ssh/id_rsa",
    "head -5 ~/.ssh/config",
    "grep -r Host ~/.ssh",
    "cat ~/.ssh/*",
    "less server.pem",
    "cat certs/*.pem",
    "base64 tls/server.key",
    "cat .env",
    "cat backend/.env.local",
    "cat .env*",
    "source .env",
    ". ./.env",
    "set -a; . .env; set +a",
    "cat < .env",
    "while read l; do echo $l; done < .env",
    "python3 -c 'print(1)' < ~/.ssh/id_rsa",
    "git show HEAD:.env",
    "scp host:~/.ssh/id_rsa /tmp/x",
    "cp .env /tmp/x",
    "awk 1 --file=.env",
    "cat ~/.config/op/config",
    "ls x && cat .env | head",
    "export $(cat .env | xargs)",
    "rtk cat .env",
    "secret-tool lookup service chb",
    "/usr/bin/secret-tool lookup service chb",
    "secret-tool --verbose lookup service chb",
    "op read op://vault/item/field",
    "op --account my read op://vault/item/field",
    "TOKEN=$(op read op://v/i/f) && echo ok",
    "rtk proxy op read op://v/i/f",
    "cat .e*",
    "cat .en?",
    "cat ~/.ss*/id_rsa",
    "cat ~/.config/o?/config",
    "cat certs/*.pe?",
    "cat tls/x.k*",
    "ssh -i ~/.ssh/k host grep -F ~/.ssh/config",
    'echo "$(cat .env)"',
    'X="$(cat ~/.ssh/id_rsa)"; echo $X',
    "echo `cat ~/.ssh/id_rsa`",
    'echo "token: `op read op://v/i/f`"',
    "bash -c 'cat .env'",
    'sh -c "head ~/.ssh/config"',
    "ssh localhost cat ~/.ssh/id_rsa",
    "ssh host 'cat .env'",
    "ssh -i ~/.ssh/id_ed25519 host cat .env",
]

ALLOWED = [
    "cat .env.example",
    "cp .env.example backend/.env.example",
    "git add .env.example",
    "cat .envrc",
    "ls ~/.ssh",
    "ls -la ~/.ssh/",
    "stat ~/.ssh/id_ed25519",
    "test -f .env && echo yes",
    "chmod 600 ~/.ssh/id_ed25519",
    "ssh -i ~/.ssh/id_ed25519 host",
    "ssh -i~/.ssh/id_ed25519 -F ~/.ssh/config host uptime",
    "ssh -o IdentityFile=~/.ssh/id_ed25519 host",
    "ssh -o StrictHostKeyChecking=no -p 2222 -i ~/.ssh/id_ed25519 host uptime",
    "scp -i ~/.ssh/id_ed25519 build.tar host:/srv/",
    'echo "$(date)"',
    "grep -rn TODO *",
    "cat *.md",
    "cat *.*",
    "wc -l src/*",
    "cat .github/*.yml",
    "bash -c 'make check'",
    "ssh-add ~/.ssh/id_ed25519",
    "echo .env >> .gitignore",
    "printf '%s\\n' .env >> .gitignore",
    "echo x > .env",
    "jq .key data.json",
    "gh pr view 1 --json headRefName --jq .key",
    "python3 -c \"print(x.key)\"",
    "jq '.foo.key' data.json",
    "grep -rn 'secret-tool lookup' docs/",
    "grep secret-tool README.md",
    "op whoami",
    "op item list",
    "factory/bin/jira view CHB-1",
    "factory/bin/as-agent gh pr create --title 'CHB-30: x' --body-file /tmp/b",
    "factory/bin/as-agent git push -u origin CHB-30-harness-and-secrets-hooks",
    "make check",
    "uv run --env-file .env.example pytest tests/test_x.py",
    "cat README.md",
    "git commit -F /tmp/msg",
    "cat key.md monkey.ts",
]


class SecretPathTest(unittest.TestCase):
    def test_secret_paths_are_secret(self) -> None:
        for path in SECRET_PATHS:
            with self.subTest(path):
                self.assertTrue(hook.secret_path(path, home=HOME))

    def test_other_paths_are_not(self) -> None:
        for path in OTHER_PATHS:
            with self.subTest(path):
                self.assertFalse(hook.secret_path(path, home=HOME))

    def test_home_forms_expand(self) -> None:
        for word in ["~/.ssh/x", "$HOME/.ssh/x", "${HOME}/.config/op/x"]:
            with self.subTest(word):
                self.assertTrue(hook.secret_path(word, home=HOME))


class ReadTest(unittest.TestCase):
    def test_a_read_of_a_secret_path_is_denied(self) -> None:
        for path in ["~/.ssh/id_rsa", ".env", "backend/.env.test", "/repo/tls/server.key"]:
            with self.subTest(path):
                self.assertIsNotNone(hook.read_reason(path, home=HOME, cwd="/repo"))

    def test_a_relative_path_resolves_against_the_working_directory(self) -> None:
        self.assertIsNotNone(hook.read_reason("id_rsa", home=HOME, cwd=f"{HOME}/.ssh"))
        self.assertIsNone(hook.read_reason("id_rsa", home=HOME, cwd="/repo"))

    def test_an_empty_working_directory_falls_back_to_the_process_folder(self) -> None:
        with mock.patch.object(hook.os, "getcwd", return_value=f"{HOME}/.ssh"):
            code, _, _ = _main(json.dumps({"cwd": "", "tool_input": {"file_path": "id_rsa"}}))

        self.assertEqual(code, 2)

    def test_other_reads_are_allowed(self) -> None:
        for path in [".env.example", "README.md", "/repo/factory/hooks/x.py"]:
            with self.subTest(path):
                self.assertIsNone(hook.read_reason(path, home=HOME, cwd="/repo"))


class BashTest(unittest.TestCase):
    def test_commands_that_print_secrets_are_denied(self) -> None:
        for command in DENIED:
            with self.subTest(command):
                self.assertIsNotNone(hook.bash_reason(command, home=HOME))

    def test_other_commands_are_allowed(self) -> None:
        for command in ALLOWED:
            with self.subTest(command):
                self.assertIsNone(hook.bash_reason(command, home=HOME))

    def test_the_reason_names_the_secret_and_the_wrappers(self) -> None:
        why = hook.bash_reason("cat .env", home=HOME) or ""

        self.assertIn("`.env`", why)
        self.assertIn("factory/bin/as-agent", why)
        self.assertIn(".env.example", why)


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


def _event(tool: str, **tool_input: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": "/repo", "tool_input": tool_input})


class ContractTest(unittest.TestCase):
    def test_a_denied_read_exits_2_with_the_reason_on_stderr(self) -> None:
        code, out, err = _main(_event("Read", file_path="/repo/.env"))

        self.assertEqual((code, out), (2, ""))
        self.assertIn("Blocked", err)

    def test_a_denied_command_exits_2_with_the_reason_on_stderr(self) -> None:
        code, out, err = _main(_event("Bash", command="cat ~/.ssh/id_rsa"))

        self.assertEqual((code, out), (2, ""))
        self.assertIn("Blocked", err)

    def test_allowed_calls_exit_0_silently(self) -> None:
        self.assertEqual(_main(_event("Read", file_path="/repo/README.md")), (0, "", ""))
        self.assertEqual(_main(_event("Bash", command="make check")), (0, "", ""))

    def test_input_without_a_path_or_command_is_left_alone(self) -> None:
        self.assertEqual(_main(_event("Read"))[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_unreadable_input_is_denied(self) -> None:
        self.assertEqual(_main("not json")[0], 2)

    def test_an_error_in_the_matcher_denies(self) -> None:
        with mock.patch.object(hook, "bash_reason", side_effect=IndexError("boom")):
            code, _, err = _main(_event("Bash", command="ls"))

        self.assertEqual(code, 2)
        self.assertIn("hook failed (IndexError)", err)

    def test_project_settings_register_the_hook_on_read_and_bash(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        (group,) = [g for g in settings["hooks"]["PreToolUse"] if "deny_secret_reads.py" in g["hooks"][0]["command"]]

        self.assertEqual(set(group["matcher"].split("|")), {"Read", "Bash"})
        self.assertIn("python3 -I", group["hooks"][0]["command"])
        self.assertIn("factory/hooks/deny_secret_reads.py", group["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
