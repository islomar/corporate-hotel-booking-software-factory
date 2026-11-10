#!/usr/bin/env python3
"""Routing for the ticket-to-PR workflow (CHB-62). Standard library only.

next_step(stage, output) validates a stage's typed output against schemas/<stage>.json
and returns the next step. No workflow calls it yet: CHB-44 will.

Rules:
- analysis: blockingQuestions non-empty -> STOP_AND_NOTIFY.
- implementation: typesClean, lintClean or testsGreen false -> ANOTHER_ROUND.
- review: mergeSafety below 8.5 or any critical finding -> TRIAGE (ADR-ea8c threshold).
- triage: decision doubt -> PR_CAVEATS.
- otherwise -> NEXT_STAGE.
An unknown stage, invalid output or unreadable schema -> STOP. next_step never raises.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))

from gate import THRESHOLD, validate  # noqa: E402  (the validator and threshold of ADR-ea8c)

SCHEMAS = Path(__file__).resolve().parent / "schemas"
STAGES = ("analysis", "implementation", "review", "triage")
IMPLEMENTATION_CHECKS = ("typesClean", "lintClean", "testsGreen")

STOP = "stop"
STOP_AND_NOTIFY = "stop-and-notify"
ANOTHER_ROUND = "another-round"
TRIAGE = "triage"
PR_CAVEATS = "pr-caveats"
NEXT_STAGE = "next-stage"


@dataclass(frozen=True)
class Route:
    step: str
    reasons: tuple[str, ...] = ()


def load_schema(stage: str) -> Mapping[str, object]:
    schema = json.loads((SCHEMAS / f"{stage}.json").read_text(encoding="utf-8"))
    if not isinstance(schema, Mapping):
        raise ValueError(f"schema for {stage} is not an object")
    return schema


def next_step(stage: str, output: object) -> Route:
    """The step after `stage` produced `output`. Fails closed: any doubt about the input is STOP."""
    if stage not in STAGES:
        return Route(STOP, (f"unknown stage {stage!r}",))
    try:
        errors = validate(output, load_schema(stage))
        if errors:
            return Route(STOP, tuple(errors))
        assert isinstance(output, Mapping)
        return _route(stage, output)
    except Exception as error:  # noqa: BLE001  (fail closed on anything unexpected)
        return Route(STOP, (f"{stage}: {type(error).__name__}: {error}",))


def _route(stage: str, output: Mapping[str, object]) -> Route:
    if stage == "analysis":
        questions = tuple(str(q) for q in output["blockingQuestions"])  # type: ignore[union-attr]
        return Route(STOP_AND_NOTIFY, questions) if questions else Route(NEXT_STAGE)
    if stage == "implementation":
        failed = tuple(f"{check} is false" for check in IMPLEMENTATION_CHECKS if output[check] is not True)
        return Route(ANOTHER_ROUND, failed) if failed else Route(NEXT_STAGE)
    if stage == "review":
        reasons: list[str] = []
        merge_safety = output["mergeSafety"]
        if merge_safety < THRESHOLD:  # type: ignore[operator]
            reasons.append(f"mergeSafety {merge_safety} is below {float(THRESHOLD)}")
        criticals = sum(1 for f in output["findings"] if f["severity"] == "critical")  # type: ignore[union-attr,index]
        if criticals:
            reasons.append(f"{criticals} critical finding{'s' if criticals > 1 else ''}")
        return Route(TRIAGE, tuple(reasons)) if reasons else Route(NEXT_STAGE)
    if output["decision"] == "doubt":
        return Route(PR_CAVEATS)
    return Route(NEXT_STAGE)
