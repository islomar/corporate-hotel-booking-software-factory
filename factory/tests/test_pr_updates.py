"""CHB-172, CHB-177: agent and Dependabot PRs stay up to date with main without the owner.

The decision table: which PRs the workflow updates, labels or leaves alone. The run: which GitHub
calls it makes, with a fake `gh`.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pr_updates as pu

REPO = "owner/repo"
OID = "a" * 40


def _pr(**changes: object) -> pu.Pr:
    fields: dict = {
        "number": 7,
        "node_id": "PR_x",
        "head_oid": OID,
        "author": "chb-factory-agent",
        "base": "main",
        "cross_repository": False,
        "mergeable": "MERGEABLE",
        "review_decision": None,
        "behind_by": 3,
        "labels": frozenset(),
        "rebase_requested": False,
    }
    fields.update(changes)
    return pu.Pr(**fields)


class DecideTest(unittest.TestCase):
    def test_behind_mergeable_and_not_approved_is_updated(self) -> None:
        self.assertEqual(pu.decide(_pr()), ("update",))
        self.assertEqual(pu.decide(_pr(review_decision="REVIEW_REQUIRED")), ("update",))
        self.assertEqual(pu.decide(_pr(review_decision="CHANGES_REQUESTED")), ("update",))

    def test_behind_mergeable_and_approved_is_updated_too(self) -> None:
        # CHB-177: strict_required_status_checks_policy is true, so an approved PR behind main cannot merge.
        # The rebase drops the approval; the owner approves the new head.
        self.assertEqual(pu.decide(_pr(review_decision="APPROVED")), ("update",))

    def test_up_to_date_is_left_alone(self) -> None:
        self.assertEqual(pu.decide(_pr(behind_by=0)), ())

    def test_conflicting_gets_the_label_and_no_update(self) -> None:
        self.assertEqual(pu.decide(_pr(mergeable="CONFLICTING")), ("label",))
        self.assertEqual(pu.decide(_pr(mergeable="CONFLICTING", review_decision="APPROVED")), ("label",))

    def test_conflicting_and_already_labelled_is_left_alone(self) -> None:
        self.assertEqual(pu.decide(_pr(mergeable="CONFLICTING", labels=frozenset({pu.LABEL}))), ())

    def test_a_conflicting_pr_that_is_not_behind_still_gets_the_label(self) -> None:
        self.assertEqual(pu.decide(_pr(mergeable="CONFLICTING", behind_by=0)), ("label",))

    def test_mergeable_unknown_is_left_alone(self) -> None:
        self.assertEqual(pu.decide(_pr(mergeable="UNKNOWN")), ())

    def test_a_label_on_a_mergeable_pr_is_removed(self) -> None:
        self.assertEqual(pu.decide(_pr(labels=frozenset({pu.LABEL}), behind_by=0)), ("unlabel",))
        self.assertEqual(pu.decide(_pr(labels=frozenset({pu.LABEL}))), ("unlabel", "update"))

    def test_a_pr_not_opened_by_the_agent_is_never_touched(self) -> None:
        for author in ("islomar", "chb-factory-agent-fake", "", None):
            with self.subTest(author=author):
                self.assertEqual(pu.decide(_pr(author=author)), ())
                self.assertEqual(pu.decide(_pr(author=author, mergeable="CONFLICTING")), ())

    def test_a_pr_not_into_main_is_never_touched(self) -> None:
        self.assertEqual(pu.decide(_pr(base="CHB-1-parent")), ())

    def test_a_pr_from_a_fork_is_never_touched(self) -> None:
        self.assertEqual(pu.decide(_pr(cross_repository=True)), ())


def _node(**changes: object) -> dict:
    node: dict = {
        "id": "PR_x",
        "number": 7,
        "headRefOid": OID,
        "author": {"login": "chb-factory-agent", "__typename": "Bot"},
        "baseRefName": "main",
        "isCrossRepository": False,
        "mergeable": "MERGEABLE",
        "reviewDecision": None,
        "labels": {"nodes": [{"name": "x"}]},
        "comments": {"nodes": []},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-11-12T10:00:00Z"}}]},
    }
    node.update(changes)
    return node


def _answer(*nodes: dict) -> dict:
    return {"data": {"repository": {"pullRequests": {"nodes": list(nodes)}}}}


class DependabotDecideTest(unittest.TestCase):
    def test_behind_and_mergeable_gets_a_rebase_comment(self) -> None:
        self.assertEqual(pu.decide(_pr(author="dependabot")), ("comment",))
        self.assertEqual(pu.decide(_pr(author="dependabot", review_decision="APPROVED")), ("comment",))

    def test_a_request_newer_than_the_head_is_not_repeated(self) -> None:
        self.assertEqual(pu.decide(_pr(author="dependabot", rebase_requested=True)), ())

    def test_up_to_date_or_conflicting_or_unknown_is_left_alone(self) -> None:
        # Dependabot rebases on its own when there is a conflict.
        self.assertEqual(pu.decide(_pr(author="dependabot", behind_by=0)), ())
        self.assertEqual(pu.decide(_pr(author="dependabot", mergeable="CONFLICTING")), ())
        self.assertEqual(pu.decide(_pr(author="dependabot", mergeable="UNKNOWN")), ())

    def test_never_updates_the_branch_or_labels(self) -> None:
        # The dependabot-branches ruleset lets only Dependabot push to its branches.
        for changes in ({}, {"labels": frozenset({pu.LABEL})}, {"mergeable": "CONFLICTING"}):
            with self.subTest(changes=changes):
                actions = pu.decide(_pr(author="dependabot", **changes))
                self.assertFalse({"update", "label", "unlabel"} & set(actions))

    def test_not_into_main_or_from_a_fork_is_never_touched(self) -> None:
        self.assertEqual(pu.decide(_pr(author="dependabot", base="x")), ())
        self.assertEqual(pu.decide(_pr(author="dependabot", cross_repository=True)), ())


class ToPrTest(unittest.TestCase):
    def test_reads_the_fields_the_decision_needs(self) -> None:
        self.assertEqual(
            pu.to_pr(_node(labels={"nodes": [{"name": "needs-rebase"}]}), 2),
            _pr(behind_by=2, labels=frozenset({"needs-rebase"})),
        )

    def test_a_rebase_request_by_the_agent_after_the_head_commit_is_pending(self) -> None:
        def request(login: str, body: str, at: str) -> dict:
            return {"nodes": [{"body": body, "createdAt": at, "author": {"login": login}}]}

        late, early = "2026-11-12T11:00:00Z", "2026-11-12T09:00:00Z"
        for login, body, at, expected in (
            (pu.AGENT_LOGIN, "@dependabot rebase", late, True),
            (pu.AGENT_LOGIN, "@dependabot rebase", early, False),
            ("islomar", "@dependabot rebase", late, False),
            (pu.AGENT_LOGIN, "thanks", late, False),
        ):
            with self.subTest(login=login, body=body, at=at):
                pr = pu.to_pr(_node(comments=request(login, body, at)), 1)
                self.assertEqual(pr.rebase_requested, expected)

    def test_a_missing_author_is_not_the_agent(self) -> None:
        self.assertIsNone(pu.to_pr(_node(author=None), 0).author)

    def test_a_user_with_the_agent_name_is_not_the_agent(self) -> None:
        # The agent is a GitHub App: its GraphQL type is Bot. A user cannot take the login "chb-factory-agent".
        pr = pu.to_pr(_node(author={"login": "chb-factory-agent", "__typename": "User"}), 0)
        self.assertEqual(pu.decide(pr), ())


class FakeGh:
    """Answers `gh api` calls from a script; records them."""

    def __init__(self, nodes: list[dict], behind: dict[int, int], fail: dict[str, Exception] | None = None) -> None:
        self.nodes, self.behind, self.fail = nodes, behind, fail or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> object:
        self.calls.append(args)
        text = " ".join(args)
        for needle, error in self.fail.items():
            if needle in text:
                raise error
        if "pullRequests(" in text:
            return _answer(*self.nodes)
        if "/compare/" in text:
            oid = next(a for a in args if "/compare/" in a).split("...")[-1]
            number = next(n["number"] for n in self.nodes if n["headRefOid"] == oid)
            return {"behind_by": self.behind[number]}
        return {}

    def writes(self) -> list[str]:
        return [" ".join(c) for c in self.calls if "mutation" in " ".join(c) or "-X" in c]


def _run(gh: FakeGh) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stdout(err):
        code = pu.run(REPO, gh, sleep=lambda _: None)
    return code, err.getvalue()


class RunTest(unittest.TestCase):
    def test_updates_a_behind_pr_with_a_rebase_expecting_the_head_it_read(self) -> None:
        gh = FakeGh([_node()], {7: 3})

        code, _ = _run(gh)

        self.assertEqual(code, 0)
        writes = gh.writes()
        self.assertEqual(len(writes), 1)
        self.assertIn("updatePullRequestBranch", writes[0])
        self.assertIn("updateMethod: REBASE", writes[0])
        self.assertIn(f"oid={OID}", writes[0])
        self.assertIn("id=PR_x", writes[0])

    def test_labels_a_conflicting_pr_and_does_not_update_it(self) -> None:
        gh = FakeGh([_node(mergeable="CONFLICTING")], {7: 3})

        _run(gh)

        writes = gh.writes()
        self.assertEqual(len(writes), 1)
        self.assertIn("-X POST", writes[0])
        self.assertIn("repos/owner/repo/issues/7/labels", writes[0])
        self.assertIn("labels[]=needs-rebase", writes[0])
        self.assertNotIn("updatePullRequestBranch", writes[0])

    def test_removes_a_stale_label(self) -> None:
        gh = FakeGh([_node(labels={"nodes": [{"name": "needs-rebase"}]})], {7: 0})

        _run(gh)

        self.assertEqual(len(gh.writes()), 1)
        self.assertIn("-X DELETE repos/owner/repo/issues/7/labels/needs-rebase", gh.writes()[0])

    def test_updates_an_approved_behind_pr(self) -> None:
        gh = FakeGh([_node(reviewDecision="APPROVED")], {7: 3})

        _run(gh)

        self.assertEqual(len(gh.writes()), 1)
        self.assertIn("updatePullRequestBranch", gh.writes()[0])

    def test_comments_on_a_behind_dependabot_pr_and_never_updates_its_branch(self) -> None:
        dependabot = {"login": "dependabot", "__typename": "Bot"}
        gh = FakeGh([_node(author=dependabot)], {7: 3})

        code, _ = _run(gh)

        self.assertEqual(code, 0)
        writes = gh.writes()
        self.assertEqual(len(writes), 1)
        self.assertIn("-X POST repos/owner/repo/issues/7/comments", writes[0])
        self.assertIn("body=@dependabot rebase", writes[0])

    def test_writes_nothing_for_foreign_and_current_prs(self) -> None:
        nodes = [
            _node(number=2, headRefOid="2" * 40, author={"login": "islomar", "__typename": "User"}),
            _node(number=3, headRefOid="3" * 40),
            _node(number=4, headRefOid="4" * 40, mergeable="UNKNOWN"),
        ]
        gh = FakeGh(nodes, {1: 5, 2: 5, 3: 0, 4: 5})

        code, _ = _run(gh)

        self.assertEqual(code, 0)
        self.assertEqual(gh.writes(), [])

    def test_does_not_ask_for_the_distance_of_a_pr_it_will_not_touch(self) -> None:
        gh = FakeGh([_node(author={"login": "islomar", "__typename": "User"})], {7: 5})

        _run(gh)

        self.assertFalse([c for c in gh.calls if any("/compare/" in a for a in c)])

    def test_a_moved_head_is_a_skip_not_a_failure(self) -> None:
        error = pu.GhError("Expected HEAD sha didn't match current HEAD on head branch.")
        gh = FakeGh([_node()], {7: 3}, fail={"updatePullRequestBranch": error})

        code, out = _run(gh)

        self.assertEqual(code, 0)
        self.assertIn("head moved", out)

    def test_another_error_goes_on_with_the_next_pr_and_fails_the_run(self) -> None:
        nodes = [_node(number=1, headRefOid="1" * 40), _node(number=2, headRefOid="2" * 40)]
        gh = FakeGh(nodes, {1: 1, 2: 1}, fail={"id=PR_x": pu.GhError("HTTP 403")})
        # Both PRs share the node id here, so every write fails: the run still tries both.

        code, out = _run(gh)

        self.assertEqual(code, 1)
        self.assertEqual(len(gh.writes()), 2)
        self.assertIn("::error::", out)

    def test_a_failed_read_fails_the_run(self) -> None:
        gh = FakeGh([], {}, fail={"pullRequests(": pu.GhError("HTTP 502")})

        code, _ = _run(gh)

        self.assertEqual(code, 1)

    def test_mergeable_unknown_is_read_again_before_deciding(self) -> None:
        answers = [_answer(_node(mergeable="UNKNOWN")), _answer(_node(mergeable="MERGEABLE"))]
        gh = FakeGh([], {7: 3})
        original = gh.__call__

        def call(args: list[str]) -> object:
            if "pullRequests(" in " ".join(args):
                gh.calls.append(args)
                return answers.pop(0) if answers else _answer(_node())
            return original(args)

        gh.nodes = [_node()]
        slept: list[float] = []

        with contextlib.redirect_stdout(io.StringIO()):
            code = pu.run(REPO, call, sleep=slept.append)

        self.assertEqual(code, 0)
        self.assertEqual(len(slept), 1)
        self.assertEqual(len([w for w in gh.writes() if "updatePullRequestBranch" in w]), 1)


class MainTest(unittest.TestCase):
    def test_refuses_a_repository_that_is_not_owner_slash_name(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pu.main(["--repo", "not a repo"]), 2)


if __name__ == "__main__":
    unittest.main()
