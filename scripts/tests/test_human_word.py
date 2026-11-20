"""The repository calls the person "the human", never "the owner" (CHB-194).

`git grep -i -w` finds every "owner" and "owners" in the paths below. A line fails unless each use
sits inside one of the technical uses listed here: a GitHub or Google term, an API field, a CLI flag,
or a file that names the old word on purpose. To add an entry, give the reason in its comment.
The Stop hook factory/hooks/check_human_word.py applies the common technical uses to the agent's replies.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATHS = ("CLAUDE.md", "docs", "factory", "scripts", ".github", "infra", "Makefile", "TODO.md", "CONTRIBUTING.md")
EXCLUDED = ("docs/sessions",)  # session exports are data, never edited (CLAUDE.md)
WORD = re.compile(r"\bowners?\b", re.IGNORECASE)

# Technical uses in any file.
ANYWHERE = (
    r"code[ -]owners?",  # GitHub code owners (CODEOWNERS, code-owner review)
    r"(?:repository|repo|organization|organisation|org)[ _]owner",  # the GitHub account that owns the repository or organization
    r"owner/(?:repo|repository|name)\b",  # the OWNER/NAME form of a repository
    r"\$owner\b|\(owner: |owner \{ login \}|owner=\{owner\}",  # GitHub GraphQL and gh api placeholders
    r"[\"']owner[\"']",  # the "owner" field of a GitHub API answer
    r"--owner\b",  # CLI flags: check_supply_chain.py --owner, cp/install --owner
    r"\.owner\b",  # a JSON path such as .owner.login
    r"roles/owner\b",  # the Google Cloud role
    r"`[^`]*`",  # inline code: a quoted word, path or command, as the Stop hook skips it
)

# Technical uses in one file.
IN_FILE = {
    # The hook and its tests name the word it refuses.
    "factory/hooks/check_human_word.py": (r".*",),
    "factory/tests/test_check_human_word.py": (r".*",),
    "scripts/tests/test_human_word.py": (r".*",),
    # The wrapper still reads the headings and box text of tickets written before CHB-194.
    "factory/jira.py": (r"Needs human: owner steps|No owner decision|called the person \"owner\"",),
    "factory/tests/test_jira.py": (r"Needs human: owner steps|No owner decision|owner asked|assertNotIn\(\"owner\"",),
    # The login of the repository owner (github.repository_owner), passed as --owner.
    "scripts/check_supply_chain.py": (r"\bowner: str|evidence\.owner|== owner$|owner=args\.owner",),
    "scripts/tests/test_check_supply_chain.py": (r"\bOWNER\b|\bowner=OWNER",),
    ".github/workflows/supply-chain.yml": (r"\bOWNER\b",),
    # GraphQL answer fields of the repository owner.
    "factory/pr_updates.py": (r"\bowner, name = repo",),
    # The OWNER environment variable of the supply-chain workflow, read by tests and the CHB-204 dry run.
    "scripts/tests/test_workflow_guards.py": (r"\bOWNER:",),
    "docs/plans/CHB-204/dry-run.sh": (r"'OWNER: ",),
    # The acli command name.
    "factory/tests/test_deny_raw_jira_writes.py": (r"change-owner",),
    # A path the review plan must not echo.
    "factory/tests/test_review_plan.py": (r"@owner",),
    # The owners of a CODEOWNERS pattern.
    "scripts/tests/test_codeowners.py": (r"\bowners\b|\bOwners from",),
    # The display name of a live notification channel: renaming it needs a Terraform apply.
    "infra/gcp/monitoring.tf": (r"display_name = \"chb owner email\"",),
}


def problems(path: str, line: str) -> bool:
    """True when the line uses "owner" outside every technical use allowed for its file."""
    patterns = [re.compile(p, re.IGNORECASE) for p in ANYWHERE] + [re.compile(p) for p in IN_FILE.get(path, ())]
    spans = [m.span() for pattern in patterns for m in pattern.finditer(line)]
    return any(not any(start <= m.start() and m.end() <= end for start, end in spans) for m in WORD.finditer(line))


def grep() -> list[tuple[str, int, str]]:
    excluded = [f":(exclude){path}" for path in EXCLUDED]
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(ROOT), "grep", "-I", "-n", "-i", "-w", "-e", "owner", "-e", "owners", "--", *PATHS, *excluded],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode not in (0, 1):
        raise RuntimeError(out.stderr)
    found = []
    for row in out.stdout.splitlines():
        path, number, line = row.split(":", 2)
        found.append((path, int(number), line))
    return found


class RepositoryTest(unittest.TestCase):
    def test_the_repository_calls_the_person_the_human(self) -> None:
        bad = [f"{path}:{number}: {line.strip()}" for path, number, line in grep() if problems(path, line)]

        self.assertEqual(bad, [], 'Write "the human" for the person (CHB-194), or add a technical use with its reason.')

    def test_every_listed_file_exists(self) -> None:
        for path in IN_FILE:
            self.assertTrue((ROOT / path).is_file(), path)

    def test_every_file_entry_still_matches_a_line(self) -> None:
        lines: dict[str, list[str]] = {}
        for path, _, line in grep():
            lines.setdefault(path, []).append(line)
        for path, patterns in IN_FILE.items():
            for pattern in patterns:
                self.assertTrue(any(re.search(pattern, line) for line in lines.get(path, [])), f"{path}: {pattern} matches no line; remove it")


class ProblemsTest(unittest.TestCase):
    def test_refuses_the_person(self) -> None:
        for line in ("Ask the owner.", "the owner's approval", "Owner: react", "owner decision", "both owners"):
            self.assertTrue(problems("docs/x.md", line), line)

    def test_allows_technical_uses(self) -> None:
        for line in ("code-owner review", "no code owner", "the repository owner", "--repo OWNER/NAME", '"owner": x', ".owner.login", "roles/owner", "the word `owner`"):
            self.assertFalse(problems("docs/x.md", line), line)

    def test_a_technical_use_does_not_cover_another_on_the_same_line(self) -> None:
        self.assertTrue(problems("docs/x.md", "the repository owner and the owner"))

    def test_a_file_entry_applies_only_to_its_file(self) -> None:
        self.assertFalse(problems("factory/tests/test_deny_raw_jira_writes.py", "acli jira filter change-owner --id 1"))
        self.assertTrue(problems("docs/x.md", "acli jira filter change-owner --id 1"))


if __name__ == "__main__":
    unittest.main()
