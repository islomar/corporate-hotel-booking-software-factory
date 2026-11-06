import copy
import json
import sys
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))

from gate import (
    MAX_ROUNDS,
    SUPPORTED_KEYWORDS,
    GateError,
    check_run_payload,
    dimension_scores,
    evaluate,
    merge_safety,
    parse_json,
    parse_name_list,
    render_summary,
    review_round,
    risk_class,
    validate,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review"
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "review" / "schema.json").read_text())


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _envelope(review: object, **overrides: object) -> str:
    result = {"type": "result", "subtype": "success", "is_error": False, "structured_output": review}
    return json.dumps(result | overrides)


def _finding(severity: str) -> dict:
    return {"severity": severity, "file": "a.py", "line": 1, "message": "m", "suggestedFix": "f"}


def _clean_review() -> dict:
    names = ("codeQuality", "security", "businessAlignment", "tests", "architecture")
    return {"summary": "s", "dimensions": {n: {"score": 10, "findings": []} for n in names}}


class EvaluateTest(unittest.TestCase):
    def test_valid_review_passes_with_recomputed_merge_safety(self) -> None:
        verdict = evaluate(_envelope(_fixture("valid.json")), SCHEMA)

        self.assertTrue(verdict.passed, verdict.reasons)
        self.assertEqual(verdict.merge_safety, Fraction("9.325"))

    def test_claimed_scores_are_ignored(self) -> None:
        verdict = evaluate(_envelope(_fixture("claims-high-scores.json")), SCHEMA)

        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.merge_safety, Fraction("7.8"))

    def test_a_critical_finding_fails_even_above_the_threshold(self) -> None:
        verdict = evaluate(_envelope(_fixture("critical.json")), SCHEMA)

        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.merge_safety, Fraction("9.25"))
        self.assertTrue(any("critical" in r for r in verdict.reasons))

    def test_schema_violations_fail(self) -> None:
        verdict = evaluate(_envelope(_fixture("invalid.json")), SCHEMA)

        self.assertFalse(verdict.passed)
        self.assertIsNone(verdict.merge_safety)
        self.assertTrue(any("architecture" in r for r in verdict.reasons))
        self.assertTrue(any("major" in r for r in verdict.reasons))

    def test_injected_fields_fail_instead_of_being_dropped(self) -> None:
        verdict = evaluate(_envelope(_fixture("injected-fields.json")), SCHEMA)

        self.assertFalse(verdict.passed)
        self.assertTrue(any("mergeSafety" in r for r in verdict.reasons))
        self.assertTrue(any("approved" in r for r in verdict.reasons))

    def test_malformed_json_fails(self) -> None:
        verdict = evaluate((FIXTURES / "malformed.json").read_text(), SCHEMA)

        self.assertFalse(verdict.passed)
        self.assertIsNone(verdict.merge_safety)

    def test_empty_output_fails(self) -> None:
        self.assertFalse(evaluate("", SCHEMA).passed)

    def test_a_non_zero_claude_exit_fails(self) -> None:
        verdict = evaluate(_envelope(_fixture("valid.json")), SCHEMA, claude_exit_code=1)

        self.assertFalse(verdict.passed)

    def test_an_error_result_fails(self) -> None:
        envelope = _envelope(_fixture("valid.json"), subtype="error_max_budget_usd", is_error=True)

        self.assertFalse(evaluate(envelope, SCHEMA).passed)

    def test_an_error_flag_with_subtype_success_fails(self) -> None:
        # Claude Code 2.1.318 without credentials prints subtype "success" with is_error true.
        envelope = _envelope(_fixture("valid.json"), is_error=True, result="Not logged in")

        self.assertFalse(evaluate(envelope, SCHEMA).passed)

    def test_a_result_without_structured_output_fails(self) -> None:
        envelope = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "I refuse."})

        self.assertFalse(evaluate(envelope, SCHEMA).passed)

    def test_exactly_the_threshold_passes_and_just_below_fails(self) -> None:
        review = _clean_review()
        cq = review["dimensions"]["codeQuality"]["findings"]
        cq += [_finding("improvement"), _finding("improvement"), _finding("cleanup"), _finding("cleanup")]
        at_threshold = evaluate(_envelope(review), SCHEMA)

        cq.append(_finding("cleanup"))
        below = evaluate(_envelope(review), SCHEMA)

        self.assertEqual(at_threshold.merge_safety, Fraction("8.5"))
        self.assertTrue(at_threshold.passed, at_threshold.reasons)
        self.assertEqual(below.merge_safety, Fraction("8.35"))
        self.assertFalse(below.passed)


