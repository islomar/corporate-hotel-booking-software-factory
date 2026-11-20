"""CODEOWNERS leaves supply-chain files without a code owner (ADR-ebe6, CHB-207).

The `main` ruleset requires code-owner review, and a GitHub App cannot be a code owner.
A supply-chain file with a code owner makes every Dependabot PR that changes it wait for the
human, even when the `supply-chain` check passes. This test reads the real CODEOWNERS
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
# Pattern syntax GitHub does not support; the matcher raises instead of guessing.
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


def non_pin_lines(text: str) -> list[str]:
    """Lines of a versions.tf that are not version pins. An empty list means only pins.

    Allowed: one top-level `terraform` block holding `required_version` and a
    `required_providers` block. Anything else (a `cloud` or `backend` block,
    `provider_meta`, a resource, an import) is reported. Text after `#` or `//` is
    dropped as a comment.

    A line-based heuristic, not an HCL parser: it expects one statement per line, as
    `terraform fmt` writes. It fails closed on what it cannot read line by line: a
    `/* */` comment, more than one brace on a line, or a `#` or `//` inside a quoted
    string (an odd number of quotes before it). It does not inspect the contents of
    `required_providers`: Terraform accepts only provider requirement maps there.
    The `supply-chain` check still asks the human to approve any change to
    versions.tf that Dependabot did not make.
    """
    found = []
    depth = 0
    terraform_blocks = 0
    for raw in text.splitlines():
        line = re.sub(r"(#|//).*", "", raw).strip()
        in_string = line.count('"') % 2 == 1
        if "/*" in raw or in_string or line.count("{") + line.count("}") > 1:
            found.append(raw.strip())
        if not line:
            continue
        if depth == 0:
            if re.fullmatch(r"terraform\s*\{", line):
                terraform_blocks += 1
            else:
                found.append(line)
        elif depth == 1 and line != "}":
            if not re.match(r"(required_version\s*=|required_providers\s*\{)", line):
                found.append(line)
        depth += line.count("{") - line.count("}")
    if terraform_blocks > 1:
        found.append("more than one terraform block")
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


class VersionPinsTest(unittest.TestCase):
    PINS = (
        "# comment\n"
        "terraform {\n"
        '  required_version = "~> 1.17.0"\n'
        "\n"
        "  required_providers {\n"
        "    google = {\n"
        '      source  = "hashicorp/google"\n'
        '      version = "~> 8.11.0"  # pinned\n'
        "    }\n"
        "  }\n"
        "}\n"
    )

    def test_only_version_pins_pass(self):
        self.assertEqual(non_pin_lines(self.PINS), [])

    def test_anything_else_is_reported(self):
        inside = '  backend "gcs" {\n    bucket = "b"\n  }\n'
        for name, text in {
            "backend": self.PINS.replace("\n  required_providers", f"\n{inside}  required_providers"),
            "cloud": self.PINS.replace("\n  required_providers", "\n  cloud {\n  }\n  required_providers"),
            "provider_meta": self.PINS.replace(
                "\n  required_providers", '\n  provider_meta "google" {\n  }\n  required_providers'
            ),
            "provider": self.PINS + 'provider "google" {\n  project = "p"\n}\n',
            "import": self.PINS + "import {\n  to = a.b\n  id = \"x\"\n}\n",
            "variable": self.PINS + 'variable "x" {}\n',
            "second terraform block": self.PINS + 'terraform {\n  required_version = "1"\n}\n',
            "block comment": self.PINS + "/* note */\n",
            "two blocks on one line": self.PINS.replace(
                "  required_providers {\n", "  required_providers { } \n  cloud {\n"
            ),
            "one-line block": self.PINS + 'variable "x" { }\n',
            "comment marker in a string": self.PINS.replace('"~> 1.17.0"', '"~> 1.17.0 # x"'),
        }.items():
            with self.subTest(name):
                self.assertNotEqual(non_pin_lines(text), [])


class RepositoryCodeownersTest(unittest.TestCase):
    def setUp(self):
        self.rules = parse(CODEOWNERS.read_text())

    def test_terraform_provider_pins_have_no_code_owner(self):
        # infra/github/versions.tf does not exist yet. It is listed so a future file
        # there gets the same treatment.
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
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(non_pin_lines(path.read_text()), [])

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
