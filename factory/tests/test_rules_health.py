"""CHB-167: the deterministic checks of the rules health check."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rules_health as rh

ROOT = Path(__file__).resolve().parents[2]


def tree(files: dict[str, str]) -> Path:
    base = Path(tempfile.mkdtemp())
    for name, text in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return base


class CodeSpans(unittest.TestCase):
    def test_inline_spans_carry_their_line_number(self):
        spans = rh.code_spans("a `one` b\n\nc `two` `three`\n")
        self.assertEqual(spans, [(1, "one"), (3, "two"), (3, "three")])

    def test_fenced_lines_are_not_inline_spans(self):
        self.assertEqual(rh.code_spans("```sh\nmake check\n```\n`x`\n"), [(4, "x")])


class DeadPaths(unittest.TestCase):
    def setUp(self):
        self.root = tree({"docs/a.md": "x", "factory/hooks/h.py": "x", "infra/github/r.tf": "x", "docs/process/k.md": "x"})

    def check(self, text: str) -> list[str]:
        return [f["claim"] for f in rh.dead_paths(text, "docs/process/k.md", self.root)]

    def test_existing_paths_pass_from_root_and_from_the_file_folder(self):
        self.assertEqual(self.check("`docs/a.md` `k.md` `factory/hooks/h.py` `hooks/h.py`"), [])

    def test_infra_relative_path_resolves(self):
        self.assertEqual(self.check("`github/r.tf`"), [])

    def test_missing_path_is_reported_once_per_claim(self):
        self.assertEqual(self.check("`docs/gone.md` and `docs/gone.md`"), ["docs/gone.md"])

    def test_line_suffix_anchor_and_trailing_slash_are_stripped(self):
        self.assertEqual(self.check("`docs/a.md:12` `docs/a.md#top` `docs/`"), [])

    def test_tokens_that_are_not_paths_are_ignored(self):
        text = (
            "`In Review/Validating` `https://x.io/a.md` `docs/*.md` `<dir>/a.md` `~/x/y.md` "
            "`refs/heads/main` `docs/sessions/x.md` `a b/c.md` `$HOME/x.md`"
        )
        self.assertEqual(self.check(text), [])

    def test_a_finding_names_file_line_and_stable_id(self):
        found = rh.dead_paths("a\n`docs/gone.md`\n", "CLAUDE.md", self.root)
        self.assertEqual(found[0]["file"], "CLAUDE.md")
        self.assertEqual(found[0]["line"], 2)
        self.assertEqual(found[0]["check"], "dead-path")
        again = rh.dead_paths("\n\n\n`docs/gone.md`\n", "CLAUDE.md", self.root)
        self.assertEqual(found[0]["id"], again[0]["id"])


class DeadCommands(unittest.TestCase):
    def check(self, text: str) -> list[str]:
        return [f["claim"] for f in rh.dead_commands(text, "CLAUDE.md", {"view", "ready"}, {"check", "dev"})]

    def test_known_commands_pass(self):
        self.assertEqual(self.check("`factory/bin/jira view CHB-1` `make check`\n```sh\nfactory/bin/jira ready CHB-2\n```"), [])

    def test_unknown_jira_subcommand_is_reported(self):
        self.assertEqual(self.check("`factory/bin/jira nope CHB-1`"), ["factory/bin/jira nope"])

    def test_unknown_make_target_is_reported(self):
        self.assertEqual(self.check("run `make gone`"), ["make gone"])

    def test_prose_make_and_flags_are_ignored(self):
        self.assertEqual(self.check("make sure it works. `factory/bin/jira --help`"), [])

    def test_jira_subcommands_are_read_from_the_source(self):
        source = 'a = commands.add_parser("view", help="x")\nb = commands.add_parser("sort-todo")\n'
        self.assertEqual(rh.jira_subcommands(source), {"view", "sort-todo"})

    def test_make_targets_are_read_from_the_makefile(self):
        text = ".PHONY: a\nhelp: ## List\ncheck: check-a check-b ## Gate\n\tcmd\nVAR = x\n"
        self.assertEqual(rh.make_targets(text), {"help", "check"})


class ExitCodes(unittest.TestCase):
    def test_constants_are_read_with_ast(self):
        source = "EXIT_JIRA = 1\nEXIT_BLOCKED = 4  # why\nOTHER = 9\nEXIT_X = 'no'\n"
        self.assertEqual(rh.exit_codes(source), {1, 4})

    def test_known_code_passes_and_unknown_is_reported(self):
        found = rh.unknown_exit_codes("It exits 5. Exit code 3. exit 0. If it exits 7 then stop.", "k.md", {1, 2, 3, 4, 5})
        self.assertEqual([f["claim"] for f in found], ["exit 7"])

    def test_line_number(self):
        found = rh.unknown_exit_codes("a\nexits 9\n", "k.md", {1})
        self.assertEqual(found[0]["line"], 2)


class Hooks(unittest.TestCase):
    def test_unregistered_and_missing_hooks(self):
        root = tree(
            {
                "factory/hooks/a.py": "if __name__ == '__main__':\n    pass\n",
                "factory/hooks/b.py": "if __name__ == '__main__':\n    pass\n",
                "factory/hooks/helper.py": "X = 1\n",
                ".claude/settings.json": '{"c": "python3 -I \\"${CLAUDE_PROJECT_DIR}/factory/hooks/a.py\\" factory/hooks/gone.py"}',
            }
        )
        found = rh.hook_registration(root)
        self.assertEqual(sorted(f["claim"] for f in found), ["factory/hooks/b.py", "factory/hooks/gone.py"])


class Run(unittest.TestCase):
    def test_the_repository_is_clean(self):
        """The checks run on this repository; a finding here is a rule or a path that went stale."""
        found = rh.run(ROOT)
        self.assertEqual(found, [], json.dumps(found, indent=1))

    def test_documents_exclude_sessions_and_postmortems(self):
        root = tree({"CLAUDE.md": "x", "docs/sessions/s.md": "x", "docs/postmortems/p.md": "x", "docs/process/k.md": "x"})
        names = [str(p.relative_to(root)) for p in rh.documents(root)]
        self.assertEqual(sorted(names), ["CLAUDE.md", "docs/process/k.md"])

    def test_main_prints_json_and_exits_1_with_findings(self):
        root = tree({"CLAUDE.md": "`docs/gone.md`\n", "factory/jira.py": "", "factory/as_agent.py": "", "Makefile": "", ".claude/settings.json": "{}"})
        out = io.StringIO()
        self.assertEqual(rh.main(["--root", str(root)], out), 1)
        self.assertEqual(json.loads(out.getvalue())["findings"][0]["claim"], "docs/gone.md")

    def test_main_exits_0_when_clean(self):
        root = tree({"CLAUDE.md": "x\n", "factory/jira.py": "", "factory/as_agent.py": "", "Makefile": "", ".claude/settings.json": "{}"})
        out = io.StringIO()
        self.assertEqual(rh.main(["--root", str(root)], out), 0)


if __name__ == "__main__":
    unittest.main()
