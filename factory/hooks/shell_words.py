"""Split a Bash command into simple commands and strip launchers, for the Bash hooks (CHB-54).

`simple_commands("cd x && rtk uv run pytest -q")` gives `[["cd", "x"], ["pytest", "-q"]]`.
The split is textual, like the other hooks: a heredoc body is read as commands too.
A command that shlex cannot read (an apostrophe in a heredoc) falls back to a plain split,
so a quoting quirk never turns into a crash that denies every command.
"""

from __future__ import annotations

import posixpath
import re
import shlex

PUNCTUATION = ";&|()\n"
FALLBACK_SEPARATOR = re.compile(r"[;&|()\n]+")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"^python[0-9.]*$")

# Launchers dropped with their leading flags: `rtk git ...`, `as-agent git ...`, `env X=1 git ...`.
PLAIN_LAUNCHERS = {"rtk", "as-agent", "env", "time", "nice", "nohup", "command", "exec", "uvx", "npx"}
RUN_LAUNCHERS = {("uv", "run"), ("npm", "exec"), ("poetry", "run"), ("rtk", "proxy")}
LAUNCHER_VALUE_OPTIONS = {
    "--project", "--directory", "--with", "--with-requirements", "--extra", "--group", "--python", "-p",
    "--package", "--env-file", "--index", "--prefix", "-w", "--workspace", "-c", "--call",
}
PYTHON_VALUE_OPTIONS = {"-X", "-W", "-Q"}


def tokens(command: str) -> list[str]:
    """Words and separators of the command; separators are runs of ; & | ( ) and newlines."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: split on separators and spaces, drop quote marks
        out: list[str] = []
        for part in FALLBACK_SEPARATOR.split(command):
            out.extend(w.strip("'\"") for w in part.split())
            out.append(";")
        return out


def _separator(word: str) -> bool:
    return bool(word) and all(c in PUNCTUATION for c in word)


def simple_commands(command: str) -> list[list[str]]:
    """The command's simple commands, each with launchers stripped, empty ones left out."""
    found: list[list[str]] = []
    current: list[str] = []
    for word in [*tokens(command), ";"]:
        if _separator(word):
            words = strip_launchers(current)
            if words:
                found.append(words)
            current = []
        else:
            current.append(word)
    return found


def name(word: str) -> str:
    """The program name of a word: `/usr/bin/git` and `factory/bin/as-agent` give `git` and `as-agent`."""
    return posixpath.basename(word)


def strip_launchers(words: list[str]) -> list[str]:
    """Drop assignments and launchers so the first word is the program that runs."""
    i = 0
    while i < len(words):
        word, prog = words[i], name(words[i])
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if ASSIGNMENT.match(word):
            i += 1
        elif (prog, nxt) in RUN_LAUNCHERS:
            i = _skip_options(words, i + 2, LAUNCHER_VALUE_OPTIONS)
        elif prog in PLAIN_LAUNCHERS:
            i = _skip_options(words, i + 1, LAUNCHER_VALUE_OPTIONS)
        elif prog == "timeout":
            i = _skip_options(words, i + 1, set()) + 1
        elif PYTHON.match(prog):
            j = _skip_options(words, i + 1, PYTHON_VALUE_OPTIONS, stop="-m")
            if j + 1 < len(words) and words[j] == "-m":
                return [words[j + 1], *words[j + 2 :]]
            return words[i:]
        else:
            return words[i:]
    return []


def _skip_options(words: list[str], i: int, value_options: set[str], stop: str = "") -> int:
    while i < len(words) and words[i].startswith("-") and words[i] != stop:
        if words[i] == "--":
            return i + 1
        i += 2 if words[i] in value_options else 1
    return i


def positionals(args: list[str], value_options: set[str]) -> list[str]:
    """Arguments that are not options or option values; everything after `--` counts."""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            out.extend(args[i + 1 :])
            break
        if arg.startswith("-") and arg != "-":
            i += 2 if arg in value_options else 1
            continue
        out.append(arg)
        i += 1
    return out
