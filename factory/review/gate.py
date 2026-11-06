#!/usr/bin/env python3
"""Review gate (ADR-ea8c, rubric CHB-40). Standard library only.

Reads the JSON that `claude -p --output-format json --json-schema ...` printed, validates
`structured_output` against schema.json, recomputes every dimension score from the findings,
and decides. Scores claimed by the model are displayed, never used.

Pass iff the output is valid, Merge Safety >= 8.5 and no finding is critical.
Anything else fails: empty or malformed output, an error result, a schema violation,
a non-zero exit of claude.

Usage (from .github/workflows/review.yml):
  gate.py --result result.json --claude-exit-code N --schema schema.json
          --changed-files names.z --prior-runs runs.json --app-slug SLUG
          --head-sha SHA --out check-run.json
Writes the check run payload to --out. Exit code: 0 pass, 1 fail, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

WEIGHTS = {"codeQuality": 30, "security": 25, "businessAlignment": 15, "tests": 15, "architecture": 15}
LABELS = {
    "codeQuality": "Code quality",
    "security": "Security",
    "businessAlignment": "Business alignment",
    "tests": "Tests",
    "architecture": "Architecture",
}
# CHB-40 gives ranges (critical -2 to -3, improvement -1 to -2, cleanup -0.5).
# The gate takes the upper bound of each range so the model has no say in the size.
DEDUCTIONS = {"critical": Fraction(3), "improvement": Fraction(2), "cleanup": Fraction(1, 2)}
START = Fraction(10)
CRITICAL_CAP = Fraction(7)
THRESHOLD = Fraction(17, 2)
MAX_ROUNDS = 3
CHECK_NAME = "review"
SUMMARY_LIMIT = 60000  # GitHub caps a check run summary at 65535 characters.

ANNOTATIONS = {"$schema", "title", "description", "$defs"}
SUPPORTED_KEYWORDS = ANNOTATIONS | {
    "$ref",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "maxItems",
}

HARNESS_PREFIXES = (".github/", ".claude/", "factory/", "infra/", "scripts/", "docs/decisions/", "docs/knowledge/")
HARNESS_FILES = {"Makefile", "CLAUDE.md"}
SUPPLY_CHAIN_NAMES = {
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
    ".npmrc",
    ".nvmrc",
    "Dockerfile",
    "compose.yaml",
}
DOCS_PREFIXES = ("docs/specs/", "docs/postmortems/", "docs/sessions/")
DOCS_FILES = {"README.md"}
TEST_FILE = re.compile(r"\.(test|spec)\.[^/]+$")


class GateError(Exception):
    """The reviewer output cannot be trusted."""


@dataclass
class Verdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    merge_safety: Fraction | None = None
    scores: dict[str, Fraction] = field(default_factory=dict)
    claimed: dict[str, object] = field(default_factory=dict)
    review: dict[str, object] | None = None


# --- parsing and validation -------------------------------------------------------------


def _reject_constant(name: str) -> object:
    raise GateError(f"{name} is not valid JSON")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateError(f"duplicate key {key!r}")
        result[key] = value
    return result


def parse_json(text: str) -> object:
    """Strict JSON: no NaN or Infinity, no duplicate keys."""
    try:
        return json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as error:
        raise GateError(f"output is not valid JSON: {error}") from None


def _is_type(value: object, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    return False


def _resolve(ref: str, root: Mapping[str, object]) -> Mapping[str, object]:
    if not ref.startswith("#/"):
        raise GateError(f"unsupported $ref {ref}")
    node: object = root
    for part in ref[2:].split("/"):
        if not isinstance(node, Mapping) or part not in node:
            raise GateError(f"unresolved $ref {ref}")
        node = node[part]
    if not isinstance(node, Mapping):
        raise GateError(f"unresolved $ref {ref}")
    return node


def validate(instance: object, schema: Mapping[str, object], root: Mapping[str, object] | None = None, path: str = "$") -> list[str]:
    """Validate the subset of JSON Schema in SUPPORTED_KEYWORDS. Any other keyword is an error."""
    root = schema if root is None else root
    errors = [f"{path}: unsupported schema keyword {k}" for k in schema if k not in SUPPORTED_KEYWORDS]
    if errors:
        return errors
    if "$ref" in schema:
        return validate(instance, _resolve(str(schema["$ref"]), root), root, path)

    types = schema.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_is_type(instance, str(n)) for n in names):
            return [f"{path}: expected {' or '.join(map(str, names))}"]
    if "enum" in schema and not any(_same(instance, v) for v in schema["enum"]):  # type: ignore[union-attr]
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")

    if _is_type(instance, "number"):
        if "minimum" in schema and instance < schema["minimum"]:  # type: ignore[operator]
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:  # type: ignore[operator]
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:  # type: ignore[operator]
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:  # type: ignore[operator]
            errors.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(instance, list):
        if "maxItems" in schema and len(instance) > schema["maxItems"]:  # type: ignore[operator]
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += validate(item, schema["items"], root, f"{path}[{i}]")  # type: ignore[arg-type]
    if isinstance(instance, dict):
        properties: Mapping[str, Mapping[str, object]] = schema.get("properties", {})  # type: ignore[assignment]
        for name in schema.get("required", []):  # type: ignore[union-attr]
            if name not in instance:
                errors.append(f"{path}.{name}: required")
        for name, value in instance.items():
            if name in properties:
                errors += validate(value, properties[name], root, f"{path}.{name}")
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}.{name}: field not allowed")
    return errors


def _same(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b


# --- scoring ----------------------------------------------------------------------------


def dimension_scores(review: Mapping[str, object]) -> dict[str, Fraction]:
    """Score per dimension from its findings alone. Claimed scores are ignored."""
    dimensions: Mapping[str, Mapping[str, object]] = review["dimensions"]  # type: ignore[assignment]
    scores: dict[str, Fraction] = {}
    for name in WEIGHTS:
        findings: list[Mapping[str, object]] = dimensions[name]["findings"]  # type: ignore[assignment]
        severities = [str(f["severity"]) for f in findings]
        score = max(Fraction(0), START - sum((DEDUCTIONS[s] for s in severities), Fraction(0)))
        if "critical" in severities:
            score = min(score, CRITICAL_CAP)
        scores[name] = score
    return scores


def merge_safety(scores: Mapping[str, Fraction | int]) -> Fraction:
    total = sum(WEIGHTS.values())
    return sum((Fraction(scores[n]) * w for n, w in WEIGHTS.items()), Fraction(0)) / total


def _findings(review: Mapping[str, object]) -> Iterable[tuple[str, Mapping[str, object]]]:
    dimensions: Mapping[str, Mapping[str, object]] = review["dimensions"]  # type: ignore[assignment]
    for name in WEIGHTS:
        for finding in dimensions[name]["findings"]:  # type: ignore[union-attr]
            yield name, finding


def structured_output(envelope_text: str) -> object:
    envelope = parse_json(envelope_text)
    if not isinstance(envelope, dict):
        raise GateError("claude output is not a JSON object")
    if envelope.get("is_error") is not False or envelope.get("subtype") != "success":
        raise GateError(f"claude run ended with {envelope.get('subtype', 'no result')!r}")
    if "structured_output" not in envelope:
        raise GateError("claude run produced no structured output")
    return envelope["structured_output"]


def evaluate(envelope_text: str, schema: Mapping[str, object], claude_exit_code: int = 0) -> Verdict:
    if claude_exit_code != 0:
        return Verdict(False, [f"claude exited with code {claude_exit_code}"])
    if not envelope_text.strip():
        return Verdict(False, ["claude produced no output"])
    try:
        review = structured_output(envelope_text)
    except GateError as error:
        return Verdict(False, [str(error)])
    errors = validate(review, schema)
    if errors:
        return Verdict(False, ["schema: " + e for e in errors[:50]])
    assert isinstance(review, dict)  # noqa: S101 - the schema requires an object

    scores = dimension_scores(review)
    safety = merge_safety(scores)
    claimed = {n: review["dimensions"][n]["score"] for n in WEIGHTS}
    reasons = []
    criticals = sum(1 for _, f in _findings(review) if f["severity"] == "critical")
    if criticals:
        reasons.append(f"{criticals} critical finding(s)")
    if safety < THRESHOLD:
        reasons.append(f"Merge Safety {_fmt(safety)} is below {_fmt(THRESHOLD)}")
    return Verdict(not reasons, reasons, safety, scores, claimed, review)


# --- risk class and rounds --------------------------------------------------------------


def parse_name_list(text: str) -> list[str]:
    """Paths from `git diff -z --no-renames --name-only`."""
    return [p for p in text.split("\0") if p]


def _path_class(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if path.startswith(HARNESS_PREFIXES) or path in HARNESS_FILES:
        return "harness"
    if name in SUPPLY_CHAIN_NAMES:
        return "supply-chain"
    if path.startswith(DOCS_PREFIXES) or path in DOCS_FILES:
        return "docs"
    if path.startswith("e2e/") or "/tests/" in f"/{path}" or TEST_FILE.search(name):
        return "tests"
    return "feature"


def risk_class(paths: Sequence[str]) -> str:
    """ADR-ea8c risk class of a change. "tests-only": every non-docs file is a test."""
    if not paths:
        return "harness"
    classes = {_path_class(p) for p in paths}
    for strictest in ("harness", "supply-chain", "feature"):
        if strictest in classes:
            return strictest
    return "tests-only" if "tests" in classes else "docs"


def review_round(prior_runs: Iterable[Mapping[str, object]], app_slug: str) -> int:
    """This run's round: earlier `review` check runs by the reviewer App, plus one."""
    def own(run: object) -> bool:
        if not isinstance(run, Mapping) or run.get("name") != CHECK_NAME:
            return False
        app = run.get("app")
        return isinstance(app, Mapping) and app.get("slug") == app_slug

    return sum(1 for r in prior_runs if own(r)) + 1


