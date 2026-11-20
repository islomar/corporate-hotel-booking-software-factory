#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny an agent launch whose label does not start with its Jira key (CHB-190).

The owner watches several agents at once and sees only each one's short label. A label that does
not start with the ticket key forces a question. A prose rule to put the key first gets forgotten.

Denied: an Agent tool call whose `description` does not start with `CHB-<n>:` or `no-ticket:` followed by text.
The match is case-sensitive and anchored at the first character: `Implement CHB-23`, an empty label
`chb-23:` and a bare `CHB-23:` are denied.

Unlike the other PreToolUse hooks, this one fails open: input that is not JSON, a `tool_input` that is
not an object, a missing or non-string `description`, or an error inside the matcher lets the launch run,
with a note on stderr for an error. A broken label check must never block every agent launch; a
wrong label costs a question, not damage.

Contract (https://code.claude.com/docs/en/hooks, section "Agent"): the tool call arrives as JSON on
stdin, with `tool_input` fields `prompt`, `description`, `subagent_type` and `model`.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal permission flow.
"""

from __future__ import annotations

import json
import re
import sys

LABEL = re.compile(r"^(?:CHB-[0-9]+|no-ticket):\s*\S")


def labelled(description: str) -> bool:
    """True when the label starts with `CHB-<n>:` or `no-ticket:` and has text after it."""
    return bool(LABEL.match(description))


def reason(description: str) -> str:
    return (
        f"Blocked: the agent label {json.dumps(description)} does not start with the Jira key (CHB-190). "
        "Start the `description` with `CHB-<n>:` for ticket work, as `CHB-23: request limits`, "
        "or with `no-ticket:` for work without a ticket, as `no-ticket: read a page`. Name the task after the colon."
    )


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail open: a broken label check must not block every agent launch
        print(f"The agent label hook failed ({type(error).__name__}); the launch goes ahead.", file=sys.stderr)
        return 0


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        return 0
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    description = tool_input.get("description") if isinstance(tool_input, dict) else None
    if not isinstance(description, str) or labelled(description):
        return 0
    print(reason(description), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
