#!/usr/bin/env python3
"""Claude Code Stop hook: triage the review findings of agent PRs before the turn ends (CHB-209).

On 2026-11-19 PRs #139 and #140 sat blocked after the owner's approval: the required `triage` check
stayed red because the agent never triaged the review. On #140 the agent then told the owner the PR
waited for the owner's approval.

The hook lists the open PRs of the agent's account (`triage_review.AGENT`) and reads the comments of
each. It runs the same function as the required check, `triage_review.evaluate`, so both agree on what
"untriaged" means: the latest review comment of the reviewer App has findings and no triage comment of
the agent's account carries the marker of the reviewed sha. For each such PR it names the PR and the
commands, and exits 2 with the list on stderr.

It fails open: a GitHub error (gh missing, a timeout, a failed call, output that is not JSON) or anything
unexpected prints one line to stderr and exits 0. A network failure must not trap the session.

It never loops. Like check_wip_minimums.py it remembers the findings it refused for, per session, in
`<temp dir>/chb-untriaged-reviews-<uid>/<session id>.json` (CHB_UNTRIAGED_REVIEWS_STATE_DIR overrides the
folder). A second stop with the same findings passes with one line; a new review refuses again.

Reads use plain `gh` (the owner's login, read only) with the repository path, never the current folder.

Contract (https://code.claude.com/docs/en/hooks, Stop): the event arrives as JSON on stdin with
`session_id`. Exit code 2 blocks the stop and feeds stderr to the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from typing import TextIO

HOOKS = os.path.dirname(os.path.abspath(__file__))
FACTORY = os.path.dirname(HOOKS)
sys.path.insert(0, FACTORY)  # python3 -I does not add it
sys.path.insert(0, HOOKS)

import triage_review  # noqa: E402
from check_wip_minimums import already_refused  # noqa: E402

STATE_DIR_ENV = "CHB_UNTRIAGED_REVIEWS_STATE_DIR"
CALL_TIMEOUT_SECONDS = 6
PULLS_PATH = "repos/{repo}/pulls?state=open&per_page=100"


def gh(args: list[str]) -> str:
    """`gh <args>` with a short timeout. Raises on failure, like triage_review.Gh."""
    return subprocess.run(  # noqa: S603
        ["gh", *args], stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True, timeout=CALL_TIMEOUT_SECONDS
    ).stdout


def _login(item: Mapping[str, object]) -> object:
    user = item.get("user")
    return user.get("login") if isinstance(user, Mapping) else None


def agent_pulls(pulls: Sequence[Mapping[str, object]]) -> list[int]:
    """The numbers of the open PRs of the agent's account, lowest first."""
    return sorted(
        int(str(p["number"]))
        for p in pulls
        if p.get("state") == "open" and _login(p) == triage_review.AGENT and str(p.get("number", "")).isdigit()
    )


def read_pulls(run: triage_review.Gh, repo: str) -> list[dict]:
    out = run(["api", "--paginate", PULLS_PATH.format(repo=repo), "--jq", ".[]"])
    return [item for line in out.splitlines() if line.strip() and isinstance(item := json.loads(line), dict)]


def findings(run: triage_review.Gh, repo: str = triage_review.REPOSITORY) -> list[str]:
    """One line per open agent PR whose latest review has untriaged findings. Raises on a GitHub error."""
    lines: list[str] = []
    for number in agent_pulls(read_pulls(run, repo)):
        verdict = triage_review.evaluate(triage_review.read_comments(run, repo, str(number)))
        if not verdict.ok:
            lines.append(f"PR #{number}: {verdict.message.replace('<PR>', str(number))}")
    return lines


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV) or posixpath.join(tempfile.gettempdir(), f"chb-untriaged-reviews-{os.getuid()}")


def _reason(error: BaseException) -> str:
    detail = getattr(error, "stderr", None) if isinstance(error, subprocess.CalledProcessError) else None
    text = f"{type(error).__name__}: {detail or error}" if isinstance(error, (subprocess.SubprocessError, OSError, ValueError)) else type(error).__name__
    return " ".join(text.split())[:300]


def main(stdin: TextIO = sys.stdin, gh: triage_review.Gh | None = None, state_dir: str | None = None) -> int:
    try:
        event = json.load(stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    session_id = event.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    try:
        lines = findings(gh or globals()["gh"])
    except BaseException as error:  # noqa: BLE001 - fail open on anything
        print(f"Untriaged review check skipped: {_reason(error)}", file=sys.stderr)
        return 0
    fingerprint = hashlib.sha256("\n".join(lines).encode()).hexdigest() if lines else None
    try:
        repeated = already_refused(state_dir if state_dir is not None else globals()["state_dir"](), session_id, fingerprint)
    except OSError as error:
        print(f"Untriaged review check skipped: state folder unusable ({type(error).__name__})", file=sys.stderr)
        return 0
    if not lines:
        return 0
    if repeated:
        print("Untriaged reviews: same findings as the last refusal, allowing the stop.", file=sys.stderr)
        return 0
    print(
        "Agent PRs have untriaged review findings, and their required `triage` check stays red until you triage them "
        "(CHB-209, factory/README.md, Triage of review findings). The PR does not wait for the owner:\n"
        + "\n".join(f"- {line}" for line in lines),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
