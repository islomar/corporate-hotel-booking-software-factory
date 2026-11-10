#!/usr/bin/env python3
"""The docs allowlist: which changed paths count as documentation. Standard library only.

One source of truth for two users (CHB-117):
- factory/review/plan.py skips the model review of a docs-only PR (ADR-ea8c, CHB-105).
- .github/workflows/ci.yml skips the backend, frontend and e2e jobs of a docs-only PR.

CI uses a narrower rule than the reviewer, never a wider one: a change under docs/sessions/
runs every job, because the backend job's tests check that folder for unredacted exports (CHB-122).

ci.yml runs this file as it is on the base commit, so a PR cannot change the rule that judges it.
This file imports nothing from the repository: CI runs it alone, copied out of the checkout.

Usage (from ci.yml):
  docs_list.py --repo DIR --base SHA --head SHA --event EVENT
Prints `docs_only=true` or `docs_only=false` for $GITHUB_OUTPUT. Exit code: always 0.
Anything unexpected (an event other than pull_request, a malformed SHA, a failed git diff,
no changed files) prints `docs_only=false`, so every job runs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import PurePosixPath

SHA = re.compile(r"[0-9a-f]{40}")

# Never docs, at any stage: these paths steer the agents, the factory or the supply chain.
GOVERNANCE_PREFIXES = (
    ".github/",
    ".claude/",
    "factory/",
    "infra/",
    "scripts/",
    "docs/decisions/",
    "docs/process/",
    "docs/knowledge/",
)
GOVERNANCE_NAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "Makefile"}
MANIFEST_NAMES = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile",
    "Pipfile.lock",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
    ".npmrc",
    ".nvmrc",
    ".python-version",
    "Dockerfile",
    "compose.yaml",
}
MANIFEST_PATTERN = re.compile(r"(^requirements.*\.txt$|\.lock$|\.lockb$|-lock\.(json|yaml)$)")
DOC_EXTENSIONS = {".md"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# Docs for the reviewer, but CI runs every job when they change (see the module docstring).
CI_ALWAYS_TESTED_PREFIXES = ("docs/sessions/",)


def is_governance(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith(GOVERNANCE_PREFIXES)
        or name in GOVERNANCE_NAMES
        or name in MANIFEST_NAMES
        or MANIFEST_PATTERN.search(name) is not None
    )


def is_doc(path: str) -> bool:
    """Allowlist: Markdown and images under docs/, and README.md files. Governance never counts."""
    pure = PurePosixPath(path)
    if not path or path.startswith("/") or is_governance(path):
        return False
    if any(part.startswith(".") or part == "" for part in path.split("/")):
        return False
    suffix = pure.suffix.lower()
    if path.startswith("docs/"):
        return suffix in DOC_EXTENSIONS | IMAGE_EXTENSIONS
    return pure.name == "README.md"


def docs_only(paths: Sequence[str]) -> bool:
    return bool(paths) and all(is_doc(p) for p in paths)


def ci_docs_only(paths: Sequence[str]) -> bool:
    """True when CI may skip the backend, frontend and e2e jobs."""
    return docs_only(paths) and not any(p.startswith(CI_ALWAYS_TESTED_PREFIXES) for p in paths)


def changed_paths(repo: str, base: str, head: str) -> list[str] | None:
    """Paths changed from the merge base of base and head to head, or None if git fails."""
    command = ["git", "-C", repo, "diff", "-z", "--no-renames", "--name-only", f"{base}...{head}"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--repo", "--base", "--head", "--event"):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    skip = False
    if args.event != "pull_request":
        print(f"Event {args.event}: every job runs.", file=sys.stderr)
    elif not (SHA.fullmatch(args.base) and SHA.fullmatch(args.head)):
        print("Base or head SHA is not 40 hex characters: every job runs.", file=sys.stderr)
    else:
        paths = changed_paths(args.repo, args.base, args.head)
        if paths is None:
            print("git diff failed: every job runs.", file=sys.stderr)
        else:
            skip = ci_docs_only(paths)
            print(f"{len(paths)} changed files, docs only for CI: {skip}.", file=sys.stderr)
    print(f"docs_only={'true' if skip else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
