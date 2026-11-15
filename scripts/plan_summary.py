#!/usr/bin/env python3
"""Turn a Terraform plan into a comment that is safe to show in a public repository (CHB-13).

The repository is public. A plan holds attribute values, and the state holds more: the alert
address and the billing account ID, for example (ADR-3c1f, Consequences). This script reads the
JSON of a plan (`terraform show -json <planfile>`) and keeps two things: the counts of resources
to add, change and destroy, and the address and action of each changed resource. It never reads
an attribute value, and it prints and writes nothing else. Subcommands, all used by
`.github/workflows/terraform-plan.yml`:

    summary PLAN_JSON BODY_FILE --sha SHA --run-url URL   the PR comment of a plan
    failure BODY_FILE --sha SHA --run-url URL             the PR comment of a failed plan
    errors LOG_FILE                                       print only the `Error:` lines of a log
    state-vars STATE_JSON ENV_FILE                        the two required variables, read from the
                                                          state, as `NAME=value` lines; each value
                                                          is masked in the job log first

`state-vars` exists because the plan needs `billing_account` and `alert_email`, and neither is
in the repository. The state holds both, and the planner may read the state. A change of either
value in a pull request is therefore not planned: the plan compares against the applied values.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

MARKER = "<!-- chb-terraform-plan -->"
MAX_LISTED = 100
MAX_ERROR_LINE = 400
LISTED_ACTIONS = ("create", "update", "delete", "replace")
ADDRESS_FORBIDDEN = re.compile(r'[^A-Za-z0-9_.\[\]"/:@=-]')
ERROR_LINE = re.compile(r"^[│╷╵\s]*(Error: .*)$")
STATE_VARIABLES = (
    ("TF_VAR_billing_account", "google_billing_budget.monthly", ("billing_account",)),
    ("TF_VAR_alert_email", "google_monitoring_notification_channel.email", ("labels", "email_address")),
)


class StateError(Exception):
    """The state lacks a resource or an attribute that the plan needs as a variable."""


def action_of(actions: list[str]) -> str:
    """One word for a resource change: replace for delete plus create, else the action itself."""
    if "delete" in actions and "create" in actions:
        return "replace"
    return "+".join(actions)


def clean_address(address: str) -> str:
    return ADDRESS_FORBIDDEN.sub("?", address)


def changes(plan: dict[str, Any]) -> list[tuple[str, str]]:
    """Address and action of each resource that the plan touches. No-op and read are left out."""
    found = []
    for item in plan.get("resource_changes") or []:
        action = action_of(list((item.get("change") or {}).get("actions") or []))
        if action in ("no-op", "read", ""):
            continue
        found.append((clean_address(str(item.get("address", "?"))), action))
    return found


def counts(found: list[tuple[str, str]]) -> tuple[int, int, int]:
    """Add, change, destroy as Terraform counts them: a replace is one add and one destroy."""
    tally = Counter(action for _, action in found)
    add = tally["create"] + tally["replace"]
    destroy = tally["delete"] + tally["replace"]
    return add, tally["update"], destroy


def summarize(plan: dict[str, Any], *, sha: str, run_url: str) -> str:
    found = changes(plan)
    add, change, destroy = counts(found)
    lines = [
        MARKER,
        "## Terraform plan: `infra/gcp`",
        "",
        f"**{add} to add, {change} to change, {destroy} to destroy**",
        "",
    ]
    if found:
        lines += ["| Resource | Action |", "|---|---|"]
        lines += [f"| `{address}` | {action} |" for address, action in found[:MAX_LISTED]]
        if len(found) > MAX_LISTED:
            lines += ["", f"{len(found) - MAX_LISTED} more not listed."]
        if destroy:
            lines += ["", "A destroy or replace is almost always a branch that is behind `main`. Check before merging."]
    else:
        lines += ["No changes. The infrastructure matches this branch."]
    lines += [
        "",
        f"Commit `{sha[:7]}`, [run]({run_url}). Read-only plan (`-lock=false`): nothing is applied.",
        "Attribute values are not shown: this repository is public (ADR-3c1f).",
    ]
    return "\n".join(lines)


def failure_comment(*, sha: str, run_url: str) -> str:
    return "\n".join(
        [
            MARKER,
            "## Terraform plan: `infra/gcp`",
            "",
            "**The plan failed.** The error lines are in the job log.",
            "",
            f"Commit `{sha[:7]}`, [run]({run_url}). Nothing was applied.",
        ]
    )


def error_lines(log: str) -> list[str]:
    """Only the `Error:` lines of a Terraform log. Plan text and resource values never match."""
    found = []
    for line in log.splitlines():
        match = ERROR_LINE.match(line)
        if match:
            found.append(match.group(1)[:MAX_ERROR_LINE])
    return found


def resource_values(state: dict[str, Any], address: str) -> dict[str, Any] | None:
    root = (state.get("values") or {}).get("root_module") or {}
    for resource in root.get("resources") or []:
        if resource.get("address") == address:
            return resource.get("values") or {}
    return None


def state_variables(state: dict[str, Any]) -> dict[str, str]:
    found = {}
    for name, address, path in STATE_VARIABLES:
        node: Any = resource_values(state, address)
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, str) or not node:
            raise StateError(f"the state has no {'.'.join(path)} on {address}")
        found[name] = node
    return found


def read_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_body(path: str, body: str) -> None:
    Path(path).write_text(body + "\n", encoding="utf-8")
    print(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("summary", "failure"):
        command = sub.add_parser(name)
        if name == "summary":
            command.add_argument("plan_json")
        command.add_argument("body_file")
        command.add_argument("--sha", required=True)
        command.add_argument("--run-url", required=True)
    sub.add_parser("errors").add_argument("log_file")
    state = sub.add_parser("state-vars")
    state.add_argument("state_json")
    state.add_argument("env_file")
    args = parser.parse_args(argv)

    if args.command == "errors":
        for line in error_lines(Path(args.log_file).read_text(encoding="utf-8", errors="replace")):
            print(line)
        return 0
    if args.command == "failure":
        write_body(args.body_file, failure_comment(sha=args.sha, run_url=args.run_url))
        return 0
    try:
        data = read_json(args.plan_json if args.command == "summary" else args.state_json)
    except (OSError, ValueError):
        print(f"::error::{args.command}: the JSON file cannot be read. Nothing is printed from it.")
        return 1
    if args.command == "summary":
        write_body(args.body_file, summarize(data, sha=args.sha, run_url=args.run_url))
        return 0
    try:
        variables = state_variables(data)
    except StateError as error:
        print(f"::error::{error}")
        return 1
    if any(re.search(r"[\r\n]", value) for value in variables.values()):
        print("::error::a state value holds a line break and cannot go in an env file")
        return 1
    for value in variables.values():
        print(f"::add-mask::{value}")
    Path(args.env_file).write_text("".join(f"{k}={v}\n" for k, v in variables.items()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
