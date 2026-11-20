#!/usr/bin/env python3
"""Triage the findings of the automated PR review (CHB-60, ADR-ea8c decision 5). Stdlib only.

Usage:
  factory/bin/triage-review <PR>                 print the findings of the latest review, numbered
  factory/bin/triage-review <PR> --file FILE     post the triage of that review
  python3 -I factory/triage_review.py --check <PR>   the `triage` check (exit 0 pass, 1 fail, 2 error)
  python3 -I factory/triage_review.py --rerun <PR>   re-run the `Triage` runs of the PR head

The review is the last PR comment of the reviewer App with the marker
`<!-- chb-review reviewed:<sha> -->` (factory/review/gate.py). Its findings are the lines
`- **<kind>** (<dimension>) <file>:<line>: <message> Fix: <fix>`.

FILE starts with the line `review <sha>`, the full sha of the review it triages, then has one line
per finding: `<number> apply|discard|doubt <reason>`, for example `2 doubt the client may rewrite
the header`. A missing or other sha, a missing finding or a missing reason refuses the whole file
(CHB-195: on 2026-11-13 a triage written for one review was posted against the next one).
Then the script
1. replaces the "Caveats" section of the PR body with the doubts,
2. posts the comment "Triage of review <sha>" with a table of finding, decision and reason.
Both go through factory/bin/as-agent, so the agent's account writes them. The `triage` check
accepts the comment only from that account.
While a finding is `apply` and the PR head is still the reviewed commit, the script posts nothing
and exits 1: push the fixes first (CHB-212).

The `triage` check (CHB-60, CHB-195) fails
- on a review cut at its length limit (the line `(truncated)` of gate.py): its last findings are lost;
- when the PR head is not the reviewed commit and the files changed since that commit are not all
  docs, by the rule the reviewer uses to carry a verdict forward (factory/review/plan.py,
  factory/review/docs_list.py). The review of the new head must arrive and be triaged first;
- when the review has findings and no triage comment of the agent's account for its sha.

The comment and the findings are untrusted text: it is read as data, cut to a length, flattened
to one line with Markdown and mentions escaped, and never executed. Reads use the REST API of the
repository, not a PR file or script.

Exit codes: 0 done, 1 no review comment, a failed check or `apply` fixes not pushed,
2 usage, bad file or GitHub error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python3 -I does not add the script's folder
sys.path.insert(0, str(HERE / "review"))

import as_agent  # noqa: E402
import agreement  # noqa: E402
import docs_list  # noqa: E402
import gate  # noqa: E402

REVIEWER = agreement.REVIEWER
AGENT = f"{as_agent.APP_SLUG}[bot]"
REPOSITORY = as_agent.REPOSITORY
AS_AGENT = HERE / "bin" / "as-agent"
TRIAGE_WORKFLOW = "triage.yml"
DECISIONS = ("apply", "discard", "doubt")
SHA_RE = re.compile(r"[0-9a-f]{40}")
REVIEW_LINE = re.compile(r"^review\s+(\S+)$")
# GitHub's compare endpoint lists at most 300 files. A list that long may be cut: not known to be docs.
COMPARE_FILE_CAP = 300
REVIEW_SHA = re.compile(r"<!-- chb-review reviewed:([0-9a-f]{40}) -->")
TRIAGE_MARKER = "<!-- chb-triage reviewed:{sha} -->"
FINDING = re.compile(r"^- \*\*([A-Za-z]+)\*\* \(([^)]*)\) (.*)$")
WHERE = re.compile(r"^(\S+?): (.*)$")
DECISION_LINE = re.compile(r"^\s*(\d+)\s*[:.)\-]?\s*([A-Za-z]+)\b[ \t]*[:\-–—]?[ \t]*(.*?)\s*$")
CAVEATS_BLOCK = re.compile(r"## Caveats[ \t]*\n<!-- chb-caveats-start -->.*?<!-- chb-caveats-end -->", re.DOTALL)
CAVEATS_HEADING = re.compile(r"(?m)^## Caveats[ \t]*$")
NEXT_HEADING = re.compile(r"(?m)^## ")
CELL_LIMIT = 300
FINDING_LIMIT = 200

Gh = Callable[[list[str]], str]
"""Runs `gh <args>` and returns its stdout. Raises subprocess.CalledProcessError on failure."""


@dataclass(frozen=True)
class Finding:
    number: int
    kind: str
    dimension: str
    where: str
    message: str


@dataclass(frozen=True)
class Decision:
    number: int
    decision: str
    reason: str


@dataclass(frozen=True)
class Verdict:
    ok: bool
    message: str


# --- reading the review ------------------------------------------------------------------


def _unescape(text: str) -> str:
    """Undo the Markdown escapes of gate.py `_plain`."""
    return re.sub(r"\\([\\`*_\[\]<>|#])", r"\1", text).replace("@​", "@")


def _flat(text: str, limit: int) -> str:
    """One line, no control characters (a terminal escape in a finding), cut to `limit`."""
    flat = " ".join(text.split())
    flat = re.sub(r"[\x00-\x1f\x7f]", "", flat)
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _md(text: str, limit: int) -> str:
    """Untrusted text as one escaped Markdown line: no mentions, no HTML, no table breaks."""
    flat = re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", _flat(text, limit))
    return flat.replace("@", "@​")


def parse_review(body: str) -> tuple[str | None, list[Finding]]:
    """The reviewed sha (None if the marker is unreadable) and the numbered findings."""
    marker = REVIEW_SHA.search(body)
    found: list[Finding] = []
    for line in body.splitlines():
        match = FINDING.match(line.strip())
        if match is None:
            continue
        kind, dimension, rest = match.groups()
        where_match = WHERE.match(rest)
        where, message = where_match.groups() if where_match else ("", rest)
        found.append(Finding(len(found) + 1, kind, dimension, _unescape(where), _unescape(message)))
    return (marker.group(1) if marker else None), found


def _login(item: Mapping[str, object]) -> object:
    user = item.get("user")
    return user.get("login") if isinstance(user, Mapping) else None


def triage_present(comments: Sequence[Mapping[str, object]], sha: str) -> bool:
    """A comment of the agent's account carrying the marker of this sha. Another account never counts."""
    marker = TRIAGE_MARKER.format(sha=sha)
    return any(_login(c) == AGENT and marker in str(c.get("body", "")) for c in comments)


