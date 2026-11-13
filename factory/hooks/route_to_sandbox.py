#!/usr/bin/env python3
"""Claude Code PreToolUse hook: third-party code runs in the sandbox container, not on the host (CHB-31).

Package installs, linters and test runners run code from npm and PyPI. On the host that code
could read the owner's keyring, 1Password and home folder. `.devcontainer/run <command>` runs it
in a container with no secret and an egress allow list (README.md, "Sandbox container").

Denied on the host, in any simple command of the Bash command line:
- `uv`, `uvx`, `npm`, `npx`, `pnpm`, `yarn`, `pip`, `pip3`, `pytest`, `py.test`, `vitest`;
- `python -m pytest` and `python -m pip`;
- `make` with a target in SANDBOX_TARGETS (`make -C <dir> check` too).
Wrappers (`timeout`, `nice -n 5`, `env -u X`, `xargs`, `sudo` and others) are skipped with their options.
Allowed: a simple command whose program is `.devcontainer/run`, `./.devcontainer/run` or the absolute
path of the launcher in the session's checkout (its arguments are not checked),
`make` without a target or with another target (`dev`, `e2e`, `up` start Docker themselves),
stdlib Python (`python3 -I -m unittest`, `python3 -I scripts/...`), and everything else.
It guards against mistakes, not against an agent that hides a call: `bash -c "npm ci"`, `eval`,
scripts that call these tools, and a `.devcontainer/run` reached after `cd` elsewhere are not seen. The git
pre-commit hook (scripts/git-hooks/pre-commit) uses the container too.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_words import ASSIGNMENT, PYTHON, is_operator, name, redirection, tokens  # noqa: E402

PROGRAMS = {"uv", "uvx", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "pytest", "py.test", "vitest"}
PYTHON_MODULES = {"pytest", "pip"}
SANDBOX_TARGETS = {"check", "test", "check-backend", "check-frontend", "check-deps", "format", "mutate"}
# Programs that run the next word as a command, with the options of each that take a value.
# `timeout` also takes one positional value, the duration.
WRAPPERS = {
    "rtk": set(), "time": set(), "nohup": set(), "command": set(), "exec": set(), "setsid": set(),
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "--class", "-n", "--classdata", "-p", "--pid"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "xargs": {"-n", "-I", "-P", "-L", "-d", "-E", "-s", "-a", "--max-args", "--max-procs", "--arg-file"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "--user", "--group"},
}
MAKE_VALUE_OPTIONS = {"-C", "--directory", "-f", "--file", "--makefile", "-j", "--jobs", "-I", "-o", "-W"}
LAUNCHER = ".devcontainer/run"


def segments(command: str) -> list[list[str]]:
    """Simple commands as written: words between operators, a redirection's target left out."""
    found: list[list[str]] = []
    current: list[str] = []
    words = tokens(command)
    i = 0
    while i < len(words):
        word = words[i]
        if is_operator(word):
            if redirection(word) and not any(c in word for c in ";|()\n") and "&&" not in word:
                i += 2
                continue
            if current:
                found.append(current)
            current = []
        else:
            current.append(word)
        i += 1
    if current:
        found.append(current)
    return found


def _program(words: list[str]) -> list[str]:
    """The words from the program that runs: assignments and wrappers with their options dropped."""
    i = 0
    while i < len(words):
        word = words[i]
        if ASSIGNMENT.match(word):
            i += 1
            continue
        wrapper = name(word)
        if wrapper not in WRAPPERS:
            break
        i += 1
        if wrapper == "rtk" and i < len(words) and words[i] == "proxy":
            i += 1
        value_options = WRAPPERS[wrapper]
        while i < len(words) and words[i].startswith("-") and words[i] != "-":
            i += 2 if words[i] in value_options else 1
        if wrapper == "timeout":
            i += 1  # the duration
    return words[i:]


def _is_launcher(word: str, top: str | None) -> bool:
    """`.devcontainer/run`, `./.devcontainer/run`, or the absolute path of this checkout's launcher."""
    if word in (LAUNCHER, f"./{LAUNCHER}"):
        return True
    return top is not None and os.path.isabs(word) and os.path.normpath(word) == os.path.join(top, LAUNCHER)


def _make_targets(args: list[str]) -> list[str]:
    targets, i = [], 0
    while i < len(args):
        word = args[i]
        if word in MAKE_VALUE_OPTIONS:
            i += 2
            continue
        if not word.startswith("-") and "=" not in word:
            targets.append(word)
        i += 1
    return targets


def _needs_sandbox(words: list[str], top: str | None) -> bool:
    if not words or _is_launcher(words[0], top):
        return False
    if words[0].endswith(LAUNCHER):  # a launcher of another checkout, or one the hook cannot place
        return True
    prog, args = name(words[0]), words[1:]
    if prog in PROGRAMS:
        return True
    if PYTHON.match(prog) and "-m" in args:
        module_index = args.index("-m") + 1
        return module_index < len(args) and args[module_index] in PYTHON_MODULES
    if prog == "make":
        return any(target in SANDBOX_TARGETS for target in _make_targets(args))
    return False


def reason(command: str, top: str | None = None) -> str | None:
    """Why the command must run in the sandbox, or None. `top` is the checkout of the session."""
    for words in segments(command):
        program = _program(words)
        if _needs_sandbox(program, top):
            shown = " ".join(program)
            return (
                f"Blocked: `{shown}` runs third-party code. Run it in the sandbox container: "
                f"`{LAUNCHER} {shown}` (no secrets, egress allow list; README.md, \"Sandbox container\"; CHB-31)."
            )
    return None


def _top(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the sandbox routing hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the sandbox routing hook could not read its input.", file=sys.stderr)
        return 2
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    why = reason(command, _top(cwd))
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
