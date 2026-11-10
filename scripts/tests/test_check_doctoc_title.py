import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_doctoc_title import DEFAULT_TITLE, findings, main, markdown_files

# The marker is built from parts so this file does not hold a doctoc block itself.
START = "<!-- START " + "doctoc generated TOC please keep comment here to allow auto update -->"
DONT_EDIT = "<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->"
END = "<!-- END " + "doctoc generated TOC please keep comment here to allow auto update -->"


def toc(title: str) -> str:
    return f"{START}\n{DONT_EDIT}\n{title}\n\n- [Usage](#usage)\n\n{END}\n\n# Usage\n"


class FindingsTest(unittest.TestCase):
    def test_default_title_passes(self):
        self.assertEqual(findings(toc(DEFAULT_TITLE)), [])

    def test_default_title_is_doctocs_byte_for_byte(self):
        self.assertEqual(
            DEFAULT_TITLE, "**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*"
        )

    def test_contents_heading_fails(self):
        self.assertEqual(findings(toc("## Contents")), [(3, "## Contents")])

    def test_missing_title_fails(self):
        self.assertEqual(findings(toc("")), [(3, "")])

    def test_text_without_a_doctoc_block_passes(self):
        self.assertEqual(findings("# Title\n\n## Contents\n"), [])

    def test_a_quoted_marker_is_not_a_block(self):
        self.assertEqual(findings(f"Run doctoc; it writes `{START}`.\n\n## Contents\n"), [])

    def test_every_block_is_checked(self):
        text = toc(DEFAULT_TITLE) + "\n" + toc("## Contents")
        self.assertEqual(findings(text), [(13, "## Contents")])

    def test_a_marker_on_the_last_line_fails(self):
        self.assertEqual(findings(f"text\n{START}\n"), [(4, "")])


class MarkdownFilesTest(unittest.TestCase):
    def test_keeps_markdown_and_drops_worktrees(self):
        listed = ["README.md", "docs/a.md", "scripts/x.py", ".claude/worktrees/agent-1/README.md"]
        self.assertEqual(markdown_files(listed), ["README.md", "docs/a.md"])


class MainTest(unittest.TestCase):
    def run_main(self, files: dict[str, str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in files.items():
                path = Path(tmp, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(Path(tmp), sorted(files))
            return code, out.getvalue()

    def test_default_titles_pass(self):
        self.assertEqual(self.run_main({"README.md": toc(DEFAULT_TITLE), "docs/a.md": "# A\n"})[0], 0)

    def test_another_title_fails_with_file_line_and_fix(self):
        code, out = self.run_main({"README.md": toc(DEFAULT_TITLE), "docs/a.md": toc("## Contents")})
        self.assertEqual(code, 1)
        self.assertIn("file=docs/a.md,line=3::", out)
        self.assertIn("'## Contents'", out)
        self.assertIn(f"doctoc --title '{DEFAULT_TITLE}' docs/a.md", out)
        self.assertNotIn("README.md", out)

    def test_a_listed_file_that_is_gone_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(Path(tmp), ["deleted.md"]), 0)

    def test_the_repository_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
