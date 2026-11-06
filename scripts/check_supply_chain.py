#!/usr/bin/env python3
"""Fail when a PR changes supply-chain files without Dependabot or the owner behind it.

Supply-chain files (manifests, lockfiles, images, runtime pins) have no code owner, so
Dependabot PRs can merge unattended. This check takes the code owner's place for them
(ADR-ebe6). A PR passes when:

- it changes no supply-chain file; or
- the owner's latest review is an approval of the current head commit; or
- the PR author is `dependabot[bot]` and every commit is Dependabot's: author
  `dependabot[bot]`, committer `web-flow` and a signature GitHub verified. GitHub records
  Dependabot commits this way. A release-age change (`min-release-age*` in `.npmrc`,
  `exclude-newer*` in `pyproject.toml`) rules this path out.

Changed files come from `git diff --no-renames --name-only BASE...HEAD`. Commits and
reviews come from the GitHub API. PR title, body and labels are never read.

Usage: check_supply_chain.py --repo OWNER/NAME --pr N --base SHA --head SHA --owner LOGIN
Environment: GH_TOKEN, a token that can read the pull request.
Exit codes: 0 pass, 1 fail, 2 configuration, git or API error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import PurePosixPath

API = "https://api.github.com"
DEPENDABOT = "dependabot[bot]"
# GitHub commits Dependabot's changes and signs them with its web-flow key.
GITHUB_COMMITTER = "web-flow"

SUPPLY_CHAIN_NAMES = (
    "pyproject.toml",
    "uv.lock",
    "requirements*.txt",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    "Dockerfile",
    "Dockerfile.*",
    "*.dockerfile",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    ".nvmrc",
    ".node-version",
    ".python-version",
)


class CheckError(Exception):
    pass


def is_supply_chain_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SUPPLY_CHAIN_NAMES)


# Release-age settings


def _npmrc_settings(text: str) -> list[tuple[str, str]]:
    settings = []
    for raw in text.splitlines():
        line = raw.strip()
        if "=" in line and not line.startswith(("#", ";")):
            key, value = (part.strip() for part in line.split("=", 1))
            if key.startswith("min-release-age"):
                settings.append((key, value))
    return sorted(settings)


def _pyproject_settings(text: str) -> dict[str, object]:
    uv = tomllib.loads(text).get("tool", {}).get("uv", {})
    return {key: value for key, value in uv.items() if key.startswith("exclude-newer")}


def release_age_changed(path: str, base_text: str | None, head_text: str | None) -> bool:
    """True when the release-age settings in an .npmrc or pyproject.toml differ.

    None means the file does not exist on that side. A file that does not parse counts
    as a change: the check fails closed.
    """
    name = PurePosixPath(path).name
    if name == ".npmrc":
        if base_text is None or head_text is None:
            return True
        return _npmrc_settings(base_text) != _npmrc_settings(head_text)
    if name == "pyproject.toml":
        try:
            base = _pyproject_settings(base_text) if base_text is not None else {}
            head = _pyproject_settings(head_text) if head_text is not None else {}
        except tomllib.TOMLDecodeError:
            return True
        return base != head
    return False


# Commits and reviews


def _login(user: dict | None) -> str | None:
    return user.get("login") if user else None


def is_dependabot_commit(commit: dict) -> bool:
    verification = commit.get("commit", {}).get("verification") or {}
    return (
        _login(commit.get("author")) == DEPENDABOT
        and _login(commit.get("committer")) == GITHUB_COMMITTER
        and verification.get("verified") is True
    )


def owner_approved_head(reviews: list[dict], owner: str, head_sha: str) -> bool:
    """The owner's latest deciding review approves the head commit.

    Comments do not change a decision. A dismissal or a change request cancels it.
    """
    decisions = [
        r
        for r in reviews
        if _login(r.get("user")) == owner
        and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
    ]
    if not decisions:
        return False
    latest = decisions[-1]
    return latest["state"] == "APPROVED" and latest.get("commit_id") == head_sha


# Decision


@dataclass(frozen=True)
class Evidence:
    changed_files: list[str]
    release_age_files: list[str]
    pr_author: str | None
    pr_head_sha: str
    pr_commit_count: int
    commits: list[dict]
    reviews: list[dict]
    owner: str
    head_sha: str


@dataclass(frozen=True)
class Verdict:
    ok: bool
    message: str


def _dependabot_problems(evidence: Evidence) -> list[str]:
    problems = []
    if evidence.pr_author != DEPENDABOT:
        problems.append(f"the PR author is {evidence.pr_author}, not {DEPENDABOT}")
    if evidence.pr_head_sha != evidence.head_sha:
        problems.append(f"the PR head moved to {evidence.pr_head_sha}; a newer run decides")
    if not evidence.commits or len(evidence.commits) != evidence.pr_commit_count:
        problems.append(
            f"the API listed {len(evidence.commits)} of {evidence.pr_commit_count} commits"
        )
    for commit in evidence.commits:
        if not is_dependabot_commit(commit):
            verification = commit.get("commit", {}).get("verification") or {}
            problems.append(
                f"commit {commit.get('sha', '?')[:12]}: author {_login(commit.get('author'))}, "
                f"committer {_login(commit.get('committer'))}, "
                f"verified signature {verification.get('verified', False)}"
            )
    return problems


def decide(evidence: Evidence) -> Verdict:
    supply_chain = [f for f in evidence.changed_files if is_supply_chain_file(f)]
    if not supply_chain:
        return Verdict(True, "No supply-chain file changed.")
    listing = "Supply-chain files changed:\n" + "\n".join(f"- {f}" for f in supply_chain)
    if owner_approved_head(evidence.reviews, evidence.owner, evidence.head_sha):
        return Verdict(True, f"{listing}\nThe owner {evidence.owner} approved the head commit.")
    ask = f"Needs an approval from {evidence.owner} on the head commit."
    if evidence.release_age_files:
        files = ", ".join(evidence.release_age_files)
        return Verdict(False, f"{listing}\nRelease-age settings changed in {files}. {ask}")
    problems = _dependabot_problems(evidence)
    if problems:
        reasons = "\n".join(f"- {p}" for p in problems)
        return Verdict(False, f"{listing}\nNot a Dependabot-only PR:\n{reasons}\n{ask}")
    return Verdict(True, f"{listing}\nEvery commit is Dependabot's, signed by GitHub.")


# Git and GitHub


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603,S607


def changed_files(base: str, head: str) -> list[str]:
    result = _git("diff", "--no-renames", "--name-only", f"{base}...{head}")
    if result.returncode != 0:
        raise CheckError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def file_at(ref: str, path: str) -> str | None:
    result = _git("show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def merge_base(base: str, head: str) -> str:
    result = _git("merge-base", base, head)
    if result.returncode != 0:
        raise CheckError(f"git merge-base failed: {result.stderr.strip()}")
    return result.stdout.strip()


def github_get(path: str, token: str) -> list | dict:
    """GET an API path. Lists are followed through every page."""
    url: str | None = f"{API}{path}"
    pages: list = []
    while url:
        request = urllib.request.Request(  # noqa: S310 - fixed https host
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.load(response)
            link = response.headers.get("Link", "")
        if not isinstance(body, list):
            return body
        pages.extend(body)
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None
    return pages


def gather(args: argparse.Namespace, token: str) -> Evidence:
    files = changed_files(args.base, args.head)
    fork_point = merge_base(args.base, args.head)
    release_age_files = [
        f for f in files if release_age_changed(f, file_at(fork_point, f), file_at(args.head, f))
    ]
    pr_path = f"/repos/{args.repo}/pulls/{args.pr}"
    pr = github_get(pr_path, token)
    if not isinstance(pr, dict):
        raise CheckError("unexpected pull request response")
    commits = github_get(f"{pr_path}/commits?per_page=100", token)
    reviews = github_get(f"{pr_path}/reviews?per_page=100", token)
    return Evidence(
        changed_files=files,
        release_age_files=release_age_files,
        pr_author=_login(pr.get("user")),
        pr_head_sha=pr["head"]["sha"],
        pr_commit_count=int(pr["commits"]),
        # `pulls/{n}/commits` lists at most 250 commits. decide() compares the count.
        commits=list(commits),
        reviews=list(reviews),
        owner=args.owner,
        head_sha=args.head,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--owner", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("::error::GH_TOKEN is not set")
        return 2
    try:
        evidence = gather(args, token)
    except (CheckError, OSError, ValueError, KeyError) as error:
        print(f"::error::supply-chain check could not run: {error}")
        return 2
    verdict = decide(evidence)
    print(verdict.message)
    if not verdict.ok:
        print("::error::supply-chain: " + verdict.message.splitlines()[-1])
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
