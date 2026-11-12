#!/usr/bin/env python3
"""Refuse to deploy a commit that is not on main (CHB-173). Standard library only.

deploy.yml trusts the branch name that a `workflow_run` event reports. A pushed tag named `main`
reports `main` too, so a commit that never went through a pull request could reach production.
The `gate` job of deploy.yml runs this check before any job gets credentials.

deploy.yml runs this file as it is on main (`git show refs/remotes/origin/main:...`), never from the
commit it deploys, so the commit under test cannot change the rule that judges it.

Usage (from deploy.yml):
  check_deploy_commit.py --repo DIR --sha SHA --main-ref refs/remotes/origin/main
Exit 0: SHA is a commit on main. Exit 1: anything else (not 40 lowercase hex, unknown, not on main).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence

SHA = re.compile(r"[0-9a-f]{40}")


def _git(repo: str, *args: str) -> int:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False).returncode  # noqa: S603,S607


def check(repo: str, sha: str, main_ref: str) -> str | None:
    """None when sha is a commit on main_ref, else the reason it is refused."""
    if not SHA.fullmatch(sha):
        return "the commit SHA is not 40 lowercase hex characters"
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{main_ref}^{{commit}}") != 0:
        return f"{main_ref} does not exist"
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}") != 0:
        return f"commit {sha} is not on {main_ref}: it is unknown here"
    if _git(repo, "merge-base", "--is-ancestor", sha, main_ref) != 0:
        return f"commit {sha} is not on {main_ref}"
    return None


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--repo", "--sha", "--main-ref"):
        parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    reason = check(args.repo, args.sha, args.main_ref)
    if reason:
        print(f"Refused, nothing deploys: {reason} (CHB-173).", file=sys.stderr)
        return 1
    print(f"Commit {args.sha} is on {args.main_ref}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
