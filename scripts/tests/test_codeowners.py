"""CODEOWNERS leaves supply-chain files without a code owner (ADR-ebe6, CHB-207).

The `main` ruleset requires code-owner review, and a GitHub App cannot be a code owner.
A supply-chain file with an owner makes every Dependabot PR that changes it wait for the
owner, even when the `supply-chain` check passes. This test reads the real CODEOWNERS
with GitHub's rules, so a later edit cannot bring that wait back unnoticed.

GitHub's rules (docs.github.com, "About code owners", "CODEOWNERS syntax"): patterns
follow gitignore, the last matching pattern wins, and a pattern with no owners leaves
the file without a code owner. `!` and `[ ]` do not work.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_supply_chain import is_supply_chain_file

ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"


UNSUPPORTED = ("\\", "[", "]", "!")


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """A regex for the repository-relative paths a CODEOWNERS pattern matches.

    Supported: a leading `/` (anchored), a trailing `/` (directory), `*`, `?` and `**`.
    A wildcard in the last segment does not reach nested files (GitHub's `docs/*`
    example). Escapes, `!` and `[ ]` raise: GitHub does not support them, and this
    matcher does not guess.
    """
    if any(char in pattern for char in UNSUPPORTED):
        raise ValueError(f"CODEOWNERS pattern this test cannot model: {pattern}")
    directory = pattern.endswith("/")
    body = pattern.strip("/")
    # gitignore: a slash at the start or in the middle anchors the pattern at the root.
    anchored = pattern.startswith("/") or "/" in body
    regex = ""
    i = 0
    while i < len(body):
        if body.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            regex += "/.*"
            i += 3
        elif body.startswith("**", i):
            regex += ".*"
            i += 2
        elif body[i] == "*":
            regex += "[^/]*"
            i += 1
        elif body[i] == "?":
            regex += "[^/]"
            i += 1
        else:
            regex += re.escape(body[i])
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    # A pattern that names a directory covers everything below it. One ending in `/`
    # matches only a directory, so it needs something below it. GitHub's example says
    # `docs/*` does not match nested files, so a wildcard last segment stops there.
    if directory:
        suffix = "/.*"
    elif "*" in body.rsplit("/", 1)[-1]:
        suffix = ""
    else:
        suffix = "(?:/.*)?"
    return re.compile(f"{prefix}{regex}{suffix}")


def parse(text: str) -> list[tuple[re.Pattern[str], list[str]]]:
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        rules.append((_pattern_regex(pattern), owners))
    return rules


def owners_of(path: str, rules: list[tuple[re.Pattern[str], list[str]]]) -> list[str] | None:
    """Owners from the last matching pattern; None when no pattern matches."""
    found = None
    for regex, owners in rules:
        if regex.fullmatch(path):
            found = owners
    return found


class MatcherTest(unittest.TestCase):
    """The examples of GitHub's "About code owners" page."""

    def test_a_later_pattern_with_no_owners_removes_the_owner(self):
        rules = parse("/apps/ @octocat\n/apps/github\n")
        self.assertEqual(owners_of("apps/web/main.js", rules), ["@octocat"])
        self.assertEqual(owners_of("apps/github", rules), [])
        self.assertEqual(owners_of("apps/github/main.js", rules), [])

    def test_double_star_matches_at_any_depth(self):
        rules = parse("**/logs @octocat\n/infra/**/versions.tf\n")
        for path in ["build/logs/a.txt", "deeply/nested/logs/a.txt", "logs/a.txt"]:
            with self.subTest(path=path):
                self.assertEqual(owners_of(path, rules), ["@octocat"])
        for path in ["infra/versions.tf", "infra/gcp/versions.tf", "infra/a/b/versions.tf"]:
            with self.subTest(path=path):
                self.assertEqual(owners_of(path, rules), [])
        self.assertIsNone(owners_of("versions.tf", rules))

    def test_single_star_stays_in_one_directory(self):
        rules = parse("docs/* docs@example.com\n")
        self.assertEqual(owners_of("docs/getting-started.md", rules), ["docs@example.com"])
        self.assertIsNone(owners_of("docs/build-app/troubleshooting.md", rules))

    def test_a_name_without_slash_matches_in_any_directory(self):
        rules = parse(".npmrc @islomar\n")
        self.assertEqual(owners_of("frontend/.npmrc", rules), ["@islomar"])
        self.assertEqual(owners_of(".npmrc", rules), ["@islomar"])
        self.assertIsNone(owners_of("frontend/.npmrc.bak", rules))

    def test_patterns_it_cannot_model_raise(self):
        for pattern in ["\\#file", "!/infra/gcp/state.tf", "/infra/[ab].tf"]:
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                parse(f"{pattern} @islomar\n")

    def test_a_trailing_slash_matches_only_below_the_directory(self):
        rules = parse("/infra/ @islomar\n")
        self.assertEqual(owners_of("infra/gcp/state.tf", rules), ["@islomar"])
        self.assertIsNone(owners_of("infra", rules))
        self.assertIsNone(owners_of("other/infra/x.tf", rules))


class RepositoryCodeownersTest(unittest.TestCase):
    def setUp(self):
        self.rules = parse(CODEOWNERS.read_text())

    def test_terraform_provider_pins_have_no_code_owner(self):
        for path in [
            "infra/gcp/.terraform.lock.hcl",
            "infra/gcp/versions.tf",
            "infra/github/.terraform.lock.hcl",
            "infra/github/versions.tf",
        ]:
            with self.subTest(path=path):
                self.assertTrue(is_supply_chain_file(path))
                self.assertFalse(owners_of(path, self.rules), "the supply-chain check decides")

    def test_versions_tf_holds_only_version_pins(self):
        # versions.tf has no code owner, so configuration in it would skip code-owner
        # review. The backend and the provider live in owned files (CHB-207).
        files = sorted((ROOT / "infra").glob("**/versions.tf"))
        self.assertTrue(files)
        for path in files:
            text = path.read_text()
            for block in ("provider", "backend", "module", "resource", "data", "locals"):
                with self.subTest(path=str(path.relative_to(ROOT)), block=block):
                    self.assertIsNone(re.search(rf"^\s*{block}\b", text, re.MULTILINE))

    def test_other_infrastructure_files_keep_the_owner(self):
        for path in [
            "infra/gcp/state.tf",
            "infra/gcp/backend.tf",
            "infra/gcp/providers.tf",
            "infra/github/ruleset.tf",
            "infra/README.md",
        ]:
            with self.subTest(path=path):
                self.assertEqual(owners_of(path, self.rules), ["@islomar"])

    def test_the_check_itself_and_the_release_age_settings_keep_the_owner(self):
        for path in [
            "scripts/check_supply_chain.py",
            "scripts/rerun_failed_supply_chain.py",
            ".github/workflows/supply-chain.yml",
            "frontend/.npmrc",
            ".github/CODEOWNERS",
        ]:
            with self.subTest(path=path):
                self.assertEqual(owners_of(path, self.rules), ["@islomar"])


if __name__ == "__main__":
    unittest.main()
