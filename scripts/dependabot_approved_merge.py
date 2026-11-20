#!/usr/bin/env python3
"""Find the needs-human Dependabot PR whose head the human approved (CHB-213). Stdlib only.

dependabot-auto-merge.yml labels majors, Playwright updates and unknown update types
`dependencies:needs-human` and leaves auto-merge off. The human's approval is the decision;
the merge click adds nothing. dependabot-approved-merge.yml runs this script after the human
submits a review, and turns auto-merge (squash) on for the PR it prints.

A PR qualifies when all of these hold (decide):
- it is open, not a draft, from this repository, into main;
- its author is `dependabot[bot]` and every commit is Dependabot's, signed by GitHub
  (check_supply_chain.is_dependabot_commit);
- it carries the label `dependencies:needs-human`;
- its current head is the head the review run saw;
- the human's latest deciding review approves that head (check_supply_chain.owner_approved_head:
  comments do not count, a dismissal or a change request cancels the approval).
The `main` ruleset must also require every check in REQUIRED_CHECKS, or auto-merge would
merge before CI.

Usage: dependabot_approved_merge.py --repo OWNER/NAME --head-sha SHA --human LOGIN
Environment: GH_TOKEN, a token that can read pull requests and rulesets.
Prints `number=N` for a qualifying PR. Exit codes: 0 done (with or without a PR), 2 usage or API error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

# The workflow runs this file with `python3 -I`, which leaves the script's folder off sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_supply_chain  # noqa: E402
from check_supply_chain import DEPENDABOT, is_dependabot_commit, owner_approved_head  # noqa: E402

LABEL = "dependencies:needs-human"
BASE = "main"
# Same list as the ruleset step of dependabot-auto-merge.yml (a test compares them).
REQUIRED_CHECKS = ("backend", "frontend", "e2e", "supply-chain")


@dataclass(frozen=True)
class Decision:
    enable: bool
    reason: str


def _login(user: dict | None) -> str | None:
    return user.get("login") if user else None


def decide(pr: dict, commits: list[dict], reviews: list[dict], *, head_sha: str, human: str, repo: str) -> Decision:
    number = pr.get("number")
    problems = []
    if pr.get("state") != "open" or pr.get("draft"):
        problems.append("not an open, ready PR")
    if (pr.get("head", {}).get("repo") or {}).get("full_name") != repo:
        problems.append("the head branch is not in this repository")
    if pr.get("base", {}).get("ref") != BASE:
        problems.append(f"the base is not {BASE}")
    if _login(pr.get("user")) != DEPENDABOT:
        problems.append(f"the author is {_login(pr.get('user'))}, not {DEPENDABOT}")
    if not commits or len(commits) != pr.get("commits") or not all(map(is_dependabot_commit, commits)):
        problems.append("not every commit is Dependabot's")
    if LABEL not in {label.get("name") for label in pr.get("labels", [])}:
        problems.append(f"no `{LABEL}` label")
    if pr.get("head", {}).get("sha") != head_sha:
        problems.append(f"the head moved from {head_sha[:12]}")
    elif not owner_approved_head(reviews, human, head_sha):
        problems.append(f"{human}'s latest deciding review does not approve the head")
    if problems:
        return Decision(False, f"PR #{number}: " + "; ".join(problems) + ".")
    return Decision(True, f"PR #{number}: {human} approved head {head_sha[:12]} of a needs-human Dependabot update.")


def missing_checks(rules: list[dict]) -> list[str]:
    required = {
        check.get("context")
        for rule in rules
        if rule.get("type") == "required_status_checks"
        for check in rule.get("parameters", {}).get("required_status_checks", [])
    }
    return [check for check in REQUIRED_CHECKS if check not in required]


def github_get(path: str, token: str) -> list | dict:
    return check_supply_chain.github_get(path, token)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--human", required=True)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("::error::GH_TOKEN is not set")
        return 2
    if not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
        print("::error::--head-sha is not a commit SHA")
        return 2
    repo = f"/repos/{args.repo}"
    try:
        pulls = github_get(f"{repo}/commits/{args.head_sha}/pulls", token)
        for number in sorted({p["number"] for p in pulls if isinstance(p.get("number"), int)}):
            pr = github_get(f"{repo}/pulls/{number}", token)
            commits = github_get(f"{repo}/pulls/{number}/commits?per_page=100", token)
            reviews = github_get(f"{repo}/pulls/{number}/reviews?per_page=100", token)
            decision = decide(pr, list(commits), list(reviews), head_sha=args.head_sha, human=args.human, repo=args.repo)
            print(decision.reason)
            if not decision.enable:
                continue
            missing = missing_checks(list(github_get(f"{repo}/rules/branches/{BASE}", token)))
            if missing:
                print(f"::warning::{BASE} does not require {', '.join(missing)}. Auto-merge stays off.")
                return 0
            print(f"number={number}")
            return 0
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"::error::cannot read the PR of {args.head_sha}: {error}")
        return 2
    print(f"::notice::No needs-human Dependabot PR with head {args.head_sha[:12]} approved by {args.human}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
