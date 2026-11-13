"""CHB-60: triage of review findings, and the `triage` check.

The four check cases (no findings, triage present, triage missing, triage from another account)
come first. Then the command: findings of the latest review, the decision file, the comment, the
Caveats section, the calls it makes with a fake `gh`.
"""

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))

import as_agent
import triage_review as tr

SHA = "a" * 40
OLD_SHA = "b" * 40
REPO = "owner/repo"

FINDINGS = (
    "- **improvement** (Code quality) backend/src/chb/request\\_limits.py:238: Chunked bodies get 400, not 413."
    " Fix: Raise a 413 from the receive wrapper.\n"
    "- **cleanup** (Architecture) backend/src/chb/app.py:94: The limiter sits on `app.state` for tests."
    " Fix: Pass it into create\\_app.\n"
    "- **improvement** (Tests) backend/tests/test\\_request\\_limits.py:142: The assertion accepts 400 or 413."
    " Fix: Assert 400.\n"
)


def review_body(sha: str = SHA, findings: str = FINDINGS) -> str:
    return (
        "**Fail** (shadow mode: this check is not required)\n\n"
        "Merge Safety: **8.07** (pass at 8.50 with no critical finding)\n\n"
        "Reasons:\n\n- Merge Safety 8.07 is below 8.50\n\n"
        "| Dimension | Weight | Score | Claimed |\n|---|---|---|---|\n| Tests | 15% | 6.00 | 7 |\n\n"
        f"Summary: The PR adds two middlewares.\n\n{findings}\n"
        "Owner: react 👍 if you agree with this verdict, 👎 if not.\n\n"
        f"<!-- chb-review reviewed:{sha} -->\n"
    )


def comment(login: str, body: str, number: int = 1) -> dict:
    return {"id": number, "user": {"login": login}, "body": body}


def review(sha: str = SHA, findings: str = FINDINGS, number: int = 1) -> dict:
    return comment(tr.REVIEWER, review_body(sha, findings), number)


def triage(sha: str = SHA, login: str = tr.AGENT, number: int = 2) -> dict:
    return comment(login, f"Triage of review {sha}\n\n| x |\n\n<!-- chb-triage reviewed:{sha} -->\n", number)


class CheckTest(unittest.TestCase):
    def test_no_findings_passes(self) -> None:
        verdict = tr.evaluate([review(findings="No findings.\n")])
        self.assertTrue(verdict.ok, verdict.message)

    def test_no_review_comment_passes(self) -> None:
        self.assertTrue(tr.evaluate([comment("islomar", "hello")]).ok)

    def test_triage_present_passes(self) -> None:
        verdict = tr.evaluate([review(), triage()])
        self.assertTrue(verdict.ok, verdict.message)

    def test_triage_missing_fails_and_names_the_sha(self) -> None:
        verdict = tr.evaluate([review()])
        self.assertFalse(verdict.ok)
        self.assertIn(SHA, verdict.message)
        self.assertIn("3 findings", verdict.message)

    def test_triage_from_another_account_fails(self) -> None:
        for login in ("islomar", "mallory", "chb-factory-reviewer[bot]", "chb-factory-agent"):
            with self.subTest(login=login):
                self.assertFalse(tr.evaluate([review(), triage(login=login)]).ok)

    def test_triage_for_another_sha_fails(self) -> None:
        self.assertFalse(tr.evaluate([review(), triage(sha=OLD_SHA)]).ok)

    def test_a_forged_review_from_another_account_is_ignored(self) -> None:
        forged = comment("mallory", review_body(OLD_SHA))
        self.assertTrue(tr.evaluate([review(findings="No findings.\n"), forged]).ok)

    def test_only_the_latest_review_counts(self) -> None:
        old = review(OLD_SHA, number=1)
        new = review(SHA, findings="No findings.\n", number=3)
        self.assertTrue(tr.evaluate([old, triage(OLD_SHA), new]).ok)
        self.assertTrue(tr.evaluate([old, new]).ok)
        self.assertFalse(tr.evaluate([review(SHA, number=1), triage(SHA), review(OLD_SHA, number=3)]).ok)

    def test_a_review_with_an_unreadable_sha_fails_closed(self) -> None:
        bad = comment(tr.REVIEWER, "x\n<!-- chb-review reviewed:nothex -->\n")
        self.assertFalse(tr.evaluate([bad]).ok)

    def test_the_agent_login_is_the_app_slug(self) -> None:
        self.assertEqual(tr.AGENT, f"{as_agent.APP_SLUG}[bot]")
        self.assertEqual(tr.REPOSITORY, as_agent.REPOSITORY)


