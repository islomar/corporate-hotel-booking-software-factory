#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny Bash commands that write to Jira around factory/bin/jira (CHB-108).

`acli` is logged in as the owner, and a raw REST call skips the wrapper's checks (WIP limits,
required sections, CHB keys only). Agents write to Jira only with factory/bin/jira.

Denied:
- `acli jira ...` unless the subcommand only reads: view, search, list*, get*, `auth status`,
  or `list` under a workitem group (comment, attachment, link, watcher). Unknown subcommands are denied,
  and so is `--help` on a write subcommand: a flag value can look like `-h`.
- curl, wget or Python calls to *.atlassian.net or a /rest/api or /rest/agile path that send
  data or use a method other than GET or HEAD.

The match runs on the whole command text, so a commit message or heredoc that contains such a
command is denied too. Write such text to a file and pass the file.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON is denied. A crash exits 1, which Claude Code treats as
a non-blocking error: the call goes ahead. Keep this script small.
"""

from __future__ import annotations

import json
import re
import sys

READ_VERBS = {"view", "search", "list", "get", "list-watchers", "list-projects", "list-sprints"}
READ_PREFIXES = ("list-", "get-")
WORKITEM_GROUPS = {"comment", "attachment", "link", "watcher"}

ACLI = re.compile(r"(?:^|[\s;&|()`'\"/=])acli(?=\s)")
SEPARATOR = re.compile(r"[;&|\n)`]")
JIRA_URL = re.compile(r"atlassian\.net|/rest/(?:api|agile)/", re.IGNORECASE)
METHOD = re.compile(r"(?:^|[\s'\"])(?:-[A-Za-z]*X\s*|--request(?:\s+|=)|--method(?:\s+|=))['\"]?([A-Za-z]+)")
DATA = re.compile(
    r"(?:^|[\s'\"])(?:--data[\w-]*|--json|--form[\w-]*|--upload-file|--post-data|--post-file|--body-data|--body-file"
    r"|-[A-Za-z]*[dFT])"
)
PYTHON = re.compile(r"\bpython[0-9.]*\b")
PYTHON_WRITE = re.compile(
    r"\.(?:post|put|patch|delete)\s*\(|\bmethod\s*=\s*['\"]?(?!GET\b|HEAD\b)\w|\b(?:data|json)\s*=", re.IGNORECASE
)

WRAPPER = "Use factory/bin/jira (create, transition, comment, assign, rank, section); see CLAUDE.md."


def acli_write(command: str) -> str | None:
    """The first `acli jira` call in the command that is not a read, or None."""
    for match in ACLI.finditer(command):
        rest = SEPARATOR.split(command[match.end() :], maxsplit=1)[0]
        positional = [w for w in re.findall(r"[^\s'\"]+", rest) if not w.startswith("-")]
        if "jira" not in positional:
            continue
        args = positional[positional.index("jira") + 1 :]
        if not _acli_read(args):
            return "acli jira " + " ".join(args[:3])
    return None


def _acli_read(args: list[str]) -> bool:
    if len(args) < 2:
        return True  # `acli jira` or `acli jira workitem` alone prints help
    group, verb = args[0], args[1]
    if group == "auth":
        return verb == "status"
    if group == "workitem" and verb in WORKITEM_GROUPS:
        return len(args) > 2 and args[2] == "list"
    return verb in READ_VERBS or verb.startswith(READ_PREFIXES)


def http_write(command: str) -> bool:
    """True when the command sends a write request to Jira with curl, wget or Python."""
    if not JIRA_URL.search(command):
        return False
    if any(m.group(1).upper() not in {"GET", "HEAD"} for m in METHOD.finditer(command)):
        return True
    if DATA.search(command):
        return True
    return bool(PYTHON.search(command) and PYTHON_WRITE.search(command))


def reason(command: str) -> str | None:
    """Why the command is denied, or None when it may run."""
    found = acli_write(command)
    if found:
        return f"Blocked: `{found}` writes to Jira as the owner's acli login. {WRAPPER}"
    if http_write(command):
        return f"Blocked: a direct write request to the Jira REST API skips the wrapper's checks. {WRAPPER}"
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
    except ValueError:
        print("Blocked: the Jira write hook could not read its input.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    why = reason(command)
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
