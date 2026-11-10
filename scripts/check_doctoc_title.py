#!/usr/bin/env python3
"""Fail when a doctoc table of contents uses a title other than doctoc's default.

One title everywhere (CHB-142): the line `npx doctoc` writes without `--title`. A file with
another title, such as `## Contents`, keeps it when doctoc reruns, and an editor hook that
forces a title rewrites it in unrelated diffs. The title is the line two below the START marker.
Only tracked Markdown files are read, so git-ignored paths and `.claude/worktrees/` are skipped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TITLE = "**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*"
START_MARKER = "<!-- START doctoc"
EXCLUDED_PREFIXES = (".claude/worktrees/",)


def findings(text: str) -> list[tuple[int, str]]:
    """(line number, title) for every doctoc block whose title is not the default."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        if not line.startswith(START_MARKER):
            continue
        title_index = index + 2
        title = lines[title_index] if title_index < len(lines) else ""
        if title != DEFAULT_TITLE:
            found.append((title_index + 1, title))
    return found


def markdown_files(listed: list[str]) -> list[str]:
    """The Markdown files of a git listing, without the excluded folders."""
    return [name for name in listed if name.endswith(".md") and not name.startswith(EXCLUDED_PREFIXES)]


def tracked_files(root: Path) -> list[str]:
    listed = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "-z"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return [name for name in listed.stdout.split("\0") if name]


def main(root: Path = ROOT, listed: list[str] | None = None) -> int:
    names = markdown_files(tracked_files(root) if listed is None else listed)
    errors = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        for line, title in findings(path.read_text(encoding="utf-8", errors="replace")):
            errors.append(
                f"::error file={name},line={line}::doctoc title {title!r} is not the default. "
                f"Fix: doctoc --title '{DEFAULT_TITLE}' {name} (CHB-142)."
            )
    for error in errors:
        print(error)
    if not errors:
        print("every doctoc table of contents uses the default title")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
