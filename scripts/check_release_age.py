#!/usr/bin/env python3
"""Fail when a lockfile pins a release younger than the project's release-age window.

`npm ci` and `uv sync --locked` install what the lockfile says. They do not re-check
`min-release-age` (.npmrc) or `exclude-newer` (pyproject.toml). This script does.

- uv.lock: offline. Every file's `upload-time` must be older than the window recorded in
  `[options] exclude-newer-span`.
- package-lock.json: entries that are new compared with `--base` are checked against the
  npm registry publish time. Window and exemptions come from the `.npmrc` next to it.

Exemptions (for a fresh security fix) use the package managers' own settings:
`exclude-newer-package = { <name> = false }` in pyproject.toml and
`min-release-age-exclude[]=<name>` in .npmrc.

npm security fixes (CHB-95): Dependabot ignores `min-release-age` for security updates, so a
fix can be younger than the window. A too-new npm entry passes when it is the package of an
open Dependabot alert on the same manifest, at exactly the alert's first patched version
(Dependabot moves to the minimum fix). Only the alerted package itself: a new transitive entry
still fails. Only for pull requests opened by Dependabot (PR_AUTHOR=dependabot[bot]). The
alerts come from the GitHub API (GH_TOKEN with Dependabot alerts read, GITHUB_REPOSITORY),
read only when an entry is too new. Without them there is no exemption, never an error.

Usage: check_release_age.py [--base GIT_REF] LOCKFILE...
Exit codes: 0 pass, 1 violations, 2 configuration or network error.
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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

REGISTRY = "https://registry.npmjs.org/"
GITHUB_API = "https://api.github.com"
DEPENDABOT = "dependabot[bot]"
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
MAX_ALERT_PAGES = 20
RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Violation:
    name: str
    version: str
    published: datetime

    def describe(self, now: datetime) -> str:
        age = now - self.published
        return f"{self.name}@{self.version} published {self.published:%Y-%m-%d %H:%M} UTC ({age.days} days ago)"


def parse_iso_days(span: str) -> timedelta:
    """Parse the ISO 8601 spans uv writes to uv.lock, such as P7D or PT24H."""
    match = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", span)
    if not match or span in ("P", "PT"):
        raise ConfigError(f"unsupported exclude-newer-span: {span!r}")
    weeks, days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# uv.lock


def uv_exempt_packages(pyproject: Path) -> set[str]:
    if not pyproject.exists():
        return set()
    uv = tomllib.loads(pyproject.read_text()).get("tool", {}).get("uv", {})
    return {normalize_python_name(name) for name in uv.get("exclude-newer-package", {})}


def normalize_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_uv_lock(lock_text: str, exempt: set[str], now: datetime) -> list[Violation]:
    lock = tomllib.loads(lock_text)
    span = lock.get("options", {}).get("exclude-newer-span")
    if not span:
        raise ConfigError(
            "uv.lock has no exclude-newer-span: set `exclude-newer` in pyproject.toml"
        )
    cutoff = now - parse_iso_days(span)
    violations: list[Violation] = []
    for package in lock.get("package", []):
        if normalize_python_name(package["name"]) in exempt:
            continue
        files = [package.get("sdist", {}), *package.get("wheels", [])]
        uploads = [parse_timestamp(f["upload-time"]) for f in files if "upload-time" in f]
        if uploads and max(uploads) > cutoff:
            violations.append(Violation(package["name"], package.get("version", "?"), max(uploads)))
    return violations


# package-lock.json


def read_npmrc(npmrc: Path) -> tuple[timedelta, list[str]]:
    if not npmrc.exists():
        raise ConfigError(f"{npmrc} is missing: it must set min-release-age")
    days: int | None = None
    exclude: list[str] = []
    for raw in npmrc.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "min-release-age":
            if not value.isdigit():
                raise ConfigError(
                    f"{npmrc}: min-release-age must be a number of days, got {value!r}"
                )
            days = int(value)
        elif key in ("min-release-age-exclude", "min-release-age-exclude[]"):
            exclude.append(value)
    if days is None:
        raise ConfigError(f"{npmrc} does not set min-release-age")
    return timedelta(days=days), exclude


def npm_registry_entries(lock_text: str) -> set[tuple[str, str]]:
    """(name, version) pairs that npm downloads from the public registry."""
    entries: set[tuple[str, str]] = set()
    for path, meta in json.loads(lock_text).get("packages", {}).items():
        resolved = meta.get("resolved", "")
        if "node_modules/" not in path or not resolved.startswith(REGISTRY):
            continue
        name = meta.get("name") or path.rsplit("node_modules/", 1)[1]
        entries.add((name, meta["version"]))
    return entries


def fetch_publish_time(name: str, version: str) -> datetime:
    url = REGISTRY + urllib.parse.quote(name, safe="@")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (fixed https host)
        times = json.load(response).get("time", {})
    if version not in times:
        raise ConfigError(f"registry has no publish time for {name}@{version}")
    return parse_timestamp(times[version])


def check_npm_entries(
    entries: set[tuple[str, str]],
    window: timedelta,
    exclude: list[str],
    now: datetime,
    publish_time=fetch_publish_time,
) -> list[Violation]:
    cutoff = now - window
    violations: list[Violation] = []
    for name, version in sorted(entries):
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude):
            continue
        published = publish_time(name, version)
        if published > cutoff:
            violations.append(Violation(name, version, published))
    return violations


# Security fixes for open Dependabot alerts


def _release(version: str) -> tuple[int, int, int] | None:
    """A plain release version as numbers. Pre-releases and other forms are None."""
    match = RELEASE.fullmatch(version.removeprefix("v"))
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def alert_fixes(alerts: list[dict], manifest_dir: str) -> dict[str, list[tuple[str, int]]]:
    """npm package name -> [(first patched version, alert number)] for one manifest folder."""
    fixes: dict[str, list[tuple[str, int]]] = {}
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        dependency = alert.get("dependency") or {}
        package = dependency.get("package") or {}
        patched = (alert.get("security_vulnerability") or {}).get("first_patched_version") or {}
        folder = PurePosixPath(dependency.get("manifest_path", "")).parent.as_posix()
        if package.get("ecosystem") != "npm" or folder != manifest_dir or not patched.get("identifier"):
            continue
        fixes.setdefault(package["name"], []).append((patched["identifier"], alert.get("number", 0)))
    return fixes


def security_fix_exemptions(
    violations: list[Violation], fixes: dict[str, list[tuple[str, int]]]
) -> tuple[list[Violation], list[str]]:
    """Split too-new entries into the ones that still fail and notes for the exempted fixes."""
    kept: list[Violation] = []
    notes: list[str] = []
    for violation in violations:
        version = _release(violation.version)
        match = None
        for patched, number in fixes.get(violation.name, []):
            floor = _release(patched)
            # Exactly the first patched version: Dependabot security updates move to the
            # minimum fixed version, and anything newer is a fresh release like any other.
            if version and floor and version == floor:
                match = number
                break
        if match is None:
            kept.append(violation)
        else:
            notes.append(f"{violation.name}@{violation.version} is a security fix: Dependabot alert #{match}")
    return kept, notes


def fetch_open_alerts(repository: str, token: str) -> list[dict]:
    """Open npm alerts, following the Link header (the endpoint uses cursor pagination)."""
    alerts: list[dict] = []
    url: str | None = f"{GITHUB_API}/repos/{repository}/dependabot/alerts?state=open&ecosystem=npm&per_page=100"
    for _ in range(MAX_ALERT_PAGES):
        if url is None:
            return alerts
        if not url.startswith(GITHUB_API + "/"):
            raise ValueError(f"unexpected next page URL: {url}")
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
        request = urllib.request.Request(url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (checked https host)
            batch = json.load(response)
            link = response.headers.get("Link", "")
        if not isinstance(batch, list):
            raise ValueError("the alerts API did not answer with a list")
        alerts.extend(batch)
        found = NEXT_LINK.search(link)
        url = found.group(1) if found else None
    raise ValueError(f"more than {MAX_ALERT_PAGES} pages of open alerts")


def repository_folder(path: Path) -> str:
    """The lockfile's folder relative to the repository root, as the alerts API writes it."""
    result = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--show-prefix"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    prefix = result.stdout.strip() if result.returncode == 0 else ""
    return PurePosixPath(prefix, path.parent.as_posix()).as_posix().rstrip("/") or "."


