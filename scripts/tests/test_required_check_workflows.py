"""The workflows of the required checks report on `merge_group` and never cancel a run of the same head (CHB-219).

A merge queue waits for the required checks on its own `merge_group` event: a workflow without that
trigger never reports there, and the queue blocks every merge.

A run cancelled while in progress leaves a cancelled check run on its head, and that blocks the PR until
someone re-runs it (#142, #151). Only a run for a new head (`pull_request` `synchronize`, first attempt)
may cancel: the run it cancels belongs to an older head. A re-run replays the original event, so
`github.run_attempt` tells it apart.

The required checks come from infra/github/repository.tf, so a new required check is covered here too.
Only the workflow of a required check may have a job of that name (CHB-234): the ruleset matches by name.
The tests read the files as text: `python3 -I` has no YAML parser. The expression reader understands
only the forms these workflows use, and fails on anything else, so a new form needs a test first.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
RULESET = ROOT / "infra" / "github" / "repository.tf"
# Workflows whose steps read `github.event.pull_request`: on `merge_group` that payload is absent.
PR_PAYLOAD_WORKFLOWS = ("supply-chain.yml", "triage.yml")
# The workflow of each required check. A new required check fails the first test until it is added here.
CHECK_WORKFLOWS = {"backend": "ci.yml", "frontend": "ci.yml", "e2e": "ci.yml", "supply-chain": "supply-chain.yml", "triage": "triage.yml"}
# Workflows outside the required checks that follow the same cancel rule (CHB-234). They have no
# `merge_group` trigger, so only the cancel tests apply to them.
OTHER_PR_WORKFLOWS = ("dependabot-auto-merge.yml",)


def required_checks() -> list[str]:
    match = re.search(r"required_checks\s*=\s*\[([^\]]*)\]", RULESET.read_text(encoding="utf-8"))
    assert match, "required_checks not found in infra/github/repository.tf"
    return re.findall(r'"([^"]+)"', match.group(1))


def job_ids(text: str) -> list[str]:
    jobs = text.split("\njobs:\n", 1)
    return re.findall(r"^  ([A-Za-z0-9_-]+):", jobs[1], re.MULTILINE) if len(jobs) == 2 else []


def check_run_names(text: str) -> set[str]:
    """The check run names of a workflow's jobs: the job-level `name:` when set, otherwise the job id."""
    jobs = text.split("\njobs:\n", 1)
    names = re.findall(r"^    name: ['\"]?([^'\"\n]+?)['\"]?\s*$", jobs[1], re.MULTILINE) if len(jobs) == 2 else []
    return set(job_ids(text)) | set(names)


def required_workflows() -> list[Path]:
    return sorted({WORKFLOWS / CHECK_WORKFLOWS[check] for check in required_checks()})


def triggers(text: str) -> list[str]:
    block = re.search(r"^on:\n((?:  .*\n|\s*\n)+)", text, re.MULTILINE)
    assert block, "no `on:` block"
    return re.findall(r"^  ([a-z_]+):", block.group(1), re.MULTILINE)


def concurrency(text: str) -> tuple[str, str]:
    match = re.search(r"^concurrency:\n  group: (.+)\n  cancel-in-progress: (.+)\n", text, re.MULTILINE)
    assert match, "no workflow-level `concurrency:` with `group:` then `cancel-in-progress:`"
    return match.group(1).strip(), match.group(2).strip()


def lookup(path: str, context: dict[str, str]) -> str:
    assert re.fullmatch(r"github(\.[a-z_]+)+", path), f"unsupported expression term {path!r}"
    return context.get(path, "")


def value(term: str, context: dict[str, str]) -> str:
    term = term.strip()
    if re.fullmatch(r"'[^']*'", term):
        return term[1:-1]
    return lookup(term, context)


def evaluate(expression: str, context: dict[str, str]) -> str | bool:
    """Evaluate `a || b`, or `a == 'x' && b != 'y' && success()`, as GitHub does for these forms.

    `success()` counts as true: the tests ask what a step does when the steps before it passed.
    """
    expression = expression.strip()
    if "&&" in expression or "==" in expression or "!=" in expression:
        assert "||" not in expression, f"unsupported mix of && and || in {expression!r}"
        for clause in expression.split("&&"):
            if clause.strip() == "success()":
                continue
            op = "!=" if "!=" in clause else "=="
            left, sep, right = clause.partition(op)
            assert sep, f"unsupported clause {clause!r}"
            # GitHub compares strings ignoring case.
            if (value(left, context).lower() == value(right, context).lower()) != (op == "=="):
                return False
        return True
    for term in expression.split("||"):
        if result := value(term, context):
            return result
    return ""


