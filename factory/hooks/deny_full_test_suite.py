#!/usr/bin/env python3
"""Claude Code PreToolUse hook: deny bare full-suite test runs while iterating (CHB-54).

While iterating, the agent runs the tests related to its change. The full suite runs in
`make check` before every commit (CLAUDE.md), and `make check` and `make test` stay allowed.

Denied, when nothing narrows the run:
- `pytest`, `py.test`, `python -m pytest`, `uv run pytest`: narrowed by a path (a word with `/` or `::`,
  or ending in `.py`), `-k` or `--lf`; a marker (`-m`) does not narrow;
- `vitest`, `npx vitest run`, `npm test`, `npm run test`: narrowed by a file filter, `-t` or `--changed`;
- `python3 -m unittest` and `python3 -m unittest discover`: narrowed by `-s <dir>`, `-p <pattern>`,
  `-k`, a start folder, a module or a file.
A path of `.` and a vitest filter that is a number do not narrow. Redirections and launchers
(`rtk`, `uv run`, `npx`, `cd x &&`) are stripped first.

Contract (https://code.claude.com/docs/en/hooks): the tool call arrives as JSON on stdin.
Exit 2 with the reason on stderr denies the call. Exit 0 with no output leaves the normal
permission flow. Input that is not JSON, or an error inside the matcher, is denied too.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shell_words import name, positional_indexes, positionals, simple_commands  # noqa: E402

WHOLE = {".", "./"}

PYTEST_VALUE_OPTIONS = {
    "-m", "-p", "-o", "-c", "-n", "-W", "-r", "--tb", "--maxfail", "--rootdir", "--ignore", "--ignore-glob",
    "--deselect", "--confcutdir", "--basetemp", "--durations", "--cov", "--cov-report", "--junitxml",
    "--log-level", "--import-mode", "--override-ini", "--config-file", "--capture",
}
PYTEST_FILTERS = ("-k", "--keyword", "--lf", "--last-failed")

VITEST_VALUE_OPTIONS = {
    "-c", "--config", "-r", "--root", "--dir", "--reporter", "--environment", "--pool", "--project",
    "--outputFile", "--testTimeout", "--shard", "--maxWorkers", "--minWorkers", "--mode",
}
VITEST_FILTERS = ("-t", "--testNamePattern", "--changed", "--related")
VITEST_COMMANDS = {"run", "watch", "dev", "related"}

NPM_VALUE_OPTIONS = {"--prefix", "-w", "--workspace", "-C", "--userconfig", "--cache"}
NPM_TEST = {"test", "t", "tst"}
NPM_RUN = {"run", "run-script", "rum", "urn"}

UNITTEST_VALUE_OPTIONS = {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory", "-k"}
UNITTEST_FILTERS = ("-k", "-p", "--pattern")

REASON = (
    "Blocked: `{run}` runs the full suite. While iterating, run the related tests only: "
    "`uv run pytest tests/test_x.py` or `uv run pytest -k name`, "
    "`npx vitest run src/x.test.ts` or `npm test -- src/x.test.ts`, "
    "`python3 -I -m unittest discover -s factory/tests -p 'test_x.py'`. "
    "`make check` runs the full suite before every commit (CLAUDE.md)."
)


def _has(args: list[str], options: tuple[str, ...]) -> bool:
    """True when an option is present, alone, with `=value`, or as a short option with its value attached."""
    for arg in args:
        if arg == "--":
            break
        for option in options:
            if arg == option or arg.startswith(option + "="):
                return True
            if len(option) == 2 and arg.startswith(option) and not arg.startswith("--"):
                return True
    return False


def _narrowed(args: list[str], value_options: set[str], filters: tuple[str, ...], narrows: Callable[[str], bool]) -> bool:
    return _has(args, filters) or any(narrows(p) for p in positionals(args, value_options))


def _pytest_path(word: str) -> bool:
    """A path or node id: `tests/`, `test_x.py`, `x.py::t`. A bare word may be the value of an unknown option."""
    return word not in WHOLE and ("/" in word or "::" in word or word.endswith(".py"))


def _vitest_filter(word: str) -> bool:
    """A file name filter. A number is more likely the value of an unknown option such as `--retry 2`."""
    return word not in WHOLE and not word.isdigit()


def _any_target(word: str) -> bool:
    return word not in WHOLE


def full_suite(words: list[str]) -> bool:
    """True when the simple command runs a whole test suite with nothing narrowing it."""
    if not words:
        return False
    prog, args = name(words[0]), words[1:]
    if prog in {"pytest", "py.test"}:
        return not _narrowed(args, PYTEST_VALUE_OPTIONS, PYTEST_FILTERS, _pytest_path)
    if prog == "vitest":
        if args[:1] and args[0] in VITEST_COMMANDS:
            args = args[1:]
        return not _narrowed(args, VITEST_VALUE_OPTIONS, VITEST_FILTERS, _vitest_filter)
    if prog == "npm":
        script_args = _npm_test_args(args)
        return script_args is not None and not _narrowed(script_args, VITEST_VALUE_OPTIONS, VITEST_FILTERS, _vitest_filter)
    if prog == "unittest":
        if args[:1] == ["discover"]:
            args = args[1:]
        start = _option_value(args, {"-s", "--start-directory"})
        return start in (None, *WHOLE) and not _narrowed(args, UNITTEST_VALUE_OPTIONS, UNITTEST_FILTERS, _any_target)
    return False


def _npm_test_args(args: list[str]) -> list[str] | None:
    """The arguments passed to the `test` script (after `--`), or None when npm runs something else."""
    at = positional_indexes(args, NPM_VALUE_OPTIONS)
    if not at:
        return None
    if args[at[0]] in NPM_TEST:
        script_args = args[at[0] + 1 :]
    elif args[at[0]] in NPM_RUN and len(at) > 1 and args[at[1]] in NPM_TEST:
        script_args = args[at[1] + 1 :]
    else:
        return None
    return [a for a in script_args if a != "--"]


def _option_value(args: list[str], options: set[str]) -> str | None:
    for k, arg in enumerate(args):
        if arg in options and k + 1 < len(args):
            return args[k + 1]
        key, _, value = arg.partition("=")
        if key in options and value:
            return value
    return None


def reason(command: str) -> str | None:
    """Why the command is denied, or None when it may run."""
    for words in simple_commands(command):
        if full_suite(words):
            return REASON.format(run=" ".join(words))
    return None


def main() -> int:
    try:
        return _decide(sys.stdin.read())
    except Exception as error:  # fail closed: a guardrail that crashes must not let the call run
        print(f"Blocked: the related-tests hook failed ({type(error).__name__}).", file=sys.stderr)
        return 2


def _decide(raw: str) -> int:
    try:
        event = json.loads(raw)
    except ValueError:
        print("Blocked: the related-tests hook could not read its input.", file=sys.stderr)
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
