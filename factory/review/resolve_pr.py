#!/usr/bin/env python3
"""Pick the PR that review.yml scores (ADR-ea8c decision 2, CHB-93). Standard library only.

The `resolve` job of review.yml fetches each candidate PR with `gh api repos/R/pulls/N` and
passes the files here. The head SHA comes from `workflow_run.head_sha`, which GitHub sets.
A PR qualifies only if it is open, not a draft, into `main` of this repository, from a branch
of this repository, not by Dependabot, and its current head is that SHA.

Several open PRs can share a head SHA. The lowest number wins; the `review` check run attaches
to the SHA, so every PR with that head shows it.

Usage (from .github/workflows/review.yml):
  resolve_pr.py --repo OWNER/NAME --head-sha SHA pr-1.json [pr-2.json ...]
Prints `number=N` and `base-sha=SHA` lines for $GITHUB_OUTPUT, or nothing when no PR qualifies.
Exit code: 0 (found or not), 2 on a malformed SHA.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}")
BASE_BRANCH = "main"
DEPENDABOT = "dependabot[bot]"


def rejection(pr: Mapping[str, object], repo: str, head_sha: str) -> str | None:
    """Why this PR must not be reviewed, or None if it qualifies."""

    def get(*path: str) -> object:
        value: object = pr
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        return value

    checks = [
        (get("state") == "open", "not open"),
        (get("draft") is False, "draft"),
        (get("base", "ref") == BASE_BRANCH, "base is not main"),
        (get("base", "repo", "full_name") == repo, "base is another repository"),
        (get("head", "repo", "full_name") == repo, "head is a fork"),
        (get("head", "sha") == head_sha, "head moved"),
        (get("user", "login") != DEPENDABOT, "Dependabot PR"),
        (isinstance(get("number"), int) and not isinstance(get("number"), bool), "no number"),
        (isinstance(get("base", "sha"), str) and SHA.fullmatch(str(get("base", "sha"))) is not None, "bad base SHA"),
    ]
    return next((reason for ok, reason in checks if not ok), None)


def select(prs: Sequence[Mapping[str, object]], repo: str, head_sha: str) -> Mapping[str, object] | None:
    if not SHA.fullmatch(head_sha):
        raise ValueError(f"head SHA is not 40 hex characters: {head_sha!r}")
    valid = [pr for pr in prs if rejection(pr, repo, head_sha) is None]
    return min(valid, key=lambda pr: int(str(pr["number"])), default=None)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("pr_files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    prs = []
    for path in args.pr_files:
        pr = json.loads(path.read_text())
        reason = rejection(pr, args.repo, args.head_sha)
        print(f"PR {pr.get('number')}: {reason or 'qualifies'}", file=sys.stderr)
        prs.append(pr)
    try:
        chosen = select(prs, args.repo, args.head_sha)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if chosen is not None:
        print(f"number={chosen['number']}")
        print(f"base-sha={chosen['base']['sha']}")  # type: ignore[index]
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
