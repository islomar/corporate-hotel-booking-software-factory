#!/usr/bin/env python3
"""Keep agent and Dependabot pull requests up to date with main without the owner (CHB-172, CHB-177). Standard library only.

Why: when one agent PR merges, the others fall behind main. The owner's "Update branch" click makes
the owner the last pusher, and the `main` ruleset requires approval of the last push by someone
else, so the owner's own approval stops counting (PR #102, 2026-11-10). A push by the agent App
leaves the owner free to approve.

The `main` ruleset requires a branch to contain the latest main before it merges
(`strict_required_status_checks_policy = true`, CHB-177), so a PR behind main cannot merge.

What: for each open PR opened by the agent App, into main, from this repository:
- behind main, mergeable: rebase it with the GraphQL mutation `updatePullRequestBranch`
  (`expectedHeadOid` = the head this run read, so a push in between makes GitHub refuse).
  An approved PR is rebased too: the rebase dismisses the approval (`dismiss_stale_reviews_on_push`)
  and the owner approves the new head. Leaving it would stop it from ever merging;
- conflicting: add the label `needs-rebase` (GitHub runs no `pull_request` workflow on a
  conflicting PR, so its checks page proves nothing). The agent resolves it in a session;
- labelled `needs-rebase` but mergeable again: remove the label.
For each open PR opened by Dependabot, into main, from this repository:
- behind main, mergeable: comment `@dependabot rebase`, unless the agent already did so after the
  head commit. The agent never pushes to a Dependabot branch (the `dependabot-branches` ruleset
  stops it); Dependabot does the rebase. A conflicting Dependabot PR rebases itself, so it is left.
Any other PR is never touched.

The workflow (.github/workflows/pr-updates.yml) runs this on every push to main with a token of the
agent App from the `agent-pr-updates` environment (main only, ADR-d7b7). `gh` reads GH_TOKEN.

Usage: pr_updates.py --repo OWNER/NAME
Exit code: 0 (also when a head moved or nothing needed doing), 1 when a GitHub call failed, 2 on bad input.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

AGENT_LOGIN = "chb-factory-agent"  # the GitHub App; GraphQL gives its login without "[bot]"
DEPENDABOT_LOGIN = "dependabot"
REBASE_COMMENT = "@dependabot rebase"
LABEL = "needs-rebase"
BASE_BRANCH = "main"
READS = 3  # reads while some PR has mergeable UNKNOWN (GitHub computes it lazily)
RETRY_SECONDS = 5.0
REPO = re.compile(r"[\w.-]+/[\w.-]+")

QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100) {
      nodes {
        id number headRefOid baseRefName isCrossRepository mergeable reviewDecision
        author { login __typename }
        labels(first: 50) { nodes { name } }
        comments(last: 10) { nodes { body createdAt author { login } } }
        commits(last: 1) { nodes { commit { committedDate } } }
      }
    }
  }
}
"""
UPDATE = """
mutation($id: ID!, $oid: GitObjectID!) {
  updatePullRequestBranch(input: {pullRequestId: $id, expectedHeadOid: $oid, updateMethod: REBASE}) {
    pullRequest { number }
  }
}
"""

Gh = Callable[[list[str]], Any]


class GhError(Exception):
    """A `gh` call failed. The message is its stderr."""


@dataclass(frozen=True)
class Pr:
    number: int
    node_id: str
    head_oid: str
    author: str | None  # the login, only when the author is a GitHub App (Bot); None otherwise
    base: str
    cross_repository: bool
    mergeable: str  # MERGEABLE, CONFLICTING or UNKNOWN
    review_decision: str | None
    behind_by: int
    labels: frozenset[str]
    rebase_requested: bool = False  # the agent commented REBASE_COMMENT after the head commit


def decide(pr: Pr) -> tuple[str, ...]:
    """The actions for one PR: "update", "label", "unlabel", "comment". Empty means leave it alone."""
    if pr.base != BASE_BRANCH or pr.cross_repository:
        return ()
    if pr.author == DEPENDABOT_LOGIN:
        # Dependabot alone writes to its branches; ask it to rebase. It rebases a conflict by itself.
        if pr.mergeable == "MERGEABLE" and pr.behind_by > 0 and not pr.rebase_requested:
            return ("comment",)
        return ()
    if pr.author != AGENT_LOGIN:
        return ()
    if pr.mergeable == "CONFLICTING":
        return () if LABEL in pr.labels else ("label",)
    if pr.mergeable != "MERGEABLE":
        return ()
    actions = ("unlabel",) if LABEL in pr.labels else ()
    if pr.behind_by > 0:
        actions += ("update",)
    return actions


def _rebase_requested(node: dict[str, Any]) -> bool:
    """True when the agent commented REBASE_COMMENT after the head commit (ISO timestamps compare as text)."""
    try:
        committed = str(node["commits"]["nodes"][-1]["commit"]["committedDate"])
    except (KeyError, IndexError, TypeError):
        return False
    for comment in (node.get("comments") or {}).get("nodes") or []:
        author = (comment.get("author") or {}).get("login")
        if author == AGENT_LOGIN and str(comment.get("body", "")).strip() == REBASE_COMMENT:
            if str(comment.get("createdAt", "")) >= committed:
                return True
    return False


