#!/usr/bin/env python3
"""Owner agreement with the review verdict (CHB-141, ADR-ea8c stage 1). Standard library only.

For every PR merged into `main` since --since, takes the last PR comment of the reviewer App
that carries the review marker (written by gate.py), and reads the owner's reactions on it:
👍 agree, 👎 disagree (both count as disagree), none: no answer.
A PR without such a comment was not reviewed by the model (docs-only, or merged before CHB-141).

Stage 1 exit (ADR-ea8c, decision 8): 20 answered PRs with 2 or fewer disagreements, and no
critical finding missed that the owner found. The script checks the first part. The owner
checks the second part in the 👎 replies it lists.

Usage: agreement.py [--repo OWNER/NAME] [--since YYYY-MM-DD]
Reads with `gh api`. Exit code: 0 report printed, 1 GitHub read failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

REVIEWER = "chb-factory-reviewer[bot]"
MARKER = "<!-- chb-review reviewed:"  # gate.py: COMMENT_MARKER + REVIEWED + sha
START = "2026-11-10"  # CHB-141: review comments with the reaction line start here.
NEEDED = 20
MAX_DISAGREE = 2

Gh = Callable[[str], list]


@dataclass
class Tally:
    agree: int = 0
    disagree: int = 0
    no_answer: int = 0
    disagree_prs: list[int] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return self.agree + self.disagree

    @property
    def reviewed(self) -> int:
        return self.answered + self.no_answer


def _login(item: Mapping[str, object]) -> object:
    user = item.get("user")
    return user.get("login") if isinstance(user, Mapping) else None


def review_comment(comments: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The last comment by the reviewer App with the marker. Text alone is forgeable, so both count."""
    own = [c for c in comments if _login(c) == REVIEWER and MARKER in str(c.get("body", ""))]
    return own[-1] if own else None


def answer(pr: Mapping[str, object], owner: str) -> str | None:
    """agree, disagree or no answer; None when the model did not review the PR."""
    comment = review_comment(pr.get("comments", []))  # type: ignore[arg-type]
    if comment is None:
        return None
    reactions = comment.get("reactions", [])
    contents = {r.get("content") for r in reactions if _login(r) == owner}  # type: ignore[union-attr]
    if "-1" in contents:
        return "disagree"
    if "+1" in contents:
        return "agree"
    return "no answer"


def tally(prs: Sequence[Mapping[str, object]], owner: str) -> Tally:
    result = Tally()
    for pr in prs:
        verdict = answer(pr, owner)
        if verdict == "agree":
            result.agree += 1
        elif verdict == "disagree":
            result.disagree += 1
            result.disagree_prs.append(int(pr["number"]))  # type: ignore[call-overload]
        elif verdict == "no answer":
            result.no_answer += 1
    return result


def stage1_exit(counts: Tally) -> str:
    if counts.disagree > MAX_DISAGREE:
        return f"no ({counts.disagree} disagree, at most {MAX_DISAGREE} allowed)"
    if counts.answered < NEEDED:
        return f"not yet (needs {NEEDED} answered PRs, has {counts.answered})"
    status = "met on agreement; the owner checks that no 👎 reply names a missed critical finding"
    if counts.disagree_prs:
        status += " (" + ", ".join(f"#{n}" for n in counts.disagree_prs) + ")"
    return status


def report(counts: Tally) -> str:
    return (
        f"PRs reviewed: {counts.reviewed}; agree {counts.agree}, disagree {counts.disagree}, "
        f"no answer {counts.no_answer}. Stage 1 exit: {stage1_exit(counts)}."
    )


def fetch_prs(gh: Gh, repo: str, since: str) -> list[dict[str, object]]:
    """PRs merged into main since the date, with their comments; reactions on the review comment only."""
    prs = []
    for pr in gh(f"repos/{repo}/pulls?state=closed&base=main&per_page=100"):
        merged_at = pr.get("merged_at")
        if not isinstance(merged_at, str) or merged_at < since:
            continue
        number = int(pr["number"])
        comments = [dict(c) for c in gh(f"repos/{repo}/issues/{number}/comments?per_page=100")]
        comment = review_comment(comments)
        if comment is not None:
            comment["reactions"] = gh(f"repos/{repo}/issues/comments/{int(comment['id'])}/reactions?per_page=100")  # type: ignore[index,call-overload]
        prs.append({"number": number, "comments": comments})
    return prs


def gh_api(path: str) -> list:
    """Every item of a paginated list endpoint. Raises CalledProcessError when gh fails."""
    out = subprocess.run(  # noqa: S603
        ["gh", "api", "--paginate", path, "--jq", ".[]"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _gh_text(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603,S607


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="OWNER/NAME; default: the repository of the current directory")
    parser.add_argument("--since", default=START, help=f"first merge date, YYYY-MM-DD (default {START})")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        print("--since must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        repo = args.repo or _gh_text("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        owner = _gh_text("repo", "view", repo, "--json", "owner", "--jq", ".owner.login")
        prs = fetch_prs(gh_api, repo, args.since)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"cannot read GitHub: {error}", file=sys.stderr)
        return 1
    print(f"Merged into main since {args.since} in {repo}; owner {owner}.")
    print(report(tally(prs, owner)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