def load_fixes(path: Path, fetch=None) -> dict[str, list[tuple[str, int]]]:
    fetch = fetch or fetch_open_alerts
    if os.environ.get("PR_AUTHOR") != DEPENDABOT:
        return {}
    repository, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not repository or not token:
        print(f"{path}: no GH_TOKEN or GITHUB_REPOSITORY, so no security-fix exemption")
        return {}
    try:
        return alert_fixes(fetch(repository, token), repository_folder(path))
    except (OSError, ValueError) as error:
        print(f"::warning file={path}::cannot read Dependabot alerts ({error}), so no security-fix exemption")
        return {}


def git_show(ref: str, path: Path) -> str | None:
    # `ref:./path` resolves the path from the current directory, not the repository root.
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{ref}:./{path.as_posix()}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


# main


def check_lockfile(path: Path, base: str | None, now: datetime) -> list[Violation]:
    text = path.read_text()
    if path.name == "uv.lock":
        return check_uv_lock(text, uv_exempt_packages(path.parent / "pyproject.toml"), now)
    if path.name == "package-lock.json":
        window, exclude = read_npmrc(path.parent / ".npmrc")
        entries = npm_registry_entries(text)
        base_text = git_show(base, path) if base else None
        if base_text is not None:
            entries -= npm_registry_entries(base_text)
        print(f"{path}: checking {len(entries)} new registry entries")
        violations = check_npm_entries(entries, window, exclude, now, publish_time=fetch_publish_time)
        if violations:
            violations, notes = security_fix_exemptions(violations, load_fixes(path))
            for note in notes:
                print(f"::notice file={path}::{note}")
        return violations
    raise ConfigError(f"unsupported lockfile: {path}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="git ref to diff npm lockfiles against")
    parser.add_argument("lockfiles", nargs="+", type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    failed = False
    for lockfile in args.lockfiles:
        try:
            violations = check_lockfile(lockfile, args.base, now)
        except (ConfigError, OSError, ValueError) as error:
            print(f"::error file={lockfile}::{error}")
            return 2
        for violation in violations:
            print(f"::error file={lockfile}::too new: {violation.describe(now)}")
        failed = failed or bool(violations)
        if not violations:
            print(f"{lockfile}: release age OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