def render(template: str, context: dict[str, str]) -> str:
    def text(match: re.Match[str]) -> str:
        result = evaluate(match.group(1), context)
        return str(result).lower() if isinstance(result, bool) else result

    return re.sub(r"\$\{\{(.*?)\}\}", text, template)


def cancels(setting: str, context: dict[str, str]) -> bool:
    if setting in ("true", "false"):
        return setting == "true"
    match = re.fullmatch(r"\$\{\{(.*)\}\}", setting)
    assert match, f"unsupported cancel-in-progress {setting!r}"
    return evaluate(match.group(1), context) is True


def run(event: str, action: str = "", attempt: str = "1", pr: str = "7", head: str = "b" * 40) -> dict[str, str]:
    context = {"github.event_name": event, "github.event.action": action, "github.run_attempt": attempt}
    if event in ("pull_request", "pull_request_review"):
        context |= {"github.event.pull_request.number": pr, "github.event.pull_request.head.sha": head, "github.ref": f"refs/pull/{pr}/merge"}
    elif event == "merge_group":
        context |= {"github.ref": f"refs/heads/gh-readonly-queue/main/pr-{pr}-{head}"}
    else:
        context |= {"github.ref": "refs/heads/main"}
    return context


# Runs that never move the head: none of them may cancel a run in their group.
SAME_HEAD_RUNS = {
    "pull_request opened": run("pull_request", "opened"),
    "pull_request reopened": run("pull_request", "reopened"),
    "pull_request edited": run("pull_request", "edited"),
    "re-run of a synchronize run": run("pull_request", "synchronize", attempt="2"),
    "pull_request_review submitted": run("pull_request_review", "submitted"),
    "pull_request_review dismissed": run("pull_request_review", "dismissed"),
    "re-run of a review run": run("pull_request_review", "submitted", attempt="3"),
    "merge_group": run("merge_group", "checks_requested"),
    "push to main": run("push"),
}


class RequiredCheckWorkflowsTest(unittest.TestCase):
    def test_every_required_check_has_a_workflow_with_that_job(self):
        self.assertEqual(sorted(required_checks()), sorted(CHECK_WORKFLOWS))
        for check, name in CHECK_WORKFLOWS.items():
            with self.subTest(check=check):
                self.assertIn(check, job_ids((WORKFLOWS / name).read_text(encoding="utf-8")))

    def test_no_other_workflow_has_a_job_named_like_a_required_check(self):
        # The ruleset matches required checks by name. A second check run with that name, even a
        # skipped one, hides which one failed and can stand in for the real check (CHB-234).
        for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):  # GitHub runs both
            clashes = {check for check in check_run_names(path.read_text(encoding="utf-8")) & set(CHECK_WORKFLOWS) if CHECK_WORKFLOWS[check] != path.name}
            with self.subTest(workflow=path.name):
                self.assertEqual(clashes, set())

    def test_every_required_check_workflow_runs_on_merge_group(self):
        for path in required_workflows():
            with self.subTest(workflow=path.name):
                self.assertIn("merge_group", triggers(path.read_text(encoding="utf-8")))

    def test_only_a_new_head_cancels_a_run_in_progress(self):
        for path in required_workflows() + [WORKFLOWS / name for name in OTHER_PR_WORKFLOWS]:
            _, cancel = concurrency(path.read_text(encoding="utf-8"))
            for name, context in SAME_HEAD_RUNS.items():
                with self.subTest(workflow=path.name, run=name):
                    self.assertFalse(cancels(cancel, context))

    def test_a_push_of_a_new_head_cancels_the_run_of_the_older_head(self):
        for path in required_workflows() + [WORKFLOWS / name for name in OTHER_PR_WORKFLOWS]:
            group, cancel = concurrency(path.read_text(encoding="utf-8"))
            old = run("pull_request", "synchronize", head="a" * 40)
            new = run("pull_request", "synchronize", head="b" * 40)
            with self.subTest(workflow=path.name):
                self.assertTrue(cancels(cancel, new))
                self.assertEqual(render(group, old), render(group, new))
                self.assertEqual(render(group, run("pull_request", "opened", head="a" * 40)), render(group, new))

    def test_a_new_head_does_not_cancel_an_edit_run_of_the_same_head(self):
        # `edited` (title, body or base) can start between a push and that push's `synchronize` run.
        for path in required_workflows():
            text = path.read_text(encoding="utf-8")
            types = re.search(r"^  pull_request:\n    types: \[([^\]]*)\]", text, re.MULTILINE)
            if not types or "edited" not in types.group(1):
                continue  # without `types`, pull_request means opened, synchronize and reopened
            group, _ = concurrency(text)
            with self.subTest(workflow=path.name):
                self.assertNotEqual(render(group, run("pull_request", "edited")), render(group, run("pull_request", "synchronize")))

    def test_groups_keep_pull_requests_and_queue_entries_apart(self):
        for path in required_workflows():
            group, _ = concurrency(path.read_text(encoding="utf-8"))
            with self.subTest(workflow=path.name):
                self.assertNotEqual(render(group, run("pull_request", "synchronize", pr="7")), render(group, run("pull_request", "synchronize", pr="8")))
                self.assertNotEqual(render(group, run("merge_group", pr="7")), render(group, run("merge_group", pr="8")))
                self.assertNotEqual(render(group, run("merge_group", pr="7")), render(group, run("pull_request", "synchronize", pr="7")))

    def test_no_job_sets_its_own_concurrency(self):
        for path in required_workflows():
            with self.subTest(workflow=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), re.compile(r"^ +concurrency:", re.MULTILINE))

    def test_on_merge_group_no_step_reads_the_pull_request_payload(self):
        def steps_event(step: str) -> tuple[str, str]:
            return ("pull_request_review", "submitted") if "pull_request_review" in step else ("pull_request", "synchronize")

        for name in PR_PAYLOAD_WORKFLOWS:
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            steps = re.split(r"^      - ", text.split("\n    steps:\n", 1)[1], flags=re.MULTILINE)[1:]
            self.assertRegex(steps[0], r"if: github\.event_name == 'merge_group'", f"{name}: the first step reports the merge_group pass")
            self.assertTrue(any("github.event.pull_request" in step for step in steps), f"{name}: no step reads the PR payload; the step parse failed")
            for step in steps:
                if "github.event.pull_request" not in step:
                    continue
                with self.subTest(workflow=name, step=step.splitlines()[0]):
                    condition = re.search(r"^(?:        )?if: (.+)$", step, re.MULTILINE)
                    self.assertIsNotNone(condition, "a step that reads github.event.pull_request needs an `if:`")
                    self.assertIs(evaluate(condition.group(1), run("merge_group", "checks_requested")), False)
                    self.assertIs(evaluate(condition.group(1), run(*steps_event(step))), True, "the step still runs on its own event")

    def test_deploy_ignores_ci_runs_of_the_merge_queue(self):
        # CI now also runs on `merge_group`; only a CI run of a push to main may deploy.
        gate = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
        self.assertRegex(gate, re.compile(r"^      github\.event\.workflow_run\.event == 'push' &&$", re.MULTILINE))


