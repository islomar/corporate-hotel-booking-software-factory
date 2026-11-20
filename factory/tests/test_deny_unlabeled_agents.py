import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import deny_unlabeled_agents as hook

ROOT = Path(__file__).resolve().parents[2]

ALLOWED = [
    "CHB-23: request limits",
    "no-ticket: read a page",
    "CHB-190: agent label hook",
    "CHB-7:x",
]

DENIED = [
    "Implement CHB-23",
    "",
    "chb-23: request limits",
    "CHB-23 request limits",
    "CHB-: request limits",
    "CHB-23x: request limits",
    "No-ticket: read a page",
    "no ticket: read a page",
    " CHB-23: request limits",
    "Explore codebase",
]


class MatcherTest(unittest.TestCase):
    def test_labels_that_start_with_a_key_or_no_ticket_are_allowed(self) -> None:
        for label in ALLOWED:
            with self.subTest(label):
                self.assertTrue(hook.labelled(label))

    def test_other_labels_are_denied(self) -> None:
        for label in DENIED:
            with self.subTest(label):
                self.assertFalse(hook.labelled(label))


def _main(stdin: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = hook.main()
    return code, out.getvalue(), err.getvalue()


def _event(tool_input: object) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": "/repo", "tool_input": tool_input})


def _launch(description: str) -> str:
    return _event({"description": description, "prompt": "do the work", "subagent_type": "general-purpose"})


class ContractTest(unittest.TestCase):
    def test_a_labelled_launch_exits_0_silently(self) -> None:
        self.assertEqual(_main(_launch("CHB-23: request limits")), (0, "", ""))
        self.assertEqual(_main(_launch("no-ticket: read a page")), (0, "", ""))

    def test_an_unlabelled_launch_is_denied_and_the_message_shows_both_forms(self) -> None:
        for label in ["Implement CHB-23", "", "chb-23: request limits"]:
            with self.subTest(label):
                code, out, err = _main(_launch(label))

                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("Blocked", err)
                self.assertIn("CHB-<n>:", err)
                self.assertIn("no-ticket:", err)
                self.assertIn("CHB-190", err)

    def test_the_message_quotes_the_refused_label(self) -> None:
        _, _, err = _main(_launch("Implement CHB-23"))

        self.assertIn("Implement CHB-23", err)

    # Fail open: a hook that crashes or meets an input shape it does not know must not block every agent launch.
    def test_unreadable_input_is_allowed(self) -> None:
        self.assertEqual(_main("not json")[0], 0)
        self.assertEqual(_main("[]")[0], 0)

    def test_a_tool_input_that_is_not_an_object_is_allowed(self) -> None:
        self.assertEqual(_main(_event("text"))[0], 0)
        self.assertEqual(_main(json.dumps({"tool_name": "Agent"}))[0], 0)

    def test_a_missing_or_non_string_description_is_allowed(self) -> None:
        self.assertEqual(_main(_event({"prompt": "do the work"}))[0], 0)
        self.assertEqual(_main(_event({"description": 7, "prompt": "do the work"}))[0], 0)

    def test_an_error_in_the_matcher_allows_with_a_note_on_stderr(self) -> None:
        with mock.patch.object(hook, "labelled", side_effect=ValueError("boom")):
            code, out, err = _main(_launch("Implement CHB-23"))

        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("ValueError", err)

    def test_project_settings_register_the_hook_on_the_agent_tool(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        groups = [g for g in settings["hooks"]["PreToolUse"] if "deny_unlabeled_agents.py" in g["hooks"][0]["command"]]

        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["matcher"].split("|")), {"Agent"})


if __name__ == "__main__":
    unittest.main()
