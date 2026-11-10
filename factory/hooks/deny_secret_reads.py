#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny Read of secret files and Bash commands that print secrets (CHB-30).

An agent never needs a secret in its context: `factory/bin/as-agent` and `factory/bin/jira` read
their token themselves, inside the process, and `.env.example` holds the variable names.

Secret paths: any path with a `.ssh` folder (`~/.ssh/**`), `*.pem`, `*.key`, `.env` and `.env.*`
except `.env.example`, and any path with `.config/op` (`~/.config/op/**`). `~`, `$HOME` and `${HOME}`
expand to the home directory; a glob such as `*.pem` or `.env*` counts.
Denied:
- Read of a secret path; a relative path resolves against the event's `cwd`;
- a Bash simple command with a secret path as a word, an `--option=<path>` value or the part
  after `:` (`git show HEAD:.env`, `scp host:~/.ssh/id`), unless the program only reads metadata
  or writes (METADATA: `ls`, `stat`, `test`, `chmod`, `ssh`, `echo`, ...);
- input redirected from a secret path (`< .env`), whatever the program;
- `secret-tool lookup` and `op read` run directly.
Launchers (`rtk`, `env`, `uv run`, `factory/bin/as-agent`) are stripped first (shell_words).

A guardrail, not a boundary: it reads the command text only. A path built at run time, a `cd` into
`~/.ssh` followed by a relative name, or a script that reads the file passes.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import posixpath
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_words import name, positionals, simple_commands, tokens  # noqa: E402

KEY_SUFFIXES = (".pem", ".key")
ENV_EXAMPLE = ".env.example"
HOME_FORMS = ("~", "$HOME", "${HOME}")
GLOB = "*?["

# Programs that take a secret path without printing the file: metadata, permissions, ssh itself, writes.
METADATA = {"ls", "stat", "test", "[", "chmod", "chown", "mkdir", "touch", "rm", "echo", "printf", "ssh", "ssh-add"}
SECRET_TOOL_VALUE_OPTIONS: set[str] = set()
OP_VALUE_OPTIONS = {"--account", "--config", "--session", "--encoding", "--format", "--cache"}

ADVICE = (
    "Agents do not read secrets (CHB-30). `factory/bin/as-agent` and `factory/bin/jira` read their token themselves; "
    "`.env.example` lists the variable names. Ask the owner if a secret value is really needed."
)


def _expand(word: str, home: str) -> str:
    for form in HOME_FORMS:
        if word == form or word.startswith(form + "/"):
            return home + word[len(form) :]
    return word


def secret_path(word: str, *, home: str) -> bool:
    """True when the word names a secret file or folder, or a glob over them."""
    parts = [p for p in _expand(word, home).split("/") if p]
    if not parts:
        return False
    if ".ssh" in parts or any(a == ".config" and b == "op" for a, b in zip(parts, parts[1:])):
        return True
    base = parts[-1]
    if any(base.endswith(s) and len(base) > len(s) and not base.startswith(".") for s in KEY_SUFFIXES):
        return True
    if base == ENV_EXAMPLE:
        return False
    return base == ".env" or base.startswith(".env.") or (base.startswith(".env") and len(base) > 4 and base[4] in GLOB)


def _candidates(word: str) -> list[str]:
    """The word, an option's `=value`, and the part after the first `:` (`HEAD:.env`, `host:~/.ssh/id`)."""
    found = [word]
    if word.startswith("-") and "=" in word:
        found.append(word.split("=", 1)[1])
    if ":" in word:
        found.append(word.split(":", 1)[1])
    return found


def read_reason(path: str, *, home: str, cwd: str) -> str | None:
    """Why a Read of the path is denied, or None."""
    full = _expand(path, home)
    if not posixpath.isabs(full):
        full = posixpath.join(cwd, full)
    if secret_path(posixpath.normpath(full), home=home):
        return f"Blocked: `{path}` is a secret file. {ADVICE}"
    return None


def _vault_lookup(words: list[str]) -> bool:
    """True for `secret-tool lookup` and `op read`. Returns a bool so no value reaches the message."""
    prog, args = name(words[0]), words[1:]
    if prog == "secret-tool":
        return positionals(args, SECRET_TOOL_VALUE_OPTIONS)[:1] == ["lookup"]
    return prog == "op" and positionals(args, OP_VALUE_OPTIONS)[:1] == ["read"]


def bash_reason(command: str, *, home: str) -> str | None:
    """Why the command is denied, or None when it may run."""
    words = tokens(command)
    for op, target in zip(words, words[1:]):
        if op and all(c in "<>&|;()\n" for c in op) and "<" in op and "(" not in op and secret_path(target, home=home):
            return f"Blocked: the command reads `{target}`, a secret file. {ADVICE}"
    for simple in simple_commands(command):
        if _vault_lookup(simple):
            return f"Blocked: `secret-tool lookup` and `op read` print a secret. {ADVICE}"
        if name(simple[0]) in METADATA:
            continue
        for word in simple[1:]:
            if any(secret_path(c, home=home) for c in _candidates(word)):
                return f"Blocked: the command names `{word}`, a secret file. {ADVICE}"
    return None


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the secrets hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the secrets hook could not read its input.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    if not isinstance(tool_input, dict):
        return 0
    home = os.path.expanduser("~")
    why = None
    if isinstance(tool_input.get("command"), str):
        why = bash_reason(tool_input["command"], home=home)
    elif isinstance(tool_input.get("file_path"), str):
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
        why = read_reason(tool_input["file_path"], home=home, cwd=cwd)
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
