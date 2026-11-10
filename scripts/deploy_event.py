#!/usr/bin/env python3
"""Record one deployment event per production deploy (CHB-74, epic CHB-72).

deploy.yml calls this script after the deploy and smoke jobs. The script builds one event:
commit SHA, PR number, first commit time, merge time, deploy start and end, deploy and smoke
results, and who authored the change (agent, human, dependabot). It writes the event twice:
1. as one Cloud Logging entry (log `chb-deploy-events`, `gcloud logging write`), and
2. as a GitHub deployment status on environment `production` (`gh api`).
A failed write never fails the deploy: the script always exits 0 and prints one
`::warning::` line that lists what failed.

Input is environment variables: GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID, COMMIT_SHA,
DEPLOY_START, DEPLOY_END, DEPLOY_RESULT, SMOKE_RESULT, GCP_PROJECT. `gh` reads GH_TOKEN.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

LOG_NAME = "chb-deploy-events"
ENVIRONMENT = "production"
AGENT_LOGIN = "chb-factory-agent[bot]"
DEPENDABOT_LOGIN = "dependabot[bot]"
Runner = Callable[..., str]


def run_command(args: Sequence[str], stdin: str | None = None) -> str:
    """Run a command and return stdout. Raises CalledProcessError on a non-zero exit."""
    return subprocess.run(  # noqa: S603
        list(args), input=stdin, text=True, capture_output=True, check=True, timeout=60
    ).stdout


def author_type(login: str | None) -> str:
    if not login:
        return "unknown"
    if login == AGENT_LOGIN:
        return "agent"
    if login == DEPENDABOT_LOGIN:
        return "dependabot"
    return "human"


def build_event(env: Mapping[str, str], pr: Mapping[str, Any] | None, commits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The event. `pr` is the pull request API object (or None), `commits` its commits."""
    author_dates = [c["commit"]["author"]["date"] for c in commits]
    server, repo, run_id = env["GITHUB_SERVER_URL"], env["GITHUB_REPOSITORY"], env["GITHUB_RUN_ID"]
    return {
        "event": "deployment",
        "schema": 1,
        "environment": ENVIRONMENT,
        "commit_sha": env["COMMIT_SHA"],
        "pr_number": pr["number"] if pr else None,
        "first_commit_time": min(author_dates) if author_dates else None,
        "merge_time": pr.get("merged_at") if pr else None,
        "deploy_start": env.get("DEPLOY_START") or None,
        "deploy_end": env.get("DEPLOY_END") or None,
        "deploy_result": env.get("DEPLOY_RESULT") or "skipped",
        "smoke_result": env.get("SMOKE_RESULT") or "skipped",
        "author_type": author_type(((pr or {}).get("user") or {}).get("login")),
        "run_url": f"{server}/{repo}/actions/runs/{run_id}",
    }


def find_pull_request(repo: str, sha: str, run: Runner) -> dict[str, Any] | None:
    pulls = json.loads(run(["gh", "api", f"repos/{repo}/commits/{sha}/pulls"]))
    merged = [p for p in pulls if p.get("merge_commit_sha") == sha]
    chosen = merged or pulls
    return chosen[0] if chosen else None


def pull_request_commits(repo: str, number: int, run: Runner) -> list[dict[str, Any]]:
    pages = json.loads(run(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/commits"]))
    return [commit for page in pages for commit in page]


def write_log_entry(event: Mapping[str, Any], project: str, run: Runner) -> None:
    run(
        ["gcloud", "logging", "write", LOG_NAME, json.dumps(event), f"--project={project}", "--payload-type=json", "--severity=INFO"]
    )


def write_deployment_status(event: Mapping[str, Any], repo: str, run: Runner) -> None:
    """A new deployment of kind `deploy-event` on `production`, with one status.

    `auto_inactive` is false so the status does not mark the deployment that Actions created
    for the deploy job as inactive.
    """
    ok = event["deploy_result"] == "success" and event["smoke_result"] == "success"
    pr = f"PR #{event['pr_number']}" if event["pr_number"] else "no PR"
    description = f"{event['commit_sha'][:7]} {pr} by {event['author_type']}, deploy {event['deploy_result']}, smoke {event['smoke_result']}"[:140]
    created = json.loads(
        run(
            ["gh", "api", "-X", "POST", f"repos/{repo}/deployments", "--input", "-"],
            stdin=json.dumps(
                {
                    "ref": event["commit_sha"],
                    "environment": ENVIRONMENT,
                    "task": "deploy-event",
                    "auto_merge": False,
                    "required_contexts": [],
                    "production_environment": True,
                    "transient_environment": False,
                    "description": description,
                }
            ),
        )
    )
    run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/deployments/{created['id']}/statuses", "--input", "-"],
        stdin=json.dumps(
            {
                "state": "success" if ok else "failure",
                "environment": ENVIRONMENT,
                "description": description,
                "log_url": event["run_url"],
                "auto_inactive": False,
            }
        ),
    )


def describe(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        detail = (error.stderr or "").strip().splitlines()[-1:] or [""]
        return f"{error.cmd[0]} exited {error.returncode} {detail[0]}".strip()
    return f"{type(error).__name__}: {error}"


def main(env: Mapping[str, str], run: Runner = run_command) -> int:
    """Always returns 0. Prints one warning line when any step failed."""
    failures: list[str] = []
    event: dict[str, Any] | None = None
    try:
        repo, sha = env["GITHUB_REPOSITORY"], env["COMMIT_SHA"]
        pr: dict[str, Any] | None = None
        commits: list[dict[str, Any]] = []
        try:
            pr = find_pull_request(repo, sha, run)
            if pr:
                commits = pull_request_commits(repo, pr["number"], run)
        except Exception as error:  # noqa: BLE001 - nothing here may fail the deploy
            failures.append(f"pull request lookup: {describe(error)}")
        event = build_event(env, pr, commits)
    except Exception as error:  # noqa: BLE001
        failures.append(f"event: {describe(error)}")
    if event is not None:
        for name, write in (
            ("Cloud Logging", lambda e: write_log_entry(e, env["GCP_PROJECT"], run)),
            ("GitHub deployment status", lambda e: write_deployment_status(e, env["GITHUB_REPOSITORY"], run)),
        ):
            try:
                write(event)
            except Exception as error:  # noqa: BLE001
                failures.append(f"{name}: {describe(error)}")
    if failures:
        print("::warning::Deploy event not fully recorded (CHB-74), the deploy is unaffected. " + "; ".join(failures).replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ))
