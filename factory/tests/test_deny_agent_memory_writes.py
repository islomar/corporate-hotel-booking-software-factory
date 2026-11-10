import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_agent_memory_writes as hook

ROOT = Path(__file__).resolve().parents[2]
HOME = "/home/u"
MEMORY = f"{HOME}/.claude/projects/-home-u-workspace-corporate-hotel-booking-software-factory/memory"

DENIED = [
    f"{MEMORY}/MEMORY.md",
    f"{MEMORY}/chb-handoff-2026-11-07.md",
    f"{MEMORY}/sub/dir/note.md",
    f"{HOME}/.claude/projects/-home-u-workspace-other-worktree/memory/x.md",
    f"{MEMORY}/../memory/x.md",
    "~/.claude/projects/p/memory/x.md",
    "/root/.claude/projects/p/memory/x.md",
]

ALLOWED = [
    "/home/u/workspace/corporate-hotel-booking-software-factory/docs/knowledge/x.md",
    "/home/u/workspace/corporate-hotel-booking-software-factory/memory/x.md",
    f"{HOME}/.claude/projects/p/session.jsonl",
    f"{HOME}/.claude/projects/p/memory-notes.md",
    f"{HOME}/.claude/settings.json",
    f"{MEMORY}/../other.md",
]


class MatcherTest(unittest.TestCase):
    def test_paths_in_agent_memory_are_denied(self) -> None:
        for path in DENIED:
            with self.subTest(path):
                self.assertTrue(hook.in_agent_memory(path, home=HOME, cwd="/tmp"))

    def test_other_paths_are_allowed(self) -> None:
        for path in ALLOWED:
            with self.subTest(path):
                self.assertFalse(hook.in_agent_memory(path, home=HOME, cwd="/tmp"))

    def test_a_relative_path_resolves_against_the_working_directory(self) -> None:
        self.assertTrue(hook.in_agent_memory("memory/x.md", home=HOME, cwd=f"{HOME}/.claude/projects/p"))
        self.assertFalse(hook.in_agent_memory("memory/x.md", home=HOME, cwd="/repo"))


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


def _event(tool: str, **tool_input: str) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": "/repo", "tool_input": tool_input})


class ContractTest(unittest.TestCase):
    def test_each_file_tool_is_denied_in_memory_with_the_reason_on_stderr(self) -> None:
        path = str(Path.home() / ".claude/projects/p/memory/MEMORY.md")
        for tool, key in [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]:
            with self.subTest(tool):
                code, out, err = _main(_event(tool, **{key: path}))

                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("Blocked", err)
                self.assertIn("factory/bin/jira", err)
                self.assertIn("docs/knowledge/", err)
                self.assertIn("ADR-eedb", err)

    def test_a_write_in_the_repository_exits_0_silently(self) -> None:
        self.assertEqual(_main(_event("Write", file_path="/repo/docs/knowledge/x.md")), (0, "", ""))

    def test_input_without_a_path_is_left_alone(self) -> None:
        self.assertEqual(_main(_event("Bash", command="ls"))[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_unreadable_input_is_denied(self) -> None:
        self.assertEqual(_main("not json")[0], 2)

    def test_an_error_in_the_matcher_denies(self) -> None:
        with mock.patch.object(hook, "in_agent_memory", side_effect=ValueError("boom")):
            code, _, err = _main(_event("Write", file_path="/repo/x"))

        self.assertEqual(code, 2)
        self.assertIn("hook failed (ValueError)", err)

    def test_project_settings_register_the_hook_on_file_tools(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        groups = [g for g in settings["hooks"]["PreToolUse"] if "deny_agent_memory_writes.py" in g["hooks"][0]["command"]]

        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["matcher"].split("|")), {"Write", "Edit", "MultiEdit", "NotebookEdit"})


if __name__ == "__main__":
    unittest.main()
