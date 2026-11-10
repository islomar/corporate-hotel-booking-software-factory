import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflow"))

import route
from route import (
    ANOTHER_ROUND,
    NEXT_STAGE,
    PR_CAVEATS,
    STAGES,
    STOP,
    STOP_AND_NOTIFY,
    TRIAGE,
    Route,
    next_step,
)

SCHEMAS = Path(__file__).resolve().parents[1] / "workflow" / "schemas"

ANALYSIS = {
    "planFile": "docs/plans/CHB-62.md",
    "acceptanceCriteria": ["AC1. Schemas exist."],
    "isVerifiable": True,
    "blockingQuestions": [],
}
IMPLEMENTATION = {"typesClean": True, "lintClean": True, "testsGreen": True}
REVIEW = {
    "mergeSafety": 9.5,
    "scores": {"codeQuality": 9, "security": 10, "businessAlignment": 10, "tests": 9, "architecture": 10},
    "findings": [{"severity": "cleanup", "suggestedFix": "Rename x to count."}],
}
TRIAGE_OUTPUT = {"decision": "fix", "fixableByAgent": True}
VALID = {"analysis": ANALYSIS, "implementation": IMPLEMENTATION, "review": REVIEW, "triage": TRIAGE_OUTPUT}


def _with(stage: str, **fields: object) -> dict:
    output = copy.deepcopy(VALID[stage])
    output.update(fields)
    return output


def _without(stage: str, field: str) -> dict:
    return {k: v for k, v in VALID[stage].items() if k != field}


