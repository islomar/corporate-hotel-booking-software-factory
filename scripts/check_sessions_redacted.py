#!/usr/bin/env python3
"""Fail when a file in docs/sessions/ holds something a redaction should have removed.

docs/sessions/ holds Claude Code session exports, and this repository is public. The owner
redacts each export by hand before adding it (CHB-122). This check stops a missed email
address, home or /mnt path (also dash-encoded, as in Claude Code scratchpad paths),
1Password reference or token-like string before the commit. It also requires the two
comments that switch Vale and rumdl off in each export, because exports are verbatim.
It prints the file, line and pattern name, never the matched text, so a secret does not
reach a CI log. A false positive is fixed by redacting or shortening the text.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS = ROOT / "docs/sessions"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    ("home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")),
    ("dash-encoded home path", re.compile(r"(?<![A-Za-z0-9])-(?:home|Users)-[A-Za-z0-9._]+-")),
    ("claude /tmp path", re.compile(r"/tmp/claude-\d+/")),
    ("/mnt path", re.compile(r"/mnt/")),
    ("1Password reference", re.compile(r"op://")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{16,}")),
    ("sk- key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("1Password token", re.compile(r"\bops_[A-Za-z0-9+/=_-]{16,}")),
    ("long hex string", re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])")),
)
# Base64 candidates must mix lower case, upper case and digits, so a long path of plain
# words separated by slashes does not count.
BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}")
HEX_ONLY = re.compile(r"[0-9a-fA-F/+]+=*")


def is_base64(candidate: str) -> bool:
    return (
        any(c.islower() for c in candidate)
        and any(c.isupper() for c in candidate)
        and any(c.isdigit() for c in candidate)
        and not HEX_ONLY.fullmatch(candidate)
    )


def findings(text: str) -> list[tuple[int, str]]:
    """(line number, pattern name) for every pattern that matches a line of text."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS:
            if pattern.search(line):
                found.append((number, name))
        if any(is_base64(m[0]) for m in BASE64.finditer(line)):
            found.append((number, "long base64 string"))
    return found


DISABLE_COMMENTS = ("<!-- vale off -->", "<!-- rumdl-disable -->")


def missing_disable_comments(text: str) -> list[str]:
    """The lint-off comments that no line of text holds."""
    lines = {line.strip() for line in text.splitlines()}
    return [comment for comment in DISABLE_COMMENTS if comment not in lines]


def is_export(name: str) -> bool:
    """True for a Markdown file that is not the folder README."""
    return name.endswith(".md") and name != "README.md"


def is_cache(path: Path, sessions: Path) -> bool:
    """True inside a folder marked with CACHEDIR.TAG, such as the .rumdl_cache/ of the Markdown linter."""
    return any((folder / "CACHEDIR.TAG").is_file() for folder in path.parents if folder.is_relative_to(sessions))


def files(sessions: Path) -> list[Path]:
    """Every file under sessions, except inside cache folders."""
    return sorted(p for p in sessions.rglob("*") if p.is_file() and not is_cache(p, sessions))


def main(sessions: Path = SESSIONS) -> int:
    errors = []
    for path in files(sessions):
        name = path.relative_to(sessions).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, pattern in findings(text):
            errors.append(
                f"::error file=docs/sessions/{name},line={line}::{pattern} found. "
                "Redact it before committing (CHB-122)."
            )
        if is_export(name) and (missing := missing_disable_comments(text)):
            errors.append(
                f"::error file=docs/sessions/{name}::export lacks {' and '.join(missing)}. "
                "Add these lines after the table of contents: " + " ".join(DISABLE_COMMENTS)
            )
    for error in errors:
        print(error)
    if not errors:
        print("docs/sessions has no unredacted detail and every export has the lint-off comments")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
