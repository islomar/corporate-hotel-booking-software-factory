#!/usr/bin/env python3
"""Decide what the review job does for one PR head (ADR-ea8c, CHB-105). Standard library only.

Three actions, decided from paths, diff size and earlier check runs of the reviewer App.
PR title, body and labels never count.

- skip: every changed file is on the docs allowlist. The job posts a `neutral` check, no Claude call.
- carry: the files changed since the last reviewed commit are all docs. The job posts that
  verdict again on the new head, no Claude call.
- review: Claude reviews the PR. Opus for high-risk paths or a large diff, Sonnet otherwise.

Usage (from .github/workflows/review.yml, before the PR head loses its .git directory):
  plan.py --head-dir DIR --base-sha SHA --head-sha SHA --prior-runs runs.json --out check-run.json
Prints `action=` and `model=` lines for $GITHUB_OUTPUT. For skip and carry it writes the check
run payload to --out. Exit code: 0, or 2 on a malformed SHA.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gate import CARRIED, CHECK_NAME, REVIEWED, SKIPPED, _plain, parse_name_list, risk_class  # noqa: E402

REVIEWER_APP_SLUG = "chb-factory-reviewer"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
# Added plus deleted lines from `git diff --numstat`. A binary file outside docs/ counts as large.
LARGE_DIFF_LINES = 400
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


def changed_lines(numstat: str) -> int | None:
    """Sum of added and deleted lines from `git diff -z --numstat`.

    None when a binary file outside the docs allowlist changed. Images under docs/ count as 0.
    """
    total = 0
    for record in (r for r in numstat.split("\0") if r):
        added, deleted, path, *_ = record.split("\t") + ["", "", ""]
        if not (added.isdigit() and deleted.isdigit()):
            if is_doc(path):
                continue
            return None
        total += int(added) + int(deleted)
    return total


def choose_model(paths: Sequence[str], lines: int | None) -> str:
    high_risk = risk_class(paths) in ("harness", "supply-chain") or any(is_governance(p) for p in paths)
    if high_risk or lines is None or lines > LARGE_DIFF_LINES:
        return OPUS
    return SONNET


# --- earlier verdicts -------------------------------------------------------------------


@dataclass(frozen=True)
class PriorVerdict:
    reviewed_sha: str
    conclusion: str
    title: str
    summary: str


def _get(run: object, *path: str) -> object:
    for key in path:
        run = run.get(key) if isinstance(run, Mapping) else None
    return run


def reviewed_sha(run: Mapping[str, object]) -> str | None:
    """The commit a valid model review came from (`reviewed:<sha>`).

    Errors (`error:`), skips, carries and runs from before CHB-105 have none, so they are never carried.
    """
    external_id = run.get("external_id")
    if not (isinstance(external_id, str) and external_id.startswith(REVIEWED)):
        return None
    sha = external_id[len(REVIEWED) :]
    return sha if SHA.fullmatch(sha) else None


def last_verdict(runs: Iterable[object], app_slug: str = REVIEWER_APP_SLUG) -> PriorVerdict | None:
    """The newest completed review by the reviewer App with a success or failure verdict."""
    candidates = []
    for run in runs:
        if not isinstance(run, Mapping) or run.get("name") != CHECK_NAME or _get(run, "app", "slug") != app_slug:
            continue
        if run.get("status") != "completed" or run.get("conclusion") not in ("success", "failure"):
            continue
        sha = reviewed_sha(run)
        if sha is None:
            continue
        candidates.append((str(run.get("completed_at") or ""), sha, run))
    if not candidates:
        return None
    _, sha, run = max(candidates, key=lambda c: c[0])
    return PriorVerdict(
        sha,
        str(run["conclusion"]),
        str(_get(run, "output", "title") or ""),
        str(_get(run, "output", "summary") or ""),
    )


# --- decision ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    action: str  # skip, carry or review
    model: str | None = None
    payload: dict[str, object] | None = None


def _file_list(paths: Sequence[str], limit: int = 50) -> str:
    lines = [f"- {_plain(p, 300)}" for p in paths[:limit]]
    if len(paths) > limit:
        lines.append(f"- and {len(paths) - limit} more")
    return "\n".join(lines)


def decide(
    head_sha: str,
    paths: Sequence[str],
    lines: int | None,
    prior: PriorVerdict | None,
    delta: Sequence[str] | None,
) -> Plan:
    """paths: changed since the merge base. delta: changed since prior.reviewed_sha, None if unknown."""
    if docs_only(paths):
        summary = "**Skipped**: every changed file is documentation. No model review ran.\n\n" + _file_list(paths)
        return Plan(
            "skip",
            payload={
                "name": CHECK_NAME,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": "neutral",
                "external_id": SKIPPED,
                "output": {"title": "Skipped: docs-only change", "summary": summary},
            },
        )
    if prior is not None and delta is not None and docs_only(delta):
        note = (
            f"**Carried forward** from `{prior.reviewed_sha[:12]}`: the files changed since that review"
            " are all documentation. No model review ran.\n\n" + _file_list(delta) + "\n\nVerdict of that review:\n\n"
        )
        return Plan(
            "carry",
            payload={
                "name": CHECK_NAME,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": prior.conclusion,
                "external_id": CARRIED + prior.reviewed_sha,
                "output": {
                    "title": ("Carried: " + prior.title)[:200],
                    "summary": (note + prior.summary)[:60000],
                },
            },
        )
    return Plan("review", model=choose_model(paths, lines))


# --- command line -----------------------------------------------------------------------


def _git(head_dir: str, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", head_dir, *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return result.stdout if result.returncode == 0 else None


def delta_since(head_dir: str, reviewed: str, head_sha: str) -> list[str] | None:
    """Files changed from an earlier reviewed commit to the head, or None if that commit is not an ancestor."""
    if _git(head_dir, "merge-base", "--is-ancestor", reviewed, head_sha) is None:
        return None
    names = _git(head_dir, "diff", "-z", "--no-renames", "--name-only", reviewed, head_sha)
    return None if names is None else parse_name_list(names)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--head-dir", "--base-sha", "--head-sha", "--prior-runs", "--out"):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    if not (SHA.fullmatch(args.base_sha) and SHA.fullmatch(args.head_sha)):
        print("base or head SHA is not 40 hex characters", file=sys.stderr)
        return 2

    span = f"{args.base_sha}...{args.head_sha}"
    names = _git(args.head_dir, "diff", "-z", "--no-renames", "--name-only", span)
    numstat = _git(args.head_dir, "diff", "-z", "--no-renames", "--numstat", span)
    if names is None or numstat is None:
        print("git diff failed: full review with Opus", file=sys.stderr)
        print(f"action=review\nmodel={OPUS}")
        return 0
    paths = parse_name_list(names)

    try:
        runs = json.loads(Path(args.prior_runs).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        runs = []
    prior = last_verdict(runs if isinstance(runs, list) else [])
    delta = None
    if prior is not None and prior.reviewed_sha != args.head_sha:
        delta = delta_since(args.head_dir, prior.reviewed_sha, args.head_sha)

    plan = decide(args.head_sha, paths, changed_lines(numstat), prior, delta)
    if plan.payload is not None:
        Path(args.out).write_text(json.dumps(plan.payload), encoding="utf-8")
    print(f"action={plan.action}")
    if plan.model is not None:
        print(f"model={plan.model}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