# --- rendering --------------------------------------------------------------------------


def _fmt(value: Fraction | int) -> str:
    """Two decimals, rounded down, so a score below the threshold never prints as the threshold."""
    hundredths = math.floor(Fraction(value) * 100)
    return f"{hundredths // 100}.{hundredths % 100:02d}"


def _plain(text: object, limit: int = 1000) -> str:
    """Untrusted text as one escaped Markdown line: no mentions, no HTML, no links."""
    flat = " ".join(str(text).split())[:limit]
    flat = re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", flat)
    return flat.replace("@", "@​")


def render_summary(verdict: Verdict, risk: str, round_number: int) -> str:
    lines = [f"**{'Pass' if verdict.passed else 'Fail'}** (shadow mode: this check is not required)", ""]
    if verdict.merge_safety is not None:
        lines.append(f"Merge Safety: **{_fmt(verdict.merge_safety)}** (pass at {_fmt(THRESHOLD)} with no critical finding)")
    round_note = " (over the cap: needs a human)" if round_number > MAX_ROUNDS else ""
    lines += [f"Risk class: `{risk}`", f"Round: {round_number} of {MAX_ROUNDS}{round_note}", ""]
    if verdict.reasons:
        lines += ["Reasons:", ""] + [f"- {_plain(r, 300)}" for r in verdict.reasons] + [""]
    if verdict.review is not None:
        lines += ["| Dimension | Weight | Score | Claimed |", "|---|---|---|---|"]
        for name, weight in WEIGHTS.items():
            lines.append(f"| {LABELS[name]} | {weight}% | {_fmt(verdict.scores[name])} | {_plain(verdict.claimed[name], 10)} |")
        lines += ["", f"Summary: {_plain(verdict.review['summary'], 2000)}", ""]
        for name, finding in _findings(verdict.review):
            where = _plain(finding["file"], 300) + (f":{finding['line']}" if finding["line"] is not None else "")
            lines.append(
                f"- **{finding['severity']}** ({LABELS[name]}) {where}: {_plain(finding['message'])}"
                f" Fix: {_plain(finding['suggestedFix'])}"
            )
    text = "\n".join(lines)
    if len(text) > SUMMARY_LIMIT:
        text = text[: SUMMARY_LIMIT - 40] + "\n\n(truncated)"
    return text


def check_run_payload(verdict: Verdict, head_sha: str, summary: str) -> dict[str, object]:
    title = "Pass" if verdict.passed else "Fail"
    if verdict.merge_safety is not None:
        title += f": Merge Safety {_fmt(verdict.merge_safety)}"
    return {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success" if verdict.passed else "failure",
        "output": {"title": title, "summary": summary},
    }


# --- command line -----------------------------------------------------------------------


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("--result", "--schema", "--changed-files", "--prior-runs", "--app-slug", "--head-sha", "--out"):
        parser.add_argument(name, required=True)
    parser.add_argument("--claude-exit-code", type=int, required=True)
    args = parser.parse_args(argv)

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    verdict = evaluate(_read(args.result), schema, args.claude_exit_code)
    risk = risk_class(parse_name_list(_read(args.changed_files)))
    try:
        runs = parse_json(_read(args.prior_runs))
    except GateError:
        runs = []
    round_number = review_round(runs if isinstance(runs, list) else [], args.app_slug)

    payload = check_run_payload(verdict, args.head_sha, render_summary(verdict, risk, round_number))
    Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
    print(f"{payload['conclusion']}: risk {risk}, round {round_number}; " + "; ".join(verdict.reasons))
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
