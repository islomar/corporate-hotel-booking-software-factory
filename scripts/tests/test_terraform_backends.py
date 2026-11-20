"""Each Terraform folder keeps its state in the GCS state bucket (CHB-216).

Without a backend block Terraform writes the state to a local `terraform.tfstate`, on the
machine that ran it. `infra/github` did that until CHB-216: the state lived only on the
owner's laptop. This test reads the `.tf` files of each folder and fails when the folder
has no `backend "gcs"` block with the bucket and prefix that infra/README.md names, or when
the block sits outside `backend.tf` (`versions.tf` has no code owner, CHB-207).

A line-based reader, not an HCL parser: it expects the layout `terraform fmt` writes.
"""

import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUCKET = "chb-factory-a92932-tfstate"
# Folder under infra/ -> prefix of its state in the bucket.
EXPECTED_PREFIXES = {"gcp": "gcp", "github": "github"}
BACKEND_START = re.compile(r'^\s*backend\s+"(?P<kind>[^"]+)"\s*\{')
ATTRIBUTE = re.compile(r'^\s*(?P<name>\w+)\s*=\s*"(?P<value>[^"]*)"')
# A quoted string, kept as it is, or a `#` or `//` comment outside quotes, dropped.
STRING_OR_COMMENT = re.compile(r'("[^"]*")|(?:#|//).*')


def without_comment(line: str) -> str:
    return STRING_OR_COMMENT.sub(lambda match: match[1] or "", line)


def backend_blocks(folder: Path) -> list[tuple[str, str, dict[str, str]]]:
    """Every backend block in the folder's `.tf` files: (file name, kind, string attributes)."""
    found = []
    for path in sorted(folder.glob("*.tf")):
        lines = iter(path.read_text(encoding="utf-8").splitlines())
        for line in lines:
            start = BACKEND_START.match(without_comment(line))
            if not start:
                continue
            attributes: dict[str, str] = {}
            for inner in lines:
                code = without_comment(inner)
                # The first lone `}` ends the block: nested blocks are not supported. A gcs
                # backend has none.
                if code.strip() == "}":
                    break
                attribute = ATTRIBUTE.match(code)
                if attribute:
                    attributes[attribute["name"]] = attribute["value"]
            found.append((path.name, start["kind"], attributes))
    return found


class BackendReaderTest(unittest.TestCase):
    def scratch_folder(self) -> Path:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        return Path(scratch.name)

    def test_reads_kind_and_attributes_and_ignores_comments(self):
        folder = self.scratch_folder()
        (folder / "backend.tf").write_text(
            '# backend "local" {}\nterraform {\n  backend "gcs" {\n'
            '    bucket = "b" # comment\n    prefix = "p"\n  }\n}\n',
            encoding="utf-8",
        )

        self.assertEqual(backend_blocks(folder), [("backend.tf", "gcs", {"bucket": "b", "prefix": "p"})])

    def test_keeps_a_hash_or_double_slash_inside_a_quoted_value(self):
        folder = self.scratch_folder()
        (folder / "backend.tf").write_text(
            'terraform {\n  backend "http" {\n    address = "https://x.example/state#a" # comment\n  }\n}\n',
            encoding="utf-8",
        )

        self.assertEqual(backend_blocks(folder), [("backend.tf", "http", {"address": "https://x.example/state#a"})])

    def test_a_folder_without_a_backend_has_none(self):
        folder = self.scratch_folder()
        (folder / "main.tf").write_text('resource "x" "y" {\n  name = "z"\n}\n', encoding="utf-8")

        self.assertEqual(backend_blocks(folder), [])


class RepositoryBackendsTest(unittest.TestCase):
    def test_every_folder_keeps_its_state_in_the_bucket_under_its_own_prefix(self):
        for name, prefix in EXPECTED_PREFIXES.items():
            with self.subTest(folder=f"infra/{name}"):
                self.assertEqual(
                    backend_blocks(ROOT / "infra" / name),
                    [("backend.tf", "gcs", {"bucket": BUCKET, "prefix": prefix})],
                )


if __name__ == "__main__":
    unittest.main()