def to_pr(node: dict[str, Any], behind_by: int) -> Pr:
    author = node.get("author") or {}
    login = author.get("login") if author.get("__typename") == "Bot" else None
    return Pr(
        number=int(node["number"]),
        node_id=str(node["id"]),
        head_oid=str(node["headRefOid"]),
        author=login,
        base=str(node.get("baseRefName")),
        cross_repository=bool(node.get("isCrossRepository")),
        mergeable=str(node.get("mergeable")),
        review_decision=node.get("reviewDecision"),
        behind_by=behind_by,
        labels=frozenset(label["name"] for label in (node.get("labels") or {}).get("nodes") or []),
        rebase_requested=_rebase_requested(node),
    )


def _mine(node: dict[str, Any]) -> bool:
    author = node.get("author") or {}
    return (
        author.get("__typename") == "Bot"
        and author.get("login") in (AGENT_LOGIN, DEPENDABOT_LOGIN)
        and node.get("baseRefName") == BASE_BRANCH
        and not node.get("isCrossRepository")
    )


def read_nodes(repo: str, gh: Gh, sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Open PRs. Reads again, a few times, while an agent PR has mergeable UNKNOWN."""
    owner, name = repo.split("/")
    nodes: list[dict[str, Any]] = []
    for attempt in range(READS):
        answer = gh(["api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"])
        try:
            nodes = answer["data"]["repository"]["pullRequests"]["nodes"]
        except (KeyError, TypeError):
            raise GhError("GitHub GraphQL answer has no pull requests") from None
        if not any(_mine(n) and n.get("mergeable") == "UNKNOWN" for n in nodes) or attempt == READS - 1:
            break
        sleep(RETRY_SECONDS)
    return nodes


def apply(repo: str, pr: Pr, action: str, gh: Gh) -> str:
    """Do one action. Returns a line for the log."""
    if action == "update":
        gh(["api", "graphql", "-f", f"query={UPDATE}", "-f", f"id={pr.node_id}", "-f", f"oid={pr.head_oid}"])
        return f"#{pr.number}: rebased on {BASE_BRANCH} (was {pr.behind_by} commits behind, head {pr.head_oid[:7]})"
    if action == "comment":
        gh(["api", "-X", "POST", f"repos/{repo}/issues/{pr.number}/comments", "-f", f"body={REBASE_COMMENT}"])
        return f"#{pr.number}: {pr.behind_by} commits behind {BASE_BRANCH}, asked Dependabot to rebase"
    if action == "label":
        gh(["api", "-X", "POST", f"repos/{repo}/issues/{pr.number}/labels", "-f", f"labels[]={LABEL}"])
        return f"#{pr.number}: conflicts with {BASE_BRANCH}, labelled {LABEL}"
    gh(["api", "-X", "DELETE", f"repos/{repo}/issues/{pr.number}/labels/{LABEL}"])
    return f"#{pr.number}: no conflict any more, removed {LABEL}"


def _head_moved(error: GhError) -> bool:
    text = str(error).lower()
    return "expected" in text and "head" in text


def run(repo: str, gh: Gh, sleep: Callable[[float], None] = time.sleep) -> int:
    try:
        nodes = read_nodes(repo, gh, sleep)
    except GhError as error:
        print(f"::error::Could not read the open pull requests: {error}")
        return 1
    failed = False
    for node in nodes:
        if not _mine(node):
            continue
        behind = 0
        try:
            if node.get("mergeable") == "MERGEABLE":
                answer = gh(["api", f"repos/{repo}/compare/{BASE_BRANCH}...{node['headRefOid']}"])
                behind = int(answer["behind_by"])
            pr = to_pr(node, behind)
            actions = decide(pr)
            if not actions:
                print(f"#{pr.number}: left alone ({pr.mergeable}, {pr.review_decision or 'not approved'}, {behind} behind)")
            for action in actions:
                print(apply(repo, pr, action, gh))
        except GhError as error:
            if _head_moved(error):
                print(f"#{node['number']}: head moved since it was read, skipped")
                continue
            print(f"::error::#{node['number']}: {error}")
            failed = True
        except (KeyError, TypeError, ValueError):
            print(f"::error::#{node.get('number')}: unexpected answer from GitHub")
            failed = True
    return 1 if failed else 0


def run_gh(args: list[str]) -> Any:
    """`gh <args>` with GH_TOKEN from the environment. Returns the parsed JSON, or None for no output."""
    try:
        result = subprocess.run(  # noqa: S603
            ["gh", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False  # noqa: S607
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise GhError(type(error).__name__) from None
    if result.returncode != 0:
        raise GhError(" ".join(result.stderr.split())[:300] or f"gh exited {result.returncode}")
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise GhError("gh returned output that is not JSON") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        print("--repo must be OWNER/NAME", file=sys.stderr)
        return 2
    return run(args.repo, run_gh)


if __name__ == "__main__":
    sys.exit(main())
