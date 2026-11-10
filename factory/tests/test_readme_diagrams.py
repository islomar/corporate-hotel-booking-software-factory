"""The hook diagram in factory/README.md lists every hook that .claude/settings.json registers (CHB-170).

A hook added to the settings without a node in the diagram leaves the diagram wrong, and nobody notices
until a denied command puzzles someone. This test fails then.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK_FILE = re.compile(r"factory/hooks/([A-Za-z0-9_]+\.py)")
MERMAID_BLOCK = re.compile(r"^```mermaid\n(.*?)^```", re.DOTALL | re.MULTILINE)


def registered_hook_files() -> list[str]:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    files = []
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                match = HOOK_FILE.search(hook["command"])
                if match is None:
                    raise AssertionError(f"no hook file in command: {hook['command']}")
                files.append(match.group(1))
    return files


def mermaid_text() -> str:
    readme = (ROOT / "factory" / "README.md").read_text()
    return "\n".join(MERMAID_BLOCK.findall(readme))


class ReadmeDiagramsTest(unittest.TestCase):
    def test_settings_register_hooks(self):
        self.assertGreaterEqual(len(registered_hook_files()), 10)

    def test_every_registered_hook_file_is_in_a_mermaid_block(self):
        diagrams = mermaid_text()
        missing = [name for name in registered_hook_files() if name not in diagrams]
        self.assertEqual(
            [],
            missing,
            "Add these hooks to the hook diagram in factory/README.md (inside a ```mermaid block)",
        )

    def test_the_diagram_has_no_hook_that_settings_do_not_register(self):
        registered = set(registered_hook_files())
        in_diagram = set(re.findall(r"\b(?:check|deny|warn|validating)_[a-z_]+\.py", mermaid_text()))
        self.assertEqual(set(), in_diagram - registered, "Remove these from the hook diagram")


if __name__ == "__main__":
    unittest.main()