class ParseTest(unittest.TestCase):
    def test_findings_are_numbered_from_one_with_their_fields(self) -> None:
        sha, found = tr.parse_review(review_body())
        self.assertEqual(sha, SHA)
        self.assertEqual([f.number for f in found], [1, 2, 3])
        self.assertEqual(found[0].kind, "improvement")
        self.assertEqual(found[0].dimension, "Code quality")
        self.assertEqual(found[0].where, "backend/src/chb/request_limits.py:238")
        self.assertTrue(found[0].message.startswith("Chunked bodies get 400"))

    def test_reason_bullets_are_not_findings(self) -> None:
        _, found = tr.parse_review(review_body(findings=""))
        self.assertEqual(found, [])

    def test_a_finding_without_a_location_still_counts(self) -> None:
        _, found = tr.parse_review(review_body(findings="- **critical** (Security) The key leaks. Fix: Rotate.\n"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "critical")


class DecisionFileTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.found = tr.parse_review(review_body())

    def test_accepts_one_line_per_finding(self) -> None:
        text = "1 apply: wrong status code\n2: discard - test hook only\n3 doubt because the client may rewrite the header\n"
        decisions, problems = tr.parse_decisions(text, self.found)
        self.assertEqual(problems, [])
        self.assertEqual([d.decision for d in decisions], ["apply", "discard", "doubt"])
        self.assertEqual(decisions[1].reason, "test hook only")
        self.assertEqual(decisions[2].reason, "because the client may rewrite the header")

    def test_blank_lines_and_comments_are_ignored(self) -> None:
        _, problems = tr.parse_decisions("# note\n\n1 apply a\n2 apply b\n\n3 apply c\n", self.found)
        self.assertEqual(problems, [])

    def test_names_the_finding_that_has_no_line(self) -> None:
        _, problems = tr.parse_decisions("1 apply a\n3 apply c\n", self.found)
        self.assertEqual(len(problems), 1)
        self.assertIn("finding 2", problems[0])
        self.assertIn("no decision", problems[0])

    def test_names_the_line_that_has_no_reason(self) -> None:
        for line in ("2 discard", "2 discard:", "2 discard -   "):
            with self.subTest(line=line):
                _, problems = tr.parse_decisions(f"1 apply a\n{line}\n3 apply c\n", self.found)
                self.assertEqual(len(problems), 1)
                self.assertIn("finding 2", problems[0])
                self.assertIn("reason", problems[0])

    def test_refuses_an_unknown_decision_a_duplicate_and_a_missing_finding_number(self) -> None:
        text = "1 apply a\n1 discard b\n2 maybe c\n9 apply d\nnonsense\n"
        _, problems = tr.parse_decisions(text, self.found)
        joined = "\n".join(problems)
        self.assertIn("finding 1", joined)
        self.assertIn("twice", joined)
        self.assertIn("line 3", joined)
        self.assertIn("finding 9", joined)
        self.assertIn("line 5", joined)
        self.assertIn("finding 3", joined)

    def test_decision_words_are_case_insensitive(self) -> None:
        decisions, problems = tr.parse_decisions("1 Apply a\n2 DISCARD b\n3 Doubt c\n", self.found)
        self.assertEqual(problems, [])
        self.assertEqual([d.decision for d in decisions], ["apply", "discard", "doubt"])


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.found = tr.parse_review(review_body())
        self.decisions, _ = tr.parse_decisions("1 apply a\n2 discard b | c @team\n3 doubt <b>x</b> unsure\n", self.found)

    def test_comment_has_title_table_and_marker(self) -> None:
        text = tr.render_comment(SHA, self.found, self.decisions)
        self.assertEqual(text.splitlines()[0], f"Triage of review {SHA}")
        self.assertIn("| # | Finding | Decision | Reason |", text)
        self.assertIn("| 1 |", text)
        self.assertIn("| apply |", text)
        self.assertTrue(text.rstrip().endswith(f"<!-- chb-triage reviewed:{SHA} -->"))

    def test_reasons_cannot_break_the_table_mention_or_inject_html(self) -> None:
        text = tr.render_comment(SHA, self.found, self.decisions)
        row = next(line for line in text.splitlines() if line.startswith("| 2 |"))
        self.assertEqual(len(re.findall(r"(?<!\\)\|", row)), 5)
        self.assertNotIn("@team", text)
        self.assertNotIn("<b>", text)

    def test_long_text_is_cut(self) -> None:
        decisions, _ = tr.parse_decisions("1 apply " + "x" * 5000 + "\n2 apply b\n3 apply c\n", self.found)
        text = tr.render_comment(SHA, self.found, decisions)
        self.assertLess(len(text), 4000)

    def test_caveats_list_only_the_doubts(self) -> None:
        section = tr.caveats_section(SHA, self.found, self.decisions)
        self.assertTrue(section.startswith("## Caveats"))
        self.assertIn(SHA[:12], section)
        self.assertIn("The assertion accepts 400 or 413", section)
        self.assertNotIn("Chunked bodies", section)

    def test_caveats_say_none_when_there_is_no_doubt(self) -> None:
        decisions, _ = tr.parse_decisions("1 apply a\n2 apply b\n3 discard c\n", self.found)
        self.assertIn("No doubts", tr.caveats_section(SHA, self.found, decisions))

    def test_replaces_the_previous_section_between_markers(self) -> None:
        old = tr.caveats_section(OLD_SHA, self.found, self.decisions)
        body = f"## Why it matters\n\nx\n\n{old}\n\n## Risks\n\ny\n"
        new = tr.caveats_section(SHA, self.found, self.decisions)
        result = tr.replace_caveats(body, new)
        self.assertEqual(result.count("## Caveats"), 1)
        self.assertIn(SHA[:12], result)
        self.assertNotIn(OLD_SHA[:12], result)
        self.assertIn("## Risks\n\ny", result)

    def test_replaces_a_hand_written_caveats_heading_up_to_the_next_heading(self) -> None:
        body = "## What\n\nx\n\n## Caveats\n\nold text\n\n## Risks\n\ny\n"
        result = tr.replace_caveats(body, tr.caveats_section(SHA, self.found, self.decisions))
        self.assertNotIn("old text", result)
        self.assertEqual(result.count("## Caveats"), 1)
        self.assertIn("## Risks\n\ny", result)

    def test_appends_when_there_is_no_section_and_replaces_again_without_growth(self) -> None:
        section = tr.caveats_section(SHA, self.found, self.decisions)
        once = tr.replace_caveats("## What\n\nx\n", section)
        self.assertEqual(tr.replace_caveats(once, section), once)
        self.assertTrue(once.startswith("## What"))
        self.assertIn("## Caveats", once)

    def test_a_missing_body_gets_the_section(self) -> None:
        self.assertIn("## Caveats", tr.replace_caveats("", tr.caveats_section(SHA, self.found, self.decisions)))


class FakeGh:
    """Records `gh` calls. `comments` is what the comments endpoint returns."""

    def __init__(self, comments: list[dict], body: str = "## What\n\nx\n", runs: list[dict] | None = None) -> None:
        self.comments = comments
        self.body = body
        self.runs = runs or []
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:2] == ["api", "--paginate"] and "issues/7/comments" in args[2]:
            return "\n".join(json.dumps(c) for c in self.comments)
        if args[:2] == ["pr", "view"]:
            return self.body
        if args[:2] == ["api", "repos/owner/repo/pulls/7"]:
            return SHA
        if args[:1] == ["api"] and "actions/workflows/triage.yml/runs" in args[1]:
            return json.dumps({"workflow_runs": self.runs})
        if args[:2] in (["pr", "edit"], ["pr", "comment"]):
            path = args[args.index("--body-file") + 1]
            self.bodies.append(Path(path).read_text())
        return ""

    def verbs(self) -> list[str]:
        return [" ".join(c[:2]) for c in self.calls]


