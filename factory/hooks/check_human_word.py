#!/usr/bin/env python3
"""Claude Code Stop hook: call the person "the human", never "the owner" (CHB-194).

On 2026-11-13 the human asked to be called "the human" everywhere: in the chat, Jira, docs and
commit messages. A promise in the chat did not hold, so this hook checks every final reply.

The hook reads the text of the agent's final reply outside fenced code blocks (``` or ~~~) and
inline code, and names each line that uses "owner", "owners", "owner's" or "owners'" as a word.
Technical terms pass: `CODEOWNERS` (one word), "repository owner", "repo owner", "organization
owner", "org owner", `owner/repo`, "root-owned", and anything in code. To quote the old word,
put it in backticks.

It never loops. Like check_wip_minimums.py and check_untriaged_reviews.py it uses stop_refusal.py:
it remembers the findings it refused for, per session, in `<temp dir>/chb-human-word-<uid>/<session id>.json`
(CHB_HUMAN_WORD_STATE_DIR overrides the folder). A second stop with the same findings passes with one line.

It fails open: bad input, an unusable state folder or any unexpected error prints at most one line
to stderr and exits 0. A broken hook must never trap a session.

Contract (https://code.claude.com/docs/en/hooks, Stop): the event arrives as JSON on stdin with
`session_id` and `last_assistant_message`. The docs say hooks "that need the final assistant text of
the current turn should use `last_assistant_message` on Stop", because `transcript_path` "may not yet
include the current turn's most recent messages". Exit code 2 blocks the stop and feeds stderr to the agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import sys
import tempfile
from typing import TextIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python3 -I does not add it

from stop_refusal import already_refused  # noqa: E402

STATE_DIR_ENV = "CHB_HUMAN_WORD_STATE_DIR"
FENCE = re.compile(r"^[ \t]*(```|~~~)")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
WORD = re.compile(r"(?<![\w.-])owners?(?:'s?|’s?)?(?![\w/-])", re.IGNORECASE)
TECHNICAL_BEFORE = re.compile(r"\b(?:repository|repo|organization|organisation|org)\s+$", re.IGNORECASE)


def _prose_lines(message: str) -> list[str]:
    """The lines of the message outside fenced code blocks."""
    lines: list[str] = []
    fence: str | None = None
    for line in message.splitlines():
        opening = FENCE.match(line)
        if fence is None:
            if opening:
                fence = opening.group(1)
                continue
            lines.append(line)
        elif opening and opening.group(1) == fence:
            fence = None
    return lines


def _calls_the_person_owner(line: str) -> bool:
    prose = INLINE_CODE.sub(" ", line)
    return any(not TECHNICAL_BEFORE.search(prose[: match.start()]) for match in WORD.finditer(prose))


def find_problems(message: str) -> list[str]:
    """Each line of prose (stripped) that calls the person "owner"."""
    return [line.strip() for line in _prose_lines(message) if _calls_the_person_owner(line)]


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV) or posixpath.join(tempfile.gettempdir(), f"chb-human-word-{os.getuid()}")


def main(stdin: TextIO = sys.stdin, state_folder: str | None = None) -> int:
    try:
        event = json.load(stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    message = event.get("last_assistant_message")
    session_id = event.get("session_id")
    session_id = session_id if isinstance(session_id, str) else ""
    try:
        problems = find_problems(message) if isinstance(message, str) else []
        fingerprint = hashlib.sha256("\n".join(problems).encode()).hexdigest() if problems else None
        repeated = already_refused(state_folder if state_folder is not None else state_dir(), session_id, fingerprint)
    except OSError as error:
        print(f"Human word check skipped: state folder unusable ({type(error).__name__})", file=sys.stderr)
        return 0
    except BaseException as error:  # noqa: BLE001 - fail open on anything
        print(f"Human word check skipped: {type(error).__name__}", file=sys.stderr)
        return 0
    if not problems:
        return 0
    if repeated:
        print("Human word check: same findings as the last refusal, allowing the stop.", file=sys.stderr)
        return 0
    listed = "\n".join(f"- {line}" for line in problems)
    print(
        'Your reply calls the person "owner". Call them "the human" (CHB-194):\n'
        f"{listed}\n"
        'Rewrite each line with "the human" (for example "the human\'s approval", "Human: react"). '
        "Technical terms pass: CODEOWNERS, repository owner, owner/repo. To quote the old word, put it in backticks.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
