#!/usr/bin/env python3
"""Claude Code Stop hook: commands for the owner to paste must work from any folder (CHB-126).

On 2026-11-07 the agent gave the owner `cp <backup> .git/config`. The owner ran it from another
repository's folder, and that repository's config was overwritten with this one's.

The hook reads the shell code blocks of the agent's final reply (fenced, with no language or
sh, bash, shell, zsh, console) and flags each command that depends on the current folder:
- `git` without `-C`, `make` without `-C`, a `cd` to a relative folder;
- a program called by a relative path (`factory/bin/jira`, `./run.sh`);
- a write command (cp, mv, rm, ln, tee, touch, mkdir, chmod, chown, truncate, install, rsync,
  rmdir, `sed -i`) or a `>` / `>>` redirect with a relative path.

Not checked: a command after `cd /absolute/folder` (in the same block), a line that starts with
`! ` (Claude Code runs it in the session folder), inline code, and blocks in another language.
A block that is not meant to be pasted (an example, a log) belongs in a ```text block.

Contract (https://code.claude.com/docs/en/hooks, Stop): the event arrives as JSON on stdin with
`last_assistant_message` and `stop_hook_active`. Printing {"decision": "block", "reason": ...}
sends the agent back to fix the reply. The hook blocks once per reply: when stop_hook_active is
true it lets the stop through, so it never loops. Bad input never blocks.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from typing import TextIO

SHELL_LANGUAGES = {"", "sh", "bash", "shell", "zsh", "console"}
FENCE = re.compile(r"^```([\w+-]*)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
SEGMENT = re.compile(r"&&|\|\||;|\|")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT = re.compile(r"^\d*>>?(?!&)(.*)$")
WRITERS = {"cp", "mv", "rm", "ln", "tee", "touch", "mkdir", "chmod", "chown", "truncate", "install", "rsync", "rmdir"}
PREFIXES = {"sudo", "env", "command", "exec", "time", "nohup"}


def find_problems(message: str) -> list[str]:
    """The commands in the message's shell blocks that depend on the current folder."""
    problems: list[str] = []
    for language, body in FENCE.findall(message):
        if language.lower() not in SHELL_LANGUAGES:
            continue
        anchored = False
        for line in _logical_lines(body):
            flagged, anchored = _check_line(line, anchored)
            if flagged:
                problems.append(line)
    return problems


def _logical_lines(body: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in body.splitlines():
        part = raw.strip()
        if part.startswith("$ "):
            part = part[2:]
        if part.endswith("\\"):
            current += part[:-1].rstrip() + " "
            continue
        current += part
        if current.strip():
            lines.append(current.strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    return lines


def _check_line(line: str, anchored: bool) -> tuple[bool, bool]:
    """Return (line is a problem, block is anchored after this line)."""
    if line.startswith("#") or line.startswith("! "):
        return False, anchored
    flagged = False
    for segment in SEGMENT.split(line):
        words = _words(segment)
        if not words:
            continue
        if words[0] == "cd":
            target = words[1] if len(words) > 1 else "~"
            if _absolute(target):
                anchored = True
            elif not anchored:
                flagged = True
            continue
        if not anchored and _depends_on_folder(words):
            flagged = True
    return flagged, anchored


def _words(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, comments=True)
    except ValueError:
        words = segment.split()
    while words and (words[0] in PREFIXES or ASSIGNMENT.match(words[0])):
        words = words[1:]
    return words


def _absolute(path: str) -> bool:
    return path.startswith(("/", "~", "$"))


def _depends_on_folder(words: list[str]) -> bool:
    program, args = words[0], words[1:]
    if "/" in program and not _absolute(program):
        return True
    if program in {"git", "make"}:
        return "-C" not in args
    for i, word in enumerate(args):
        redirect = REDIRECT.match(word)
        if redirect:
            target = redirect.group(1) or (args[i + 1] if i + 1 < len(args) else "")
            if target and not _absolute(target):
                return True
    operands = [w for w in args if not w.startswith("-") and not REDIRECT.match(w)]
    if program == "sed" and any(w == "-i" or w.startswith("-i") for w in args):
        return any(not _absolute(w) for w in operands[1:])
    if program in WRITERS:
        return any(not _absolute(w) for w in operands)
    return False


def main(stdin: TextIO = sys.stdin) -> int:
    try:
        event = json.load(stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("stop_hook_active"):
        return 0
    message = event.get("last_assistant_message")
    problems = find_problems(message) if isinstance(message, str) else []
    if problems:
        listed = "\n".join(f"- {p}" for p in problems)
        reason = (
            "Commands in your reply depend on the folder the owner runs them from (CHB-126):\n"
            f"{listed}\n"
            "Rewrite them to work from any folder: absolute paths, `git -C <repo>`, `make -C <repo>`, "
            "or start the block with `cd ~/workspace/<repo> &&`. "
            "If a block is not meant to be pasted, mark it ```text."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