def run(argv: list[str], gh: FakeGh) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = tr.main(argv, gh)
    return code, out.getvalue(), err.getvalue()


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def decisions_file(self, text: str) -> str:
        path = Path(self.dir.name) / "decisions.txt"
        path.write_text(text)
        return str(path)

    def test_prints_the_numbered_findings_of_the_latest_review(self) -> None:
        gh = FakeGh([review(OLD_SHA, number=1), review(SHA, number=3)])
        code, out, _ = run(["7"], gh)
        self.assertEqual(code, 0)
        self.assertIn(f"Review {SHA}", out)
        self.assertIn("1. improvement (Code quality) backend/src/chb/request_limits.py:238", out)
        self.assertIn("3. improvement (Tests)", out)
        self.assertEqual(gh.verbs(), ["api --paginate"])

    def test_prints_that_there_is_nothing_to_triage(self) -> None:
        code, out, _ = run(["7"], FakeGh([review(findings="No findings.\n")]))
        self.assertEqual(code, 0)
        self.assertIn("no findings", out.lower())

    def test_exit_1_without_a_review_comment(self) -> None:
        code, _, err = run(["7"], FakeGh([comment("islomar", "hi")]))
        self.assertEqual(code, 1)
        self.assertIn("no review comment", err.lower())

    def test_a_file_that_misses_a_finding_posts_nothing(self) -> None:
        gh = FakeGh([review()])
        code, _, err = run(["7", "--file", self.decisions_file("1 apply a\n2 apply b\n")], gh)
        self.assertEqual(code, 2)
        self.assertIn("finding 3", err)
        self.assertEqual(gh.verbs(), ["api --paginate"])

    def test_a_file_that_misses_a_reason_posts_nothing(self) -> None:
        gh = FakeGh([review()])
        code, _, err = run(["7", "--file", self.decisions_file("1 apply a\n2 apply\n3 apply c\n")], gh)
        self.assertEqual(code, 2)
        self.assertIn("finding 2", err)
        self.assertEqual(gh.verbs(), ["api --paginate"])

    def test_posts_the_caveats_then_the_comment(self) -> None:
        gh = FakeGh([review()])
        code, out, _ = run(["7", "--file", self.decisions_file("1 apply a\n2 discard b\n3 doubt c\n")], gh)
        self.assertEqual(code, 0, out)
        self.assertEqual(gh.verbs(), ["api --paginate", "pr view", "pr edit", "pr comment"])
        edit, post = gh.bodies
        self.assertIn("## Caveats", edit)
        self.assertIn("## What\n\nx", edit)
        self.assertTrue(post.startswith(f"Triage of review {SHA}"))
        self.assertIn("Doubt, finding 3", out)
        self.assertIn("The assertion accepts 400 or 413", out)

    def test_the_posted_comment_satisfies_the_check(self) -> None:
        gh = FakeGh([review()])
        run(["7", "--file", self.decisions_file("1 apply a\n2 discard b\n3 apply c\n")], gh)
        posted = comment(tr.AGENT, gh.bodies[-1], 9)
        self.assertTrue(tr.evaluate([review(), posted]).ok)

    def test_check_mode_exit_codes(self) -> None:
        self.assertEqual(run(["--check", "7", "--repo", REPO], FakeGh([review(), triage()]))[0], 0)
        code, out, _ = run(["--check", "7", "--repo", REPO], FakeGh([review()]))
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)
        self.assertEqual(run(["--check", "7", "--repo", REPO], FakeGh([review(findings="No findings.\n")]))[0], 0)

    def test_the_pr_number_must_be_digits(self) -> None:
        for bad in ("7;rm", "-1", "x", "07/../8"):
            with self.subTest(pr=bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                tr.main([bad], FakeGh([]))


class RerunTest(unittest.TestCase):
    RUNS = [
        {"id": 1, "head_sha": SHA, "event": "pull_request", "status": "completed", "conclusion": "success"},
        {"id": 2, "head_sha": SHA, "event": "pull_request", "status": "completed", "conclusion": "failure"},
        {"id": 3, "head_sha": SHA, "event": "pull_request", "status": "in_progress", "conclusion": None},
        {"id": 4, "head_sha": OLD_SHA, "event": "pull_request", "status": "completed", "conclusion": "success"},
        {"id": 5, "head_sha": SHA, "event": "issue_comment", "status": "completed", "conclusion": "success"},
    ]

    def test_only_completed_pull_request_runs_of_the_head_are_rerun(self) -> None:
        self.assertEqual(tr.runs_to_rerun(self.RUNS, SHA), [1, 2])

    def test_rerun_mode_asks_for_the_head_and_reruns_each_run(self) -> None:
        gh = FakeGh([], runs=self.RUNS)
        code, _, _ = run(["--rerun", "7", "--repo", REPO], gh)
        self.assertEqual(code, 0)
        reruns = [c for c in gh.calls if c[:2] == ["run", "rerun"]]
        self.assertEqual([c[2] for c in reruns], ["1", "2"])


class WorkflowTest(unittest.TestCase):
    TRIAGE = (ROOT / ".github/workflows/triage.yml").read_text()
    RERUN = (ROOT / ".github/workflows/triage-rerun.yml").read_text()

    def test_the_check_job_is_named_triage_and_runs_on_pull_request(self) -> None:
        self.assertRegex(self.TRIAGE, r"(?m)^  triage:\n")
        self.assertRegex(self.TRIAGE, r"(?m)^  pull_request:\n    types: \[opened, synchronize, reopened\]")
        self.assertNotIn("pull_request_target", self.TRIAGE)
        self.assertNotIn("secrets.", self.TRIAGE)

    def test_the_rerun_reacts_to_comments_from_main_without_secrets_or_pr_code(self) -> None:
        self.assertIn("issue_comment:", self.RERUN)
        self.assertIn("types: [created, edited, deleted]", self.RERUN)
        self.assertIn("github.event.issue.pull_request", self.RERUN)
        self.assertNotIn("secrets.", self.RERUN)
        self.assertNotIn("${{ github.event.comment", self.RERUN)
        self.assertNotIn("github.head_ref", self.RERUN)
        self.assertIn("actions: write", self.RERUN)

    def test_every_action_is_pinned_by_sha_and_no_run_block_expands_event_text(self) -> None:
        for name, text in (("triage.yml", self.TRIAGE), ("triage-rerun.yml", self.RERUN)):
            with self.subTest(workflow=name):
                for use in re.findall(r"uses: (\S+)", text):
                    self.assertRegex(use, r"@[0-9a-f]{40}$")
                for block in re.findall(r"run: [|>-]*\n((?:\s{10,}.*\n)+)", text):
                    self.assertNotIn("${{", block)

    def test_the_workflows_run_the_script_in_isolated_mode(self) -> None:
        self.assertIn("python3 -I factory/triage_review.py --check", self.TRIAGE)
        self.assertIn("python3 -I factory/triage_review.py --rerun", self.RERUN)


class RulesetTest(unittest.TestCase):
    def test_triage_is_a_required_check(self) -> None:
        text = (ROOT / "infra/github/repository.tf").read_text()
        match = re.search(r"required_checks\s*=\s*\[([^\]]*)\]", text)
        self.assertIsNotNone(match)
        self.assertIn('"triage"', match.group(1))  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