class SchemaShapeTest(unittest.TestCase):
    def test_one_schema_per_stage(self) -> None:
        self.assertEqual(sorted(p.stem for p in SCHEMAS.glob("*.json")), sorted(STAGES))

    def test_every_object_requires_all_its_fields_and_allows_no_others(self) -> None:
        def walk(node: object, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(sorted(node.get("required", [])), sorted(node.get("properties", {})), path)
                for key, value in node.items():
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    walk(value, f"{path}[{i}]")

        for path in SCHEMAS.glob("*.json"):
            walk(json.loads(path.read_text()), path.name)

    def test_review_uses_the_gate_severities_and_threshold(self) -> None:
        review = json.loads((SCHEMAS / "review.json").read_text())
        gate_schema = json.loads((Path(__file__).resolve().parents[1] / "review" / "schema.json").read_text())
        self.assertEqual(
            review["properties"]["findings"]["items"]["properties"]["severity"]["enum"],
            gate_schema["$defs"]["finding"]["properties"]["severity"]["enum"],
        )
        self.assertEqual(route.THRESHOLD, 8.5)


class ValidOutputTest(unittest.TestCase):
    def test_every_example_output_is_valid(self) -> None:
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.assertNotEqual(next_step(stage, VALID[stage]).step, STOP)


class AnalysisRoutingTest(unittest.TestCase):
    def test_blocking_questions_stop_and_notify(self) -> None:
        result = next_step("analysis", _with("analysis", blockingQuestions=["Which currency?"]))
        self.assertEqual(result, Route(STOP_AND_NOTIFY, ("Which currency?",)))

    def test_no_blocking_questions_go_to_the_next_stage(self) -> None:
        self.assertEqual(next_step("analysis", ANALYSIS), Route(NEXT_STAGE))


class ImplementationRoutingTest(unittest.TestCase):
    def test_each_failed_check_asks_for_another_round(self) -> None:
        for check in ("typesClean", "lintClean", "testsGreen"):
            with self.subTest(check=check):
                result = next_step("implementation", _with("implementation", **{check: False}))
                self.assertEqual(result, Route(ANOTHER_ROUND, (f"{check} is false",)))

    def test_all_checks_true_go_to_the_next_stage(self) -> None:
        self.assertEqual(next_step("implementation", IMPLEMENTATION), Route(NEXT_STAGE))


class ReviewRoutingTest(unittest.TestCase):
    def test_merge_safety_below_threshold_goes_to_triage(self) -> None:
        result = next_step("review", _with("review", mergeSafety=8.49, findings=[]))
        self.assertEqual(result, Route(TRIAGE, ("mergeSafety 8.49 is below 8.5",)))

    def test_merge_safety_at_threshold_passes(self) -> None:
        self.assertEqual(next_step("review", _with("review", mergeSafety=8.5)), Route(NEXT_STAGE))

    def test_a_critical_finding_goes_to_triage_even_with_a_high_score(self) -> None:
        findings = [{"severity": "critical", "suggestedFix": "Escape the input."}]
        result = next_step("review", _with("review", mergeSafety=10, findings=findings))
        self.assertEqual(result, Route(TRIAGE, ("1 critical finding",)))

    def test_both_reasons_are_reported(self) -> None:
        findings = [{"severity": "critical", "suggestedFix": "a"}, {"severity": "critical", "suggestedFix": "b"}]
        result = next_step("review", _with("review", mergeSafety=5, findings=findings))
        self.assertEqual(result, Route(TRIAGE, ("mergeSafety 5 is below 8.5", "2 critical findings")))

    def test_high_score_without_critical_goes_to_the_next_stage(self) -> None:
        self.assertEqual(next_step("review", REVIEW), Route(NEXT_STAGE))


class TriageRoutingTest(unittest.TestCase):
    def test_doubt_goes_to_pr_caveats(self) -> None:
        self.assertEqual(next_step("triage", _with("triage", decision="doubt")), Route(PR_CAVEATS))

    def test_fix_and_dismiss_go_to_the_next_stage(self) -> None:
        for decision in ("fix", "dismiss"):
            for fixable in (True, False):
                with self.subTest(decision=decision, fixable=fixable):
                    output = _with("triage", decision=decision, fixableByAgent=fixable)
                    self.assertEqual(next_step("triage", output), Route(NEXT_STAGE))


class FailClosedTest(unittest.TestCase):
    INVALID = {
        "analysis": [
            _with("analysis", blockingQuestions="none"),
            _with("analysis", blockingQuestions=[""]),
            _without("analysis", "planFile"),
            _with("analysis", planFile=""),
            _with("analysis", isVerifiable="yes"),
            _with("analysis", acceptanceCriteria=[1]),
        ],
        "implementation": [
            _with("implementation", testsGreen=1),
            _with("implementation", extra=True),
            _without("implementation", "testsGreen"),
        ],
        "review": [
            _with("review", mergeSafety=True),
            _with("review", mergeSafety=11),
            _with("review", mergeSafety=-1),
            _with("review", findings=[{"severity": "blocker", "suggestedFix": "x"}]),
            _with("review", findings=[{"severity": "critical"}]),
            _with("review", scores={"codeQuality": 9}),
            _without("review", "scores"),
        ],
        "triage": [
            _with("triage", decision="maybe"),
            _with("triage", fixableByAgent=None),
            _without("triage", "fixableByAgent"),
        ],
    }

    def test_invalid_output_for_each_schema_stops(self) -> None:
        for stage, outputs in self.INVALID.items():
            for output in outputs:
                with self.subTest(stage=stage, output=output):
                    result = next_step(stage, output)
                    self.assertEqual(result.step, STOP)
                    self.assertTrue(result.reasons)

    def test_non_object_output_stops(self) -> None:
        for output in (None, [], "ok", 1):
            with self.subTest(output=output):
                self.assertEqual(next_step("implementation", output).step, STOP)

    def test_unknown_stage_stops(self) -> None:
        for stage in ("deploy", "../review/schema", "", None):
            with self.subTest(stage=stage):
                self.assertEqual(next_step(stage, IMPLEMENTATION).step, STOP)  # type: ignore[arg-type]

    def test_unreadable_schema_stops(self) -> None:
        with mock.patch.object(route, "SCHEMAS", Path("/nonexistent")):
            self.assertEqual(next_step("implementation", IMPLEMENTATION).step, STOP)

    def test_schema_with_an_unsupported_keyword_stops(self) -> None:
        with mock.patch.object(route, "load_schema", return_value={"type": "object", "pattern": "x"}):
            self.assertEqual(next_step("implementation", IMPLEMENTATION).step, STOP)

    def test_schema_with_a_broken_ref_stops(self) -> None:
        with mock.patch.object(route, "load_schema", return_value={"$ref": "#/$defs/missing"}):
            self.assertEqual(next_step("implementation", IMPLEMENTATION).step, STOP)


if __name__ == "__main__":
    unittest.main()