class ScoresTest(unittest.TestCase):
    def test_deductions_per_severity(self) -> None:
        review = _clean_review()
        dims = review["dimensions"]
        dims["codeQuality"]["findings"] = [_finding("improvement")]
        dims["tests"]["findings"] = [_finding("cleanup")]
        dims["security"]["findings"] = [_finding("critical")]

        scores = dimension_scores(review)

        self.assertEqual(scores["codeQuality"], 8)
        self.assertEqual(scores["tests"], Fraction("9.5"))
        self.assertEqual(scores["security"], 7)
        self.assertEqual(scores["architecture"], 10)

    def test_a_critical_finding_caps_the_dimension_at_7(self) -> None:
        review = _clean_review()
        review["dimensions"]["security"]["findings"] = [_finding("critical"), _finding("cleanup")]

        self.assertEqual(dimension_scores(review)["security"], Fraction("6.5"))

        review["dimensions"]["architecture"]["findings"] = [_finding("critical")]
        self.assertLessEqual(dimension_scores(review)["architecture"], 7)

    def test_scores_floor_at_zero(self) -> None:
        review = _clean_review()
        review["dimensions"]["tests"]["findings"] = [_finding("critical")] * 5

        self.assertEqual(dimension_scores(review)["tests"], 0)

    def test_merge_safety_is_the_weighted_mean(self) -> None:
        scores = {"codeQuality": 0, "security": 10, "businessAlignment": 10, "tests": 10, "architecture": 10}

        self.assertEqual(merge_safety(scores), 7)


class ParseJsonTest(unittest.TestCase):
    def test_rejects_nan_and_infinity(self) -> None:
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
            with self.subTest(text=text), self.assertRaises(GateError):
                parse_json(text)

    def test_rejects_duplicate_keys(self) -> None:
        with self.assertRaises(GateError):
            parse_json('{"score": 1, "score": 10}')

    def test_rejects_malformed_text(self) -> None:
        with self.assertRaises(GateError):
            parse_json("{")


