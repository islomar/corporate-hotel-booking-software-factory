#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny Read of secret files and Bash commands that print secrets (CHB-30).

An agent never needs a secret in its context: `factory/bin/as-agent` and `factory/bin/jira` read
their token themselves, inside the process, and `.env.example` holds the variable names.

Secret paths: any path with a `.ssh` folder (`~/.ssh/**`), `*.pem`, `*.key`, `.env` and `.env.*`
except `.env.example`, and any path with `.config/op` (`~/.config/op/**`). `~`, `$HOME` and `${HOME}`
expand to the home directory; a glob such as `*.pem` or `.env*` counts.
Denied:
- Read of a secret path, or of a symlink whose real path is one; a relative path resolves against
  the event's `cwd`;
- a Bash simple command with a secret path as a word, an `--option=<path>` value or the part
  after `:` (`git show HEAD:.env`, `scp host:~/.ssh/id`), unless the program only reads metadata
  or writes (METADATA: `ls`, `stat`, `test`, `chmod`, `ssh-add`, `echo`, ...);
- input redirected from a secret path (`< .env`), whatever the program;
- `secret-tool lookup` and `op read` run directly.
Launchers (`rtk`, `env`, `uv run`, `factory/bin/as-agent`) are stripped first (shell_words).
The body of every `$(...)` and backtick substitution, quoted or not and nested parentheses included
(`"$( (cat .env) )"`), the arguments of `bash -c`,
`sh -c` and `eval`, and the remote command of `ssh` are checked the same way. The key and config
file options of `ssh` and `scp` (`-i`, `-F`, `-o IdentityFile=`) may name a secret path.

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
import re
import sys
from fnmatch import fnmatchcase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_words import input_redirection, name, positionals, simple_commands, tokens  # noqa: E402

KEY_SUFFIXES = (".pem", ".key")
ENV_EXAMPLE = ".env.example"
HOME_FORMS = ("~", "$HOME", "${HOME}")
GLOB = "*?["

# Programs that take a secret path without printing the file: metadata, permissions, the ssh agent, writes.
METADATA = {"ls", "stat", "test", "[", "chmod", "chown", "mkdir", "touch", "rm", "echo", "printf", "ssh-add"}
SHELLS = {"bash", "sh", "zsh", "dash", "eval"}  # their arguments are commands
KEY_FILE_PROGRAMS = {"ssh", "scp"}
KEY_FILE_OPTIONS = {"-i", "-F"}
SSH_VALUE_OPTIONS = {"-o", "-p", "-P", "-l", "-J", "-L", "-R", "-D", "-E", "-c", "-b", "-m", "-O", "-Q", "-S", "-W", "-w", "-e", "-B"}
BACKTICK = re.compile(r"`([^`]*)`")
INNERMOST = re.compile(r"\$\(([^()]*)\)")  # a `$(...)` without parentheses inside
MAX_DEPTH = 4
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


def _dot_name(part: str, name_: str) -> bool:
    """The part is the hidden name, or a glob that starts with `.` and matches it (`.ss*`, `.e*`)."""
    return part == name_ or (part.startswith(".") and any(c in part for c in GLOB) and fnmatchcase(name_, part))


def _key_file(base: str) -> bool:
    """`x.pem`, `x.key`, or a glob whose extension matches one (`*.pe?`, `x.k*`); not `*.*` or a hidden `.x.key`."""
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or stem.startswith(".") or ext == "*":
        return False
    return any(ext == s[1:] or (any(c in ext for c in GLOB) and fnmatchcase(s[1:], ext)) for s in KEY_SUFFIXES)


def secret_path(word: str, *, home: str) -> bool:
    """True when the word names a secret file or folder, or a glob over them."""
    parts = [p for p in _expand(word, home).split("/") if p]
    if not parts:
        return False
    if any(_dot_name(p, ".ssh") for p in parts):
        return True
    if any(_dot_name(a, ".config") and (b == "op" or fnmatchcase("op", b)) for a, b in zip(parts, parts[1:])):
        return True
    base = parts[-1]
    if _key_file(base):
        return True
    if base == ENV_EXAMPLE:
        return False
    return _dot_name(base, ".env") or base.startswith(".env.")