class ExpressionReaderTest(unittest.TestCase):
    def test_reads_a_job_name_as_its_check_run_name(self):
        text = "on:\n  push:\njobs:\n  build:\n    name: triage\n    steps:\n      - name: not a job\n"
        self.assertEqual(check_run_names(text), {"build", "triage"})

    def test_the_fixture_tells_heads_apart(self):
        # The group tests above would pass by construction if run() ignored the head.
        group = "x-${{ github.event.pull_request.head.sha }}"
        self.assertNotEqual(render(group, run("pull_request", "synchronize", head="a" * 40)), render(group, run("pull_request", "synchronize", head="b" * 40)))

    def test_renders_a_comparison_as_github_does(self):
        self.assertEqual(render("g-${{ github.event.action == 'edited' }}", run("pull_request", "edited")), "g-true")
        self.assertEqual(render("g-${{ github.event.action == 'edited' }}", run("pull_request", "opened")), "g-false")

    def test_reads_or_with_a_fallback(self):
        self.assertEqual(evaluate("github.event.pull_request.number || github.ref", {"github.ref": "r"}), "r")

    def test_reads_not_equal_and_success(self):
        self.assertIs(evaluate("success() && github.event_name != 'merge_group'", {"github.event_name": "merge_group"}), False)
        self.assertIs(evaluate("success() && github.event_name != 'merge_group'", {"github.event_name": "pull_request"}), True)

    def test_reads_and_of_equalities_ignoring_case(self):
        self.assertIs(evaluate("github.event_name == 'Push' && github.run_attempt == '1'", {"github.event_name": "push", "github.run_attempt": "1"}), True)
        self.assertIs(evaluate("github.event_name == 'push'", {"github.event_name": "merge_group"}), False)

    def test_rejects_forms_it_does_not_know(self):
        with self.assertRaises(AssertionError):
            evaluate("contains(github.ref, 'push')", {})
        with self.assertRaises(AssertionError):
            cancels("${{ !cancelled() }}", {})


if __name__ == "__main__":
    unittest.main()
