import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import check_human_word as hook

ROOT = Path(__file__).resolve().parents[2]

REFUSED = [
    "I will ask the owner.",
    "The owner's approval is pending.",
    "Owner: react 👍 if you agree.",
    "Tell the Owner now.",
    "This needs owner steps.",
    "Both owners agreed.",
    "It waits for the owners' answer.",
    "OWNER ACTION NEEDED",
]

ALLOWED = [
    "CODEOWNERS lists the reviewers.",
    "The repository owner gets the alert.",
    "The repo owner is the org.",
    "Use owner/repo in the URL.",
    "The file is root-owned.",
    "The human owns the decision.",
    "I will ask the human.",
    "The field `owner` holds the login.",
    "Read `.owner.login` from the payload.",
    "The organization owner pays.",
]


class FindTest(unittest.TestCase):
    def test_refuses_each_use_of_owner_for_the_person(self) -> None:
        for text in REFUSED:
            self.assertEqual(hook.find_problems(text), [text], text)

    def test_allows_technical_terms_and_the_human(self) -> None:
        for text in ALLOWED:
            self.assertEqual(hook.find_problems(text), [], text)

    def test_fenced_code_blocks_are_not_checked(self) -> None:
        for fence in ("```", "~~~"):
            text = f"Run this:\n\n{fence}bash\ngh api repos/x/y --jq .owner\necho the owner\n{fence}\n\nDone."
            self.assertEqual(hook.find_problems(text), [], fence)

    def test_inline_code_is_not_checked(self) -> None:
        self.assertEqual(hook.find_problems("The ticket said `the owner` before CHB-194."), [])

    def test_names_each_line_once_even_with_two_uses(self) -> None:
        text = "First line.\nThe owner asked the owner's team.\nLast line."
        self.assertEqual(hook.find_problems(text), ["The owner asked the owner's team."])

    def test_a_refused_use_next_to_an_allowed_one_is_still_refused(self) -> None:
        text = "The repository owner and the owner disagree."
        self.assertEqual(hook.find_problems(text), [text])


def _run(payload: object, state: str) -> tuple[int, str]:
    err = io.StringIO()
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        code = hook.main(stdin, state_folder=state)
    return code, err.getvalue()


def _event(message: object, session: str = "s1") -> dict[str, object]:
    return {"hook_event_name": "Stop", "session_id": session, "stop_hook_active": False, "last_assistant_message": message}


class ContractTest(unittest.TestCase):
    """Stop hook contract: https://code.claude.com/docs/en/hooks (Stop). Exit 2 blocks and feeds stderr to Claude."""

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.state = str(Path(self.folder.name) / "state")

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_refuses_the_stop_and_names_the_word_to_use(self) -> None:
        code, err = _run(_event("Ask the owner."), self.state)

        self.assertEqual(code, 2)
        self.assertIn("Ask the owner.", err)
        self.assertIn('"the human"', err)
        self.assertIn("CHB-194", err)

    def test_a_clean_reply_passes(self) -> None:
        self.assertEqual(_run(_event("Ask the human."), self.state), (0, ""))

    def test_refuses_once_per_session_for_the_same_findings(self) -> None:
        self.assertEqual(_run(_event("Ask the owner."), self.state)[0], 2)

        code, err = _run(_event("Ask the owner."), self.state)

        self.assertEqual(code, 0)
        self.assertIn("same findings", err)

    def test_new_findings_refuse_again(self) -> None:
        self.assertEqual(_run(_event("Ask the owner."), self.state)[0], 2)
        self.assertEqual(_run(_event("Owner: react."), self.state)[0], 2)

    def test_another_session_is_refused_too(self) -> None:
        self.assertEqual(_run(_event("Ask the owner.", "s1"), self.state)[0], 2)
        self.assertEqual(_run(_event("Ask the owner.", "s2"), self.state)[0], 2)

    def test_bad_input_never_blocks(self) -> None:
        for payload in ("not json", "[]", {"stop_hook_active": False}, _event(42)):
            self.assertEqual(_run(payload, self.state)[0], 0, payload)

    def test_an_unusable_state_folder_fails_open(self) -> None:
        blocker = Path(self.folder.name) / "file"
        blocker.write_text("x")

        code, err = _run(_event("Ask the owner."), str(blocker / "state"))

        self.assertEqual(code, 0)
        self.assertIn("skipped", err)

    def test_an_unexpected_error_fails_open(self) -> None:
        original = hook.find_problems
        hook.find_problems = lambda _: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[assignment]
        try:
            code, err = _run(_event("Ask the owner."), self.state)
        finally:
            hook.find_problems = original

        self.assertEqual(code, 0)
        self.assertIn("skipped", err)


class SettingsTest(unittest.TestCase):
    def test_registered_as_a_stop_hook(self) -> None:
        settings = json.loads((ROOT / ".claude/settings.json").read_text())
        commands = [h["command"] for entry in settings["hooks"]["Stop"] for h in entry["hooks"]]

        self.assertIn('python3 -I "${CLAUDE_PROJECT_DIR}/factory/hooks/check_human_word.py"', commands)


if __name__ == "__main__":
    unittest.main()