def _candidates(word: str) -> list[str]:
    """The word, an option's `=value`, and the part after the first `:` (`HEAD:.env`, `host:~/.ssh/id`)."""
    found = [word]
    if word.startswith("-") and "=" in word:
        found.append(word.split("=", 1)[1])
    if ":" in word:
        found.append(word.split(":", 1)[1])
    return found


def read_reason(path: str, *, home: str, cwd: str) -> str | None:
    """Why a Read of the path is denied, or None.

    Both the path as written and its real path count, so a symlink to a secret file or folder is denied.
    """
    full = _expand(path, home)
    if not posixpath.isabs(full):
        full = posixpath.join(cwd, full)
    if secret_path(posixpath.normpath(full), home=home) or secret_path(posixpath.realpath(full), home=home):
        return f"Blocked: `{path}` is a secret file. {ADVICE}"
    return None


def substitutions(command: str) -> list[str]:
    """The bodies of the outermost and the innermost `$(...)` substitutions, and of backticks.

    Outermost bodies keep nested parentheses (`$( (cat x) )`). Parentheses are counted, quotes are not:
    a `$(` that never closes gives the rest of the command, so a miscount checks more text, never less.
    Innermost bodies are checked one level down, so deep nesting never runs past MAX_DEPTH.
    """
    bodies: list[str] = []
    i = command.find("$(")
    while i != -1:
        start, depth, j = i + 2, 1, i + 2
        while j < len(command) and depth:
            depth += {"(": 1, ")": -1}.get(command[j], 0)
            j += 1
        bodies.append(command[start : j - 1] if depth == 0 else command[start:])
        i = command.find("$(", j)
    bodies.extend(match.group(1) for match in INNERMOST.finditer(command))
    bodies.extend(match.group(1) for match in BACKTICK.finditer(command))
    return bodies


def _vault_lookup(words: list[str]) -> bool:
    """True for `secret-tool lookup` and `op read`. Returns a bool so no value reaches the message."""
    prog, args = name(words[0]), words[1:]
    if prog == "secret-tool":
        return positionals(args, set())[:1] == ["lookup"]  # secret-tool has no option that takes a value
    return prog == "op" and positionals(args, OP_VALUE_OPTIONS)[:1] == ["read"]


def _without_key_files(args: list[str]) -> list[str]:
    """ssh and scp arguments without `-i <key>`, `-F <config>` and `-o IdentityFile=<key>` before the first operand.

    Words from the first operand on (the host and the remote command) stay, options or not.
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("-"):
            return out + args[i:]
        if arg in KEY_FILE_OPTIONS or (arg == "-o" and args[i + 1 : i + 2] and args[i + 1].lower().startswith("identityfile")):
            i += 2
            continue
        if arg in SSH_VALUE_OPTIONS:
            out.extend(args[i : i + 2])
            i += 2
            continue
        if arg[:2] not in KEY_FILE_OPTIONS and not arg.lower().startswith("-oidentityfile"):
            out.append(arg)
        i += 1
    return out


def bash_reason(command: str, *, home: str, depth: int = 0) -> str | None:
    """Why the command is denied, or None when it may run."""
    if depth > MAX_DEPTH:
        return None
    for body in substitutions(command):
        why = bash_reason(body, home=home, depth=depth + 1)
        if why:
            return why
    words = tokens(command)
    for op, target in zip(words, words[1:]):
        if input_redirection(op) and secret_path(target, home=home):
            return f"Blocked: the command reads `{target}`, a secret file. {ADVICE}"
    for simple in simple_commands(command):
        if _vault_lookup(simple):
            return f"Blocked: `secret-tool lookup` and `op read` print a secret. {ADVICE}"
        prog, args = name(simple[0]), simple[1:]
        if prog in METADATA:
            continue
        if prog in KEY_FILE_PROGRAMS:
            args = _without_key_files(args)
        for word in args:
            if any(secret_path(c, home=home) for c in _candidates(word)):
                return f"Blocked: the command names `{word}`, a secret file. {ADVICE}"
            if prog in SHELLS or prog == "ssh":
                why = bash_reason(word, home=home, depth=depth + 1)
                if why:
                    return why
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
        cwd = event.get("cwd") if isinstance(event.get("cwd"), str) and event.get("cwd") else os.getcwd()
        why = read_reason(tool_input["file_path"], home=home, cwd=cwd)
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
