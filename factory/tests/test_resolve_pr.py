import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))

from resolve_pr import main, rejection, select

REPO = "owner/repo"
HEAD = "a" * 40
BASE = "b" * 40


def _pr(number: int = 7, **changes: object) -> dict:
    pr = {
        "number": number,
        "state": "open",
        "draft": False,
        "user": {"login": "chb-factory-agent[bot]"},
        "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}},
        "head": {"sha": HEAD, "repo": {"full_name": REPO}},
    }
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        target = pr
        for key in parents:
            target = target[key]
        target[leaf] = value
    return pr


class RejectionTest(unittest.TestCase):
    def test_same_repository_open_pr_into_main_qualifies(self) -> None:
        self.assertIsNone(rejection(_pr(), REPO, HEAD))

    def test_each_failed_condition_rejects(self) -> None:
        cases = {
            "not open": _pr(state="closed"),
            "draft": _pr(draft=True),
            "base is not main": _pr(base__ref="release"),
            "base is another repository": _pr(base__repo={"full_name": "other/repo"}),
            "head is a fork": _pr(head__repo={"full_name": "fork/repo"}),
            "head moved": _pr(head__sha="c" * 40),
            "Dependabot PR": _pr(user={"login": "dependabot[bot]"}),
            "no number": _pr(number="7; id"),  # type: ignore[arg-type]
            "bad base SHA": _pr(base__sha="main\nnumber=1"),
        }
        for reason, pr in cases.items():
            with self.subTest(reason):
                self.assertEqual(rejection(pr, REPO, HEAD), reason)

    def test_missing_fields_reject(self) -> None:
        self.assertEqual(rejection({}, REPO, HEAD), "not open")
        self.assertEqual(rejection(_pr(draft=None), REPO, HEAD), "draft")
        self.assertEqual(rejection(_pr(head__repo=None), REPO, HEAD), "head is a fork")


class SelectTest(unittest.TestCase):
    def test_lowest_qualifying_number_wins(self) -> None:
        prs = [_pr(9), _pr(3, draft=True), _pr(5)]
        self.assertEqual(select(prs, REPO, HEAD)["number"], 5)  # type: ignore[index]

    def test_nothing_qualifies(self) -> None:
        self.assertIsNone(select([_pr(head__sha="c" * 40)], REPO, HEAD))
        self.assertIsNone(select([], REPO, HEAD))

    def test_malformed_head_sha_raises(self) -> None:
        for sha in ["abc", "A" * 40, HEAD + "\n", ""]:
            with self.subTest(sha), self.assertRaises(ValueError):
                select([_pr()], REPO, sha)


class MainTest(unittest.TestCase):
    def _run(self, *args: str, prs: list[dict]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i, pr in enumerate(prs):
                path = Path(tmp) / f"pr-{i}.json"
                path.write_text(json.dumps(pr))
                files.append(str(path))
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main([*args, *files])
            return code, out.getvalue()

    def test_prints_github_output_lines(self) -> None:
        code, out = self._run("--repo", REPO, "--head-sha", HEAD, prs=[_pr(12)])
        self.assertEqual((code, out), (0, f"number=12\nbase-sha={BASE}\n"))

    def test_prints_nothing_when_no_pr_qualifies(self) -> None:
        self.assertEqual(self._run("--repo", REPO, "--head-sha", HEAD, prs=[_pr(draft=True)]), (0, ""))

    def test_malformed_head_sha_exits_2(self) -> None:
        self.assertEqual(self._run("--repo", REPO, "--head-sha", "x", prs=[_pr()]), (2, ""))


if __name__ == "__main__":
    unittest.main()
