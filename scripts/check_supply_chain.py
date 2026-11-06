#!/usr/bin/env python3
"""Fail when a PR changes supply-chain files without Dependabot or the owner behind it.

Supply-chain files (manifests, lockfiles, resolver settings, images, runtime pins) have
no code owner, so Dependabot PRs can merge unattended. This check takes the code owner's
place for them (ADR-ebe6). A PR passes when:

- it changes no supply-chain file; or
- the owner's latest review is an approval of the current head commit; or
- the PR author is `dependabot[bot]`, every commit is Dependabot's (author
  `dependabot[bot]`, committer `web-flow`, a signature GitHub verified: GitHub records
  Dependabot commits this way), and the update is safe: patch, or minor from 1.0, read
  from the commits' `updated-dependencies` trailer (see update_risks). A release-age
  change (`min-release-age*` in `.npmrc`, `exclude-newer*` in `uv.toml` or
  `pyproject.toml`, any change to `pnpm-workspace.yaml`, `.yarnrc.yml` or
  `bunfig.toml`) rules this path out. The `dependabot-branches` ruleset lets only
  Dependabot push to `dependabot/**` branches.

Changed files come from `git diff --no-renames --name-only -z BASE...HEAD`. Commits and
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
    # Python: manifests, lockfiles and resolver settings (uv.toml overrides [tool.uv]).
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    "requirements*.txt",
    "constraints*.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "pip.conf",
    "pip.ini",
    # JavaScript: manifests, lockfiles and resolver settings.
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    ".pnpmfile.cjs",
    "bun.lock",
    "bun.lockb",
    "bunfig.toml",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    # Runtime pins.
    ".nvmrc",
    ".node-version",
    ".python-version",
)

# Image build and compose files, matched in any case: Docker and Podman accept any name
# with -f, and macOS and Windows file systems ignore case. `containerfile*` is wider than
# the Dockerfile patterns on purpose: Podman variants have no fixed suffix convention.
ANY_CASE_NAMES = (
    "dockerfile",
    "dockerfile.*",
    "*.dockerfile",
    "containerfile*",
    "*.containerfile",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


# Files that can hold a release-age setting the script does not parse: pnpm
# `minimumReleaseAge*`, Yarn `npmMinimalAgeGate`/`npmPreapprovedPackages`, Bun
# `install.minimumReleaseAge*`. Any change to them counts as a release-age change.
OPAQUE_RELEASE_AGE_NAMES = ("pnpm-workspace.yaml", ".yarnrc.yml", "bunfig.toml")


class CheckError(Exception):
    pass


class SettingsError(Exception):
    pass


def is_supply_chain_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SUPPLY_CHAIN_NAMES) or any(
        fnmatch.fnmatchcase(name.lower(), pattern) for pattern in ANY_CASE_NAMES
    )


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


def _exclude_newer(table: object) -> dict[str, object]:
    if not isinstance(table, dict):
        raise SettingsError("expected a TOML table")
    return {key: value for key, value in table.items() if key.startswith("exclude-newer")}


def _uv_settings(name: str, text: str | None) -> dict[str, object]:
    """exclude-newer* keys: top level in uv.toml, under [tool.uv] in pyproject.toml."""
    if text is None:
        return {}
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"not valid TOML: {error}") from error
    if name == "uv.toml":
        return _exclude_newer(document)
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        raise SettingsError("`tool` is not a table")
    return _exclude_newer(tool.get("uv", {}))


def release_age_change(path: str, base_text: str | None, head_text: str | None) -> str | None:
    """Why the release-age settings of a file differ between base and head, or None.

    None as text means the file does not exist on that side. A file that does not parse,
    or has an unexpected shape, counts as a change: the check fails closed. pnpm, Yarn
    and Bun settings are not parsed: any change to those files counts.
    """
    name = PurePosixPath(path).name
    if name == ".npmrc":
        if base_text is None or head_text is None:
            return "file added or deleted"
        if _npmrc_settings(base_text) != _npmrc_settings(head_text):
            return "min-release-age settings differ"
        return None
    if name in ("pyproject.toml", "uv.toml"):
        try:
            base = _uv_settings(name, base_text)
            head = _uv_settings(name, head_text)
        except SettingsError as error:
            return f"cannot read the settings ({error})"
        return "exclude-newer settings differ" if base != head else None
    if name in OPAQUE_RELEASE_AGE_NAMES:
        return "file changed (not parsed: any change counts)" if base_text != head_text else None
    return None


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


# Update type, from the `updated-dependencies` trailer Dependabot writes in its commits


def parse_updated_dependencies(message: str) -> list[dict[str, str]] | None:
    """The entries of the YAML trailer between `---` and `...`, or None if it is malformed.

    Dependabot writes a fixed subset of YAML: `- key: value` starts an entry, `  key: value`
    continues it. Anything else is rejected rather than guessed.
    """
    lines = message.splitlines()
    try:
        start = lines.index("updated-dependencies:")
    except ValueError:
        return None
    if start == 0 or lines[start - 1] != "---":
        return None
    entries: list[dict[str, str]] = []
    for line in lines[start + 1 :]:
        if line == "...":
            ok = entries and all("dependency-name" in e for e in entries)
            return entries if ok else None
        if line.startswith("- "):
            entries.append({})
            body = line[2:]
        elif line.startswith("  ") and entries:
            body = line[2:]
        else:
            return None
        key, sep, value = body.partition(": ")
        if not sep or not key or key.startswith(" "):
            return None
        entries[-1][key] = value.strip().strip("\"'")
    return None


def update_risks(dependencies: list[dict[str, str]] | None, ecosystem: str) -> list[str]:
    """Reasons an update needs a human. An empty list means safe.

    Mirrors the `decide` step of .github/workflows/dependabot-auto-merge.yml: safe are
    patch updates, minor updates from 1.0, and GitHub Actions bumps without an update type.
    A minor update keeps the major version, so the new version tells a 0.x minor apart.
    """
    if not dependencies:
        return ["no dependency metadata"]
    reasons = []
    for dependency in dependencies:
        name = dependency.get("dependency-name", "?")
        version = dependency.get("dependency-version", "")
        update_type = dependency.get("update-type", "")
        major = re.sub(r"^[^0-9]*", "", version).split(".")[0]
        if "playwright" in name:
            reasons.append(f"{name}: the Playwright image in deploy.yml must move in the same PR")
        elif update_type == "version-update:semver-major":
            reasons.append(f"{name}: major update to {version}")
        elif update_type == "version-update:semver-minor" and major == "0":
            reasons.append(f"{name}: 0.x minor update to {version}")
        elif update_type in ("version-update:semver-minor", "version-update:semver-patch") or (
            not update_type and ecosystem == "github_actions"
        ):
            continue
        else:
            reasons.append(f"{name}: unknown update type {update_type or 'none'}")
    return reasons


def dependabot_ecosystem(head_ref: str) -> str:
    """`dependabot/<ecosystem>/...`. Only Dependabot can create these branches."""
    parts = head_ref.split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "dependabot" else ""


# Decision


@dataclass(frozen=True)
class Evidence:
    changed_files: list[str]
    release_age_files: list[str]
    pr_author: str | None
    pr_head_ref: str
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


def pr_update_risks(evidence: Evidence) -> list[str]:
    if not evidence.commits:
        return ["no commits"]
    trailers = [
        parse_updated_dependencies(c.get("commit", {}).get("message", "")) for c in evidence.commits
    ]
    if any(t != trailers[0] for t in trailers):
        return ["the commits disagree on `updated-dependencies`"]
    return update_risks(trailers[0], dependabot_ecosystem(evidence.pr_head_ref))


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
    risks = pr_update_risks(evidence)
    if risks:
        reasons = "\n".join(f"- {r}" for r in risks)
        return Verdict(False, f"{listing}\nDependabot update that needs a human:\n{reasons}\n{ask}")
    return Verdict(True, f"{listing}\nEvery commit is Dependabot's, signed by GitHub; safe update.")


# Git and GitHub


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)  # noqa: S603,S607


def changed_files(base: str, head: str) -> list[str]:
    # -z: paths are NUL-separated and never C-quoted. Without it, git quotes non-ASCII
    # paths ("d\303\251p/package.json") and the file-name match misses them.
    result = _git("diff", "--no-renames", "--name-only", "-z", f"{base}...{head}")
    if result.returncode != 0:
        raise CheckError(f"git diff failed: {result.stderr.strip()}")
    return [path for path in result.stdout.split("\0") if path]


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
    release_age_files = []
    for path in files:
        reason = release_age_change(path, file_at(fork_point, path), file_at(args.head, path))
        if reason:
            print(f"{path}: release-age settings: {reason}")
            release_age_files.append(path)
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
        pr_head_ref=pr["head"]["ref"],
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
