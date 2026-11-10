"""CHB-141: the owner's agreement with the review verdict, ADR-ea8c stage 1."""

import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))

from agreement import (
    REVIEWER,
    Tally,
    answer,
    fetch_prs,
    report,
    review_comment,
    stage1_exit,
    tally,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review" / "agreement-prs.json"
OWNER = "islomar"


def _prs() -> list:
    return json.loads(FIXTURE.read_text())


def _pr(number: int) -> dict:
    return next(p for p in _prs() if p["number"] == number)


class AnswerTest(unittest.TestCase):
    def test_owner_thumbs_up_agrees(self) -> None:
        self.assertEqual(answer(_pr(70), OWNER), "agree")

    def test_owner_thumbs_down_disagrees(self) -> None:
        self.assertEqual(answer(_pr(71), OWNER), "disagree")

    def test_no_reaction_is_no_answer(self) -> None:
        self.assertEqual(answer(_pr(72), OWNER), "no answer")

    def test_reactions_from_anyone_else_are_ignored(self) -> None:
        self.assertEqual(answer(_pr(73), OWNER), "no answer")

    def test_a_pr_without_a_review_comment_was_not_reviewed(self) -> None:
        self.assertIsNone(answer(_pr(74), OWNER))

    def test_only_the_reviewer_app_with_the_marker_counts(self) -> None:
        self.assertIsNone(answer(_pr(75), OWNER))

    def test_the_last_review_comment_holds_the_verdict(self) -> None:
        comment = review_comment(_pr(76)["comments"])
        self.assertIsNotNone(comment)
        self.assertEqual(comment["id"], 762)  # type: ignore[index]
        self.assertEqual(answer(_pr(76), OWNER), "agree")

    def test_both_thumbs_count_as_disagree(self) -> None:
        self.assertEqual(answer(_pr(77), OWNER), "disagree")


class GateCommentTest(unittest.TestCase):
    def test_finds_the_comment_gate_py_writes(self) -> None:
        from gate import Verdict, comment_body

        body = comment_body(Verdict(passed=True, merge_safety=Fraction(9)), head_sha="abc", summary="s")
        comments = [{"id": 1, "user": {"login": REVIEWER}, "body": body}]

        self.assertIs(review_comment(comments), comments[0])


class TallyTest(unittest.TestCase):
    def test_counts_the_fixture_prs(self) -> None:
        result = tally(_prs(), OWNER)

        self.assertEqual(result, Tally(agree=2, disagree=2, no_answer=2, disagree_prs=[71, 77]))
        self.assertEqual(result.reviewed, 6)
        self.assertEqual(result.answered, 4)


class Stage1ExitTest(unittest.TestCase):
    def test_not_yet_below_20_answered(self) -> None:
        self.assertEqual(stage1_exit(Tally(10, 1, 1)), "not yet (needs 20 answered PRs, has 11)")

    def test_no_with_more_than_2_disagree(self) -> None:
        self.assertEqual(stage1_exit(Tally(5, 3, 0)), "no (3 disagree, at most 2 allowed)")

    def test_met_at_20_answered_with_2_disagree(self) -> None:
        self.assertEqual(
            stage1_exit(Tally(18, 2, 4, [71, 77])),
            "met on agreement; the owner checks that no 👎 reply names a missed critical finding (#71, #77)",
        )

    def test_met_without_disagreement(self) -> None:
        self.assertEqual(
            stage1_exit(Tally(20, 0, 0)),
            "met on agreement; the owner checks that no 👎 reply names a missed critical finding",
        )


class ReportTest(unittest.TestCase):
    def test_the_line_from_the_ticket(self) -> None:
        self.assertEqual(
            report(Tally(10, 1, 1, [71])),
            "PRs reviewed: 12; agree 10, disagree 1, no answer 1. Stage 1 exit: not yet (needs 20 answered PRs, has 11).",
        )


class FetchTest(unittest.TestCase):
    def test_reads_merged_prs_since_the_date_and_reactions_of_the_last_review_comment(self) -> None:
        review = {"id": 9, "user": {"login": REVIEWER}, "body": "x <!-- chb-review reviewed:abc -->"}
        responses = {
            "repos/o/r/pulls?state=closed&base=main&per_page=100": [
                {"number": 3, "merged_at": "2026-11-12T10:00:00Z"},
                {"number": 2, "merged_at": None},
                {"number": 1, "merged_at": "2026-11-01T10:00:00Z"},
            ],
            "repos/o/r/issues/3/comments?per_page=100": [review],
            "repos/o/r/issues/comments/9/reactions?per_page=100": [{"user": {"login": OWNER}, "content": "+1"}],
        }
        calls: list[str] = []

        def gh(path: str) -> list:
            calls.append(path)
            return responses[path]

        prs = fetch_prs(gh, "o/r", "2026-11-10")

        self.assertEqual([p["number"] for p in prs], [3])
        self.assertEqual(answer(prs[0], OWNER), "agree")
        self.assertNotIn("repos/o/r/issues/1/comments?per_page=100", calls)


if __name__ == "__main__":
    unittest.main()