class ValidateTest(unittest.TestCase):
    def test_valid_fixture_has_no_errors(self) -> None:
        self.assertEqual(validate(_fixture("valid.json"), SCHEMA), [])

    def test_booleans_are_not_numbers(self) -> None:
        review = _fixture("valid.json")
        review["dimensions"]["security"]["score"] = True
        finding = review["dimensions"]["codeQuality"]["findings"][0]
        finding["line"] = True

        errors = validate(review, SCHEMA)

        self.assertTrue(any("security.score" in e for e in errors), errors)
        self.assertTrue(any("line" in e for e in errors), errors)

    def test_line_must_be_a_positive_integer_or_null(self) -> None:
        review = _fixture("valid.json")
        finding = review["dimensions"]["codeQuality"]["findings"][0]
        for bad in (0, 1.5, "10"):
            finding["line"] = bad
            with self.subTest(line=bad):
                self.assertNotEqual(validate(review, SCHEMA), [])
        finding["line"] = None
        self.assertEqual(validate(review, SCHEMA), [])

    def test_length_and_count_limits(self) -> None:
        review = _fixture("valid.json")
        review["summary"] = "x" * 2001
        review["dimensions"]["tests"]["findings"] = [_finding("cleanup")] * 21

        errors = validate(review, SCHEMA)

        self.assertTrue(any("summary" in e for e in errors), errors)
        self.assertTrue(any("tests.findings" in e for e in errors), errors)

    def test_score_out_of_range(self) -> None:
        review = _fixture("valid.json")
        review["dimensions"]["tests"]["score"] = 11

        self.assertNotEqual(validate(review, SCHEMA), [])

    def test_schema_uses_only_supported_keywords(self) -> None:
        def keywords(node: object, in_properties: bool = False) -> set[str]:
            if isinstance(node, list):
                return set().union(*(keywords(n) for n in node)) if node else set()
            if not isinstance(node, dict):
                return set()
            found: set[str] = set()
            for key, value in node.items():
                if not in_properties:
                    found.add(key)
                children_are_names = not in_properties and key in ("properties", "$defs")
                if isinstance(value, (dict, list)) and key not in ("enum", "required"):
                    found |= keywords(value, in_properties=children_are_names)
            return found

        self.assertLessEqual(keywords(SCHEMA), SUPPORTED_KEYWORDS)

    def test_schema_declares_no_dialect(self) -> None:
        # claude --json-schema rejects "$schema": ".../draft/2020-12/schema" (Claude Code 2.1.318).
        self.assertNotIn("$schema", SCHEMA)

    def test_an_unsupported_keyword_is_an_error(self) -> None:
        errors = validate("abc", {"type": "string", "pattern": "^a"})

        self.assertTrue(any("pattern" in e for e in errors), errors)


