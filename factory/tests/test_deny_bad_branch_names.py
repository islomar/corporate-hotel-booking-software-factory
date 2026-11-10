import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_bad_branch_names as hook

ROOT = Path(__file__).resolve().parents[2]

DENIED = [
    "git switch -c my-feature",
    "git switch -cmy-feature",
    "git switch --create my-feature origin/main",
    "git switch --create=my-feature",
    "git switch -C feature/x",
    "git checkout -b fix",
    "git checkout -B CHB-54",
    "git checkout --orphan scratch",
    "git checkout -b chb-54-lowercase-key",
    "git checkout -b CHB-54-Upper-Slug",
    "git checkout -b CHB-54-trailing-",
    "git checkout -b CHB-x-slug",
    "git branch my-feature",
    "git branch my-feature origin/main",
    "git branch -f my-feature",
    "git branch --track my-feature origin/main",
    "git branch -m new-name",
    "git branch -M CHB-1-old wrong",
    "git worktree add -b feature ../wt",
    "git push origin my-feature",
    "git push -u origin feature/x",
    "git push --force-with-lease origin +my-feature",
    "git push origin CHB-1-ok:wrong-name",
    "git push origin HEAD:refs/heads/wrong-name",
    "git -C /repo checkout -b bad",
    "git -c user.name=x checkout -b bad",
    "cd /repo && git checkout -b bad",
    "rtk git checkout -b bad",
    "rtk proxy git checkout -b bad",
    "factory/bin/as-agent git push -u origin bad-name",
    "/home/u/repo/factory/bin/as-agent git push origin bad-name",
    "env GIT_TRACE=1 git switch -c bad",
    "git status; git checkout -b bad",
    "git fetch origin\ngit checkout -b bad origin/main",
    "git checkout -b bad && cat <<'EOF' > /tmp/x\ndon't\nEOF",
]

ALLOWED = [
    "git switch -c CHB-54-guardrail-hooks",
    "git switch -c CHB-54-guardrail-hooks origin/main",
    "git checkout -b CHB-1-x",
    "git checkout -b CHB-123-a1-b2-c3",
    "git branch CHB-54-guardrail-hooks origin/main",
    "git branch -m CHB-54-guardrail-hooks",
    "git switch -c main",
    "git checkout -b dependabot/npm_and_yarn/frontend/vite-7.1.0",
    "git push origin dependabot/pip/backend/ruff-0.9.0",
    "git switch -c worktree-agent-aed3cdf8e34319d39",
    "git worktree add -b worktree-agent-abc .claude/worktrees/agent-abc",
    "git push -u origin CHB-54-guardrail-hooks",
    "factory/bin/as-agent git push -u origin CHB-54-guardrail-hooks",
    "rtk git push origin main",
    "git push",
    "git push origin",
    "git push -u origin HEAD",
    "git push origin :old-branch",
    "git push origin --delete old-branch",
    "git push -d origin old-branch",
    "git push origin --tags",
    "git push origin refs/tags/v1.0",
    'git push -u origin "$(git branch --show-current)"',
    "git push -u origin $(git branch --show-current)",
    'git push origin "$BRANCH"',
    "git branch",
    "git branch -a",
    "git branch -vv",
    "git branch --show-current",
    "git branch --list 'CHB-*'",
    "git branch -d old-branch",
    "git branch -D old-branch",
    "git branch --delete old-branch",
    "git branch -dr origin/old-branch",
    "git branch --merged main",
    "git branch --contains abc123",
    "git branch -u origin/main",
    "git branch --set-upstream-to=origin/main",
    "git checkout main",
    "git checkout -- src/file.py",
    "git switch CHB-12-old",
    "git switch -",
    "git worktree add ../wt CHB-12-old",
    "git worktree list",
    "git log --oneline -1",
    "git commit -m 'git checkout -b not-a-branch'",
    "cat <<'EOF' > /tmp/msg\ndon't stop\nEOF",
    "echo it's",
    "make check",
    "factory/bin/jira transition CHB-54 'In Review'",
    "factory/bin/as-agent gh pr create --title 'CHB-54: x' --body-file /tmp/b",
    "gh pr checks 67 --watch",
]


class MatcherTest(unittest.TestCase):
    def test_commands_that_create_or_push_a_badly_named_branch_are_denied(self) -> None:
        for command in DENIED:
            with self.subTest(command):
                self.assertIsNotNone(hook.reason(command))

    def test_other_commands_are_allowed(self) -> None:
        for command in ALLOWED:
            with self.subTest(command):
                self.assertIsNone(hook.reason(command))

    def test_reason_names_the_branch_and_the_expected_format(self) -> None:
        why = hook.reason("git checkout -b fix-login") or ""

        self.assertIn("`fix-login`", why)
        self.assertIn("CHB-<n>-<slug>", why)
        self.assertIn("^CHB-[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$", why)


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


def _event(command: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}})


class ContractTest(unittest.TestCase):
    """The Claude Code hook contract: exit 2 with the reason on stderr denies the tool call."""

    def test_denied_command_exits_2_with_reason_on_stderr(self) -> None:
        code, out, err = _main(_event("git checkout -b bad"))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Blocked", err)

    def test_allowed_command_exits_0_silently(self) -> None:
        self.assertEqual(_main(_event("git checkout -b CHB-1-x")), (0, "", ""))

    def test_input_without_a_command_is_left_alone(self) -> None:
        self.assertEqual(_main(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/x"}}))[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_unreadable_input_is_denied(self) -> None:
        self.assertEqual(_main("not json")[0], 2)

    def test_an_error_in_the_matcher_denies(self) -> None:
        with mock.patch.object(hook, "reason", side_effect=IndexError("boom")):
            code, _, err = _main(_event("ls"))

        self.assertEqual(code, 2)
        self.assertIn("hook failed (IndexError)", err)

    def test_project_settings_register_the_hook_on_bash(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        (group,) = [g for g in settings["hooks"]["PreToolUse"] if "deny_bad_branch_names.py" in g["hooks"][0]["command"]]

        self.assertEqual(group["matcher"], "Bash")
        self.assertIn("python3 -I", group["hooks"][0]["command"])
        self.assertIn("factory/hooks/deny_bad_branch_names.py", group["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
