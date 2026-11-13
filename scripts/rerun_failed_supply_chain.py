#!/usr/bin/env python3
"""Re-run the failed `supply-chain` runs of a PR head after a run passes (CHB-184). Stdlib only.

The `supply-chain` check fails on `pull_request` when a PR changes a supply-chain file and the
owner has not approved. The owner's approval starts a `pull_request_review` run that passes, but
the earlier failed run stays on the head commit and keeps the required check red. The agent's
GitHub App cannot re-run workflows, so the passing run does it with its own token.

A re-run evaluates the live reviews again (check_supply_chain.py reads them from the API), so it
passes only while the owner's latest deciding review approves the head. A PR without that approval
never reaches this script: its run fails first.

Usage: rerun_failed_supply_chain.py --repo OWNER/NAME --head SHA --run-id ID
Environment: GH_TOKEN, a token with `actions: write`.
Exit codes: 0 done (a failed re-run request only warns), 2 configuration or API error.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request

import check_supply_chain
from check_supply_chain import API

WORKFLOW_FILE = "supply-chain.yml"
EVENTS = ("pull_request", "pull_request_review")


def github_get(path: str, token: str) -> list | dict:
    return check_supply_chain.github_get(path, token)


def github_post(path: str, token: str) -> None:
    request = urllib.request.Request(  # noqa: S310 - fixed https host
        f"{API}{path}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30):  # noqa: S310
        pass


def failed_runs_to_rerun(runs: list[dict], head_sha: str, current_run_id: int) -> list[int]:
    return [
        r["id"]
        for r in runs
        if r.get("head_sha") == head_sha
        and r.get("event") in EVENTS
        and r.get("conclusion") == "failure"
        and r["id"] != current_run_id
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("::error::GH_TOKEN is not set")
        return 2
    path = f"/repos/{args.repo}/actions/workflows/{WORKFLOW_FILE}/runs?head_sha={args.head}&per_page=100"
    try:
        body = github_get(path, token)
        runs = body["workflow_runs"] if isinstance(body, dict) else []
    except (OSError, KeyError) as error:
        print(f"::error::cannot list the runs of {args.head}: {error}")
        return 2
    for run_id in failed_runs_to_rerun(runs, args.head, args.run_id):
        try:
            github_post(f"/repos/{args.repo}/actions/runs/{run_id}/rerun-failed-jobs", token)
            print(f"Re-ran failed run {run_id}.")
        except OSError as error:
            print(f"::warning::could not re-run run {run_id}: {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