def _count(n: int) -> str:
    return f"{n} finding" + ("" if n == 1 else "s")


def truncated(body: str) -> bool:
    """gate.py cut the summary at its length limit: a line that is only the marker. A finding line never is."""
    return any(line.strip() == gate.TRUNCATED for line in body.splitlines())


def _truncated_message(sha: str) -> str:
    return (
        f"Review {sha} is truncated: gate.py cut it at its length limit, so its last findings are missing "
        "and cannot be triaged. Push a commit to get a new review, or the human decides."
    )


Changed = Callable[[str, str], "list[str] | None"]
"""changed(reviewed, head): the paths changed from the reviewed commit to the head, None when unknown."""


def _stale(sha: str, head: str, paths: Sequence[str] | None) -> Verdict:
    if paths is None:
        why = "the files changed since the review are unknown (a rebase, or GitHub could not compare)"
    elif not paths:
        why = "no file changed since the review, and the reviewer reviews such a head again"
    else:
        ordered = [p for p in paths if not docs_list.is_doc(p)] + [p for p in paths if docs_list.is_doc(p)]
        shown = ", ".join(_flat(p, 200) for p in ordered[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        why = f"the head changes more than docs: {shown}"
    return Verdict(
        False,
        f"Review {sha} is older than the PR head {head}, and {why}. "
        f"Wait for the review of {head}, then triage it. Speed up: if no review comes, re-run the "
        "`Review trigger` run of that commit in the Actions tab of the PR.",
    )


def evaluate(comments: Sequence[Mapping[str, object]], head: str | None = None, changed: Changed | None = None) -> Verdict:
    """The `triage` check: the latest review must cover the head and needs a triage of its sha when it has findings.

    head None skips the head comparison (the Stop hook: the agent cannot hurry the reviewer).
    """
    review = agreement.review_comment(comments)
    if review is None:
        return Verdict(True, "No review comment on this PR: nothing to triage.")
    body = str(review.get("body", ""))
    sha, found = parse_review(body)
    if sha is None:
        return Verdict(False, "The latest review comment has no readable reviewed sha. Triage cannot be checked.")
    if truncated(body):
        return Verdict(False, _truncated_message(sha))
    if head is not None and head != sha:
        paths = changed(sha, head) if changed is not None else None
        if paths is None or not docs_list.docs_only(paths):
            return _stale(sha, head, paths)
    if not found:
        return Verdict(True, f"Review {sha} has no findings: nothing to triage.")
    if triage_present(comments, sha):
        return Verdict(True, f"Review {sha} has {_count(len(found))} and a triage comment from {AGENT}.")
    return Verdict(
        False,
        f"Review {sha} has {_count(len(found))} and no triage comment from {AGENT}. "
        "Run factory/bin/triage-review <PR>, then factory/bin/triage-review <PR> --file FILE.",
    )


# --- the decision file -------------------------------------------------------------------


def split_review_line(text: str, sha: str) -> tuple[str, str | None]:
    """The file with the line `review <sha>` and the lines before it blanked, and the problem when that line is missing or names another sha.

    Blank lines keep each decision line at its line number in the file, for the refusal messages.
    """
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = REVIEW_LINE.match(line)
        if match is None:
            return "", f"the first line must be `review {sha}`, the review this file triages."
        if match.group(1) != sha:
            named = _flat(match.group(1), 60)
            return "", f"the file triages review {named}, but the latest review is {sha}. Write the file for review {sha}."
        return "\n".join([""] * (index + 1) + lines[index + 1 :]), None
    return "", f"the first line must be `review {sha}`, the review this file triages."


def parse_decisions(text: str, found: Sequence[Finding]) -> tuple[list[Decision], list[str]]:
    """Decisions in finding order, and every problem of the file (empty when it is valid)."""
    problems: list[str] = []
    chosen: dict[int, Decision] = {}
    seen: set[int] = set()
    numbers = {f.number for f in found}
    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = DECISION_LINE.match(line)
        if match is None:
            problems.append(f"line {index}: cannot read it. Expected `<number> apply|discard|doubt <reason>`.")
            continue
        number, word, reason = int(match.group(1)), match.group(2).lower(), match.group(3)
        if number not in numbers:
            problems.append(f"line {index}: finding {number} does not exist (the review has {_count(len(found))}).")
            continue
        if number in seen:
            problems.append(f"line {index}: finding {number} appears twice.")
            continue
        seen.add(number)
        if word not in DECISIONS:
            problems.append(f"line {index}: finding {number} has the decision {word!r}. Use apply, discard or doubt.")
        elif not reason:
            problems.append(f"line {index}: finding {number} has no reason.")
        else:
            chosen[number] = Decision(number, word, reason)
    problems += [f"finding {n} has no decision line." for n in sorted(numbers - seen)]
    return [chosen[n] for n in sorted(chosen)], problems


# --- what is posted ----------------------------------------------------------------------


def _describe(finding: Finding, limit: int = FINDING_LIMIT) -> str:
    where = f" {finding.where}" if finding.where else ""
    return _flat(f"{finding.kind} ({finding.dimension}){where}: {finding.message}", limit)


def render_comment(sha: str, found: Sequence[Finding], decisions: Sequence[Decision]) -> str:
    by_number = {f.number: f for f in found}
    lines = [
        f"Triage of review {sha}",
        "",
        "| # | Finding | Decision | Reason |",
        "|---|---|---|---|",
    ]
    for d in decisions:
        lines.append(
            f"| {d.number} | {_md(_describe(by_number[d.number]), FINDING_LIMIT)} | {d.decision} | {_md(d.reason, CELL_LIMIT)} |"
        )
    lines += ["", TRIAGE_MARKER.format(sha=sha), ""]
    return "\n".join(lines)


def doubt_lines(found: Sequence[Finding], decisions: Sequence[Decision]) -> list[str]:
    """One line per doubt for the chat, each finding looked up by its number."""
    by_number = {f.number: f for f in found}
    return [
        f"Doubt, finding {d.number}: {_flat(_describe(by_number[d.number], 1000), 1200)} Reason: {_flat(d.reason, CELL_LIMIT)}"
        for d in decisions
        if d.decision == "doubt"
    ]


def caveats_section(sha: str, found: Sequence[Finding], decisions: Sequence[Decision]) -> str:
    by_number = {f.number: f for f in found}
    doubts = [d for d in decisions if d.decision == "doubt"]
    lines = ["## Caveats", "<!-- chb-caveats-start -->"]
    if doubts:
        lines += [f"Doubts of the triage of review {sha[:12]}. The human decides each one.", ""]
        lines += [
            f"- Finding {d.number}: {_md(_describe(by_number[d.number]), FINDING_LIMIT)} Reason: {_md(d.reason, CELL_LIMIT)}"
            for d in doubts
        ]
    else:
        lines.append(f"No doubts in the triage of review {sha[:12]}.")
    lines.append("<!-- chb-caveats-end -->")
    return "\n".join(lines)


def replace_caveats(body: str, section: str) -> str:
    """Put `section` in the PR body in place of the previous Caveats section, or at the end."""
    if CAVEATS_BLOCK.search(body):
        return CAVEATS_BLOCK.sub(lambda _: section, body, count=1)
    heading = CAVEATS_HEADING.search(body)
    if heading:
        after = NEXT_HEADING.search(body, heading.end())
        tail = body[after.start() :] if after else ""
        head = body[: heading.start()]
        return head + section + ("\n\n" + tail if tail else "\n")
    return (body.rstrip() + "\n\n" if body.strip() else "") + section + "\n"


# --- GitHub ------------------------------------------------------------------------------


def make_gh(prefix: Sequence[str]) -> Gh:
    def gh(args: list[str]) -> str:
        return subprocess.run(  # noqa: S603
            [*prefix, "gh", *args], check=True, capture_output=True, text=True, timeout=120
        ).stdout

    return gh


def read_comments(gh: Gh, repo: str, pr: str) -> list[dict]:
    out = gh(["api", "--paginate", f"repos/{repo}/issues/{pr}/comments?per_page=100", "--jq", ".[]"])
    comments = []
    for line in out.splitlines():
        if line.strip():
            item = json.loads(line)
            if isinstance(item, dict):
                comments.append(item)
    return comments


def runs_to_rerun(runs: Sequence[Mapping[str, object]], head_sha: str) -> list[int]:
    """Completed `pull_request` runs of the head. A running one reads the live comments by itself."""
    return [
        int(str(r["id"]))
        for r in runs
        if r.get("head_sha") == head_sha and r.get("event") == "pull_request" and r.get("status") == "completed"
    ]


def head_sha(gh: Gh, repo: str, pr: str) -> str:
    return gh(["api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"]).strip()


def changed_paths(gh: Gh, repo: str, base: str, head: str) -> list[str] | None:
    """Paths changed from base to head when base is an ancestor of head, else None. Both names of a rename count.

    Like `git merge-base --is-ancestor` plus `git diff --no-renames` in factory/review/plan.py. GitHub's
    status `ahead` means base is an ancestor; `diverged` (a rebase), `behind` or `identical` give None.
    A malformed answer or a list at the cap gives None. A GitHub error raises.
    """
    try:
        answer = json.loads(gh(["api", f"repos/{repo}/compare/{base}...{head}"]))
    except ValueError:
        return None
    if not isinstance(answer, dict) or answer.get("status") != "ahead":
        return None
    files = answer.get("files")
    if not isinstance(files, list) or len(files) >= COMPARE_FILE_CAP:
        return None
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            return None
        paths.append(item["filename"])
        previous = item.get("previous_filename")
        if isinstance(previous, str):
            paths.append(previous)
    return paths


def rerun(gh: Gh, repo: str, pr: str) -> int:
    head = head_sha(gh, repo, pr)
    if not SHA_RE.fullmatch(head):
        print(f"::error::PR {pr} has no readable head sha.")
        return 2
    body = json.loads(gh(["api", f"repos/{repo}/actions/workflows/{TRIAGE_WORKFLOW}/runs?head_sha={head}&per_page=100"]))
    for run_id in runs_to_rerun(body.get("workflow_runs", []), head):
        try:
            gh(["run", "rerun", str(run_id), "--repo", repo])
            print(f"Re-ran run {run_id}.")
        except subprocess.CalledProcessError as error:
            print(f"::warning::could not re-run run {run_id}: {_flat(str(error.stderr or error), 300)}")
    return 0


# --- command line ------------------------------------------------------------------------


def _pr_number(text: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,8}", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a PR number")
    return text


def _print_findings(sha: str, found: Sequence[Finding]) -> None:
    print(f"Review {sha}: {_count(len(found))}.")
    for f in found:
        print(f"{f.number}. {_flat(_describe(f, 1000), 1200)}")
    if found:
        print(
            f"\nWrite the first line `review {sha}`, then one line per finding: "
            "`<number> apply|discard|doubt <reason>`. Run again with --file FILE."
        )


def post_triage(gh: Gh, repo: str, pr: str, sha: str, found: Sequence[Finding], decisions: Sequence[Decision]) -> None:
    """Caveats first, the comment last: the comment is what turns the check green."""
    body = gh(["pr", "view", pr, "--repo", repo, "--json", "body", "--jq", ".body"])
    with tempfile.TemporaryDirectory() as tmp:
        body_file = Path(tmp) / "pr-body.md"
        body_file.write_text(replace_caveats(body, caveats_section(sha, found, decisions)))
        gh(["pr", "edit", pr, "--repo", repo, "--body-file", str(body_file)])
        comment_file = Path(tmp) / "triage-comment.md"
        comment_file.write_text(render_comment(sha, found, decisions))
        gh(["pr", "comment", pr, "--repo", repo, "--body-file", str(comment_file)])


def _check(gh: Gh, repo: str, pr: str, comments: Sequence[Mapping[str, object]]) -> int:
    """The `triage` check against the live PR head. Exit 0 pass, 1 fail, 2 head unreadable."""
    head = head_sha(gh, repo, pr)
    if not SHA_RE.fullmatch(head):
        print(f"::error::PR {pr} has no readable head sha. Triage cannot be checked.")
        return 2
    verdict = evaluate(comments, head, lambda base, new: changed_paths(gh, repo, base, new))
    print(verdict.message if verdict.ok else f"::error::{verdict.message}")
    return 0 if verdict.ok else 1


def main(argv: Sequence[str], gh: Gh | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=_pr_number, help="PR number")
    parser.add_argument("--file", help="decisions: one line per finding")
    parser.add_argument("--repo", default=REPOSITORY, help="OWNER/NAME (default: this repository)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="the `triage` check, for the workflow")
    mode.add_argument("--rerun", action="store_true", help="re-run the Triage runs of the PR head, for the workflow")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        print("--repo must be OWNER/NAME", file=sys.stderr)
        return 2
    workflow_mode = args.check or args.rerun
    if gh is None:
        gh = make_gh([] if workflow_mode else [str(AS_AGENT)])
    try:
        if args.rerun:
            return rerun(gh, args.repo, args.pr)
        comments = read_comments(gh, args.repo, args.pr)
        if args.check:
            return _check(gh, args.repo, args.pr, comments)
        review = agreement.review_comment(comments)
        if review is None:
            print(f"PR {args.pr} has no review comment of {REVIEWER}.", file=sys.stderr)
            return 1
        body = str(review.get("body", ""))
        sha, found = parse_review(body)
        if sha is None:
            print("The latest review comment has no readable reviewed sha.", file=sys.stderr)
            return 1
        if truncated(body):
            print(f"{_truncated_message(sha)} Nothing was posted.", file=sys.stderr)
            return 1
        if not found:
            print(f"Review {sha} has no findings: nothing to triage.")
            return 0
        if args.file is None:
            _print_findings(sha, found)
            return 0
        rest, problem = split_review_line(Path(args.file).read_text(), sha)
        decisions, problems = parse_decisions(rest, found) if problem is None else ([], [problem])
        if problems:
            print(f"{args.file} is refused. Nothing was posted:", file=sys.stderr)
            for problem in problems:
                print(f"- {problem}", file=sys.stderr)
            return 2
        # The triage comment turns `triage` green, and auto-merge then merges at once (CHB-212):
        # an `apply` fix must be on the PR first, so the head must differ from the reviewed commit.
        # Equality only: a push without a fix also passes, and its own review follows.
        applied = [d for d in decisions if d.decision == "apply"]
        if applied:
            head = head_sha(gh, args.repo, args.pr)
            if not SHA_RE.fullmatch(head):
                print(f"PR {args.pr} has no readable head sha. Nothing was posted.", file=sys.stderr)
                return 2
            if head == sha:
                names = ", ".join(f"finding {d.number}" for d in applied)
                print(
                    f"Refused: marked apply: {names}. The PR head is still the reviewed commit {sha}. "
                    "Nothing was posted. The triage comment turns `triage` green, and auto-merge would merge "
                    "without the fixes. Push the fix commits first, then post the triage again.",
                    file=sys.stderr,
                )
                return 1
        doubts = doubt_lines(found, decisions)  # before the post: a lookup error must not follow a posted triage
        post_triage(gh, args.repo, args.pr, sha, found, decisions)
        print(f"Posted the triage of review {sha} on PR {args.pr}.")
        for line in doubts:
            print(line)
    except (OSError, subprocess.SubprocessError, ValueError, LookupError) as error:
        detail = getattr(error, "stderr", None) or error
        print(f"triage-review failed: {_flat(str(detail), 500)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
