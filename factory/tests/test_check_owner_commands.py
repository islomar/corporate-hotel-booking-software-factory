import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import check_owner_commands as hook

ROOT = Path(__file__).resolve().parents[2]


def block(*lines: str, lang: str = "") -> str:
    return f"Run this:\n\n```{lang}\n" + "\n".join(lines) + "\n```\n"


FLAGGED = [
    # The CHB-126 incident: the target depended on the folder the owner was in.
    "cp /tmp/x/scratchpad/git-config.bak .git/config",
    "mv backup.txt notes/backup.txt",
    "rm -rf build",
    "echo x > .env",
    "sed -i 's/a/b/' config.toml",
    "git pull && make infra-plan",
    "git status",
    "make check",
    "factory/bin/jira view CHB-1",
    "./scripts/run.sh",
    "sudo cp new.conf /etc/x.conf",
]

ALLOWED = [
    "cp /tmp/x/git-config.bak ~/workspace/repo/.git/config",
    "cd ~/workspace/repo && git pull && make infra-plan",
    "cd /home/u/repo && cp a.txt b.txt",
    "git -C ~/workspace/repo status",
    "make -C ~/workspace/repo check",
    "~/workspace/repo/factory/bin/jira view CHB-1",
    "secret-tool lookup service chb-factory key op-service-account-token | wc -c",
    "gh pr view 45",
    "! factory/bin/jira view CHB-1",
    "# a comment: cp a b",
    "echo hello",
    "rm -rf /tmp/x/scratch",
]


class FindTest(unittest.TestCase):
    def test_flags_commands_that_depend_on_the_current_folder(self) -> None:
        for line in FLAGGED:
            self.assertEqual(hook.find_problems(block(line)), [line], line)

    def test_allows_commands_that_work_from_any_folder(self) -> None:
        for line in ALLOWED:
            self.assertEqual(hook.find_problems(block(line)), [], line)

    def test_a_leading_cd_to_an_absolute_folder_anchors_the_whole_block(self) -> None:
        text = block("cd ~/workspace/repo", "git pull", "make infra-plan")

        self.assertEqual(hook.find_problems(text), [])

    def test_a_relative_cd_does_not_anchor(self) -> None:
        self.assertEqual(hook.find_problems(block("cd repo", "git pull")), ["cd repo", "git pull"])

    def test_only_shell_blocks_are_checked(self) -> None:
        for lang in ("text", "python", "json", "markdown", "diff"):
            self.assertEqual(hook.find_problems(block("git status", lang=lang)), [], lang)
        for lang in ("sh", "bash", "shell", "zsh", "console"):
            self.assertEqual(hook.find_problems(block("git status", lang=lang)), ["git status"], lang)

    def test_inline_code_and_prose_are_not_checked(self) -> None:
        self.assertEqual(hook.find_problems("Then run `git status` and `cp a b`."), [])

    def test_backslash_continuations_form_one_command(self) -> None:
        text = block("cp /tmp/a \\", "  .git/config")

        self.assertEqual(hook.find_problems(text), ["cp /tmp/a .git/config"])


def _run(payload: object) -> tuple[int, str]:
    out = io.StringIO()
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = hook.main(stdin)
    return code, out.getvalue()


class ContractTest(unittest.TestCase):
    """Stop hook contract: https://code.claude.com/docs/en/hooks (Stop)."""

    def test_blocks_the_stop_and_names_each_command(self) -> None:
        code, out = _run({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": block("git status")})

        self.assertEqual(code, 0)
        decision = json.loads(out)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("git status", decision["reason"])
        self.assertIn("CHB-126", decision["reason"])

    def test_a_clean_message_lets_the_stop_through(self) -> None:
        self.assertEqual(_run({"stop_hook_active": False, "last_assistant_message": block("gh pr view 45")}), (0, ""))

    def test_a_second_stop_after_a_block_goes_through(self) -> None:
        # One reminder per reply. stop_hook_active avoids a loop when the reply cannot be fixed.
        self.assertEqual(_run({"stop_hook_active": True, "last_assistant_message": block("git status")}), (0, ""))

    def test_bad_input_never_blocks(self) -> None:
        for payload in ("not json", "[]", {"stop_hook_active": False}):
            self.assertEqual(_run(payload), (0, ""), payload)


class SettingsTest(unittest.TestCase):
    def test_registered_as_a_stop_hook(self) -> None:
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        commands = [h["command"] for entry in settings["hooks"]["Stop"] for h in entry["hooks"]]

        self.assertIn('python3 -I "${CLAUDE_PROJECT_DIR}/factory/hooks/check_owner_commands.py"', commands)


if __name__ == "__main__":
    unittest.main()