class RiskClassTest(unittest.TestCase):
    def test_harness_paths(self) -> None:
        for path in (
            ".github/workflows/ci.yml",
            ".claude/settings.json",
            "factory/README.md",
            "factory/review/schema.json",
            "infra/gcp/budget.tf",
            "scripts/check_release_age.py",
            "Makefile",
            "CLAUDE.md",
            "docs/decisions/ADR-x.md",
            "docs/knowledge/x.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(risk_class([path, "backend/src/chb/app.py"]), "harness")

    def test_supply_chain_paths(self) -> None:
        for path in (
            "frontend/package.json",
            "e2e/package-lock.json",
            "backend/pyproject.toml",
            "backend/uv.lock",
            "frontend/.npmrc",
            "Dockerfile",
            "compose.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(risk_class([path, "frontend/src/App.tsx"]), "supply-chain")

    def test_harness_wins_over_supply_chain(self) -> None:
        self.assertEqual(risk_class(["Dockerfile", "Makefile"]), "harness")

    def test_docs_only(self) -> None:
        paths = ["docs/specs/booking.md", "docs/postmortems/CHB-1.md", "docs/sessions/s.md", "README.md"]

        self.assertEqual(risk_class(paths), "docs")

    def test_tests_only_means_every_non_docs_file_is_a_test(self) -> None:
        self.assertEqual(risk_class(["backend/tests/test_health.py", "e2e/tests/a.spec.ts"]), "tests-only")
        self.assertEqual(risk_class(["frontend/src/App.test.tsx", "docs/specs/a.md"]), "tests-only")

    def test_feature_with_tests_is_feature(self) -> None:
        paths = ["backend/src/chb/app.py", "backend/tests/test_health.py"]

        self.assertEqual(risk_class(paths), "feature")

    def test_nested_readme_is_not_docs(self) -> None:
        self.assertEqual(risk_class(["frontend/README.md"]), "feature")

    def test_no_paths_fails_closed(self) -> None:
        self.assertEqual(risk_class([]), "harness")


class ParseNameListTest(unittest.TestCase):
    def test_splits_on_nul(self) -> None:
        self.assertEqual(parse_name_list("a.py\0dir/b c.py\0"), ["a.py", "dir/b c.py"])

    def test_empty(self) -> None:
        self.assertEqual(parse_name_list(""), [])


class ReviewRoundTest(unittest.TestCase):
    def test_counts_only_the_reviewer_apps_review_runs(self) -> None:
        runs = [
            {"name": "review", "app": {"slug": "chb-factory-reviewer"}},
            {"name": "review", "app": {"slug": "chb-factory-reviewer"}},
            {"name": "review", "app": {"slug": "github-actions"}},
            {"name": "backend", "app": {"slug": "chb-factory-reviewer"}},
            {"name": "review"},
        ]

        self.assertEqual(review_round(runs, "chb-factory-reviewer"), 3)

    def test_first_round(self) -> None:
        self.assertEqual(review_round([], "chb-factory-reviewer"), 1)
        self.assertEqual(MAX_ROUNDS, 3)


class RenderSummaryTest(unittest.TestCase):
    def test_reports_verdict_scores_class_and_round(self) -> None:
        verdict = evaluate(_envelope(_fixture("valid.json")), SCHEMA)

        text = render_summary(verdict, risk="feature", round_number=2)

        self.assertIn("pass", text.lower())
        self.assertIn("9.32", text)  # rounded down: 9.325
        self.assertIn("feature", text)
        self.assertIn("2 of 3", text)
        self.assertIn("Function mixes parsing and I/O.", text)

    def test_scores_are_rounded_down(self) -> None:
        review = _clean_review()
        review["dimensions"]["codeQuality"]["findings"] = [_finding("cleanup")]
        review["dimensions"]["tests"]["findings"] = [_finding("cleanup")] * 3
        verdict = evaluate(_envelope(review), SCHEMA)

        self.assertEqual(verdict.merge_safety, Fraction("9.625"))
        self.assertIn("9.62", render_summary(verdict, risk="feature", round_number=1))

    def test_round_over_the_cap_is_flagged(self) -> None:
        verdict = evaluate(_envelope(_fixture("valid.json")), SCHEMA)

        self.assertIn("over the cap", render_summary(verdict, risk="feature", round_number=4))

    def test_mentions_are_escaped(self) -> None:
        review = _fixture("valid.json")
        review["summary"] = "ping @islomar and @org/team"
        verdict = evaluate(_envelope(review), SCHEMA)

        text = render_summary(verdict, risk="feature", round_number=1)

        self.assertNotIn("@islomar", text)
        self.assertNotIn("@org/team", text)

    def test_length_is_capped(self) -> None:
        review = _clean_review()
        long = copy.deepcopy(_finding("cleanup")) | {"message": "x" * 1000, "suggestedFix": "y" * 1000}
        for dim in review["dimensions"].values():
            dim["findings"] = [long] * 20
        verdict = evaluate(_envelope(review), SCHEMA)

        self.assertLessEqual(len(render_summary(verdict, risk="feature", round_number=1)), 60000)

    def test_failure_lists_reasons(self) -> None:
        verdict = evaluate("not json", SCHEMA)

        text = render_summary(verdict, risk="harness", round_number=1)

        self.assertIn("fail", text.lower())
        self.assertIn("not valid JSON", text)


class CheckRunPayloadTest(unittest.TestCase):
    def test_success_or_failure_only(self) -> None:
        passed = evaluate(_envelope(_fixture("valid.json")), SCHEMA)
        failed = evaluate("", SCHEMA)

        ok = check_run_payload(passed, head_sha="abc", summary="s")
        ko = check_run_payload(failed, head_sha="abc", summary="s")

        self.assertEqual(ok["conclusion"], "success")
        self.assertEqual(ko["conclusion"], "failure")
        self.assertEqual(ok["name"], "review")
        self.assertEqual(ok["head_sha"], "abc")
        self.assertEqual(ok["status"], "completed")
        self.assertEqual(ok["output"]["summary"], "s")


if __name__ == "__main__":
    unittest.main()
