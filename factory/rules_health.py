#!/usr/bin/env python3
"""Deterministic checks of the rules health check (CHB-167, procedure: docs/process/rules-health.md).

Rules and process documents name paths, commands and exit codes. When the code moves on, the text
keeps the old name and agents follow it. This script finds four kinds of stale text, reads only
files, and needs no network:

- dead-path: a backticked path under a repository folder (docs/, factory/, infra/, ...) that resolves nowhere (repo root, the file's folder, factory/, infra/).
- dead-command: `factory/bin/jira <sub>` with a subcommand jira.py does not define, or `make <t>`
  with a target the Makefile does not define.
- unknown-exit-code: "exit N" / "exits N" / "exit code N" where N is no EXIT_* constant of
  factory/jira.py or factory/as_agent.py (and not 0). Whether the meaning matches is left to the agent.
- hook-registration: a hook in factory/hooks that .claude/settings.json does not run, or a hook that
  settings.json runs and no file holds.

Output: JSON `{"findings": [...]}`; each finding has check, file, line, claim, detail and a stable id
(hash of check, file and claim, so a moved line keeps its id). Exit 1 with findings, 0 without.
Duplicates and contradictions need judgment; the agent does them (docs/process/rules-health.md).

Usage: python3 -I factory/rules_health.py [--root <repo>]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

Finding = dict[str, Any]

TOP_DOCS = ("CLAUDE.md", "README.md", "TODO.md", "factory/README.md")
DOC_FOLDERS = ("docs/process", "docs/decisions", "docs/knowledge")
ROOT_FOLDERS = ("docs", "factory", "infra", "scripts", "backend", "frontend", ".github", ".claude")
# Names of files that Claude Code reads from a user's repository (ADR-ea8c quotes them); this repository has none.
NOT_OURS = frozenset({".claude/CLAUDE.md", ".claude/rules"})
BASES = ("", "factory", "infra", ".github")  # where a path in a rule may be relative to
SKIP_PREFIXES = ("refs/", "docs/sessions/", "docs/postmortems/", "http", "~", "$", "<")
PATH_PATTERN = re.compile(r"[\w.\-/]+")
JIRA_COMMAND = re.compile(r"factory/bin/jira\s+([a-z][a-z-]*)\b")
MAKE_COMMAND = re.compile(r"(?:^|\s)make\s+([a-z][\w-]*)\b")
EXIT_PHRASE = re.compile(r"\bexits?(?: code)?\s+(\d+)\b", re.IGNORECASE)
FENCE = re.compile(r"^\s*```")


def finding(check: str, file: str, line: int, claim: str, detail: str) -> Finding:
    digest = hashlib.sha256(f"{check}|{file}|{claim}".encode()).hexdigest()[:10]
    return {"id": digest, "check": check, "file": file, "line": line, "claim": claim, "detail": detail}


def code_spans(text: str) -> list[tuple[int, str]]:
    """(line, content) of each inline backtick span outside fenced blocks."""
    spans: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            spans += [(number, m.group(1)) for m in re.finditer(r"`([^`\n]+)`", line)]
    return spans


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(line, text) of each fenced line and each inline span: where a command can appear."""
    lines: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif fenced:
            lines.append((number, line))
    return lines + code_spans(text)


def _normal(token: str) -> str | None:
    """The path a token claims, or None when it is not one."""
    token = re.sub(r"[:#].*$", "", token.strip())
    if not token or token.startswith(SKIP_PREFIXES) or not PATH_PATTERN.fullmatch(token):
        return None
    path = token.lstrip("/")
    path = path.rstrip("/")
    if path in NOT_OURS or "/" not in path or path.split("/")[0] not in ROOT_FOLDERS:
        return None
    return path


def dead_paths(text: str, file: str, root: Path) -> list[Finding]:
    found: list[Finding] = []
    seen: set[str] = set()
    folder = (root / file).parent
    for line, token in code_spans(text):
        path = _normal(token)
        if path is None or path in seen:
            continue
        seen.add(path)
        if any((base / path).exists() for base in [folder, *(root / b for b in BASES)]):
            continue
        found.append(finding("dead-path", file, line, path, f"`{path}` does not exist (checked from the repo root, {file}'s folder, factory/, infra/, .github/)"))
    return found


def jira_subcommands(source: str) -> set[str]:
    return set(re.findall(r'add_parser\(\s*"([a-z][a-z-]*)"', source))


def make_targets(makefile: str) -> set[str]:
    return set(re.findall(r"^([a-z][\w-]*):(?!=)", makefile, re.MULTILINE))


def dead_commands(text: str, file: str, subcommands: set[str], targets: set[str]) -> list[Finding]:
    found: list[Finding] = []
    seen: set[str] = set()
    for line, code in _code_lines(text):
        for sub in JIRA_COMMAND.findall(code):
            claim = f"factory/bin/jira {sub}"
            if sub not in subcommands and claim not in seen:
                seen.add(claim)
                found.append(finding("dead-command", file, line, claim, "factory/jira.py defines no such subcommand"))
        for target in MAKE_COMMAND.findall(code):
            claim = f"make {target}"
            if target not in targets and claim not in seen:
                seen.add(claim)
                found.append(finding("dead-command", file, line, claim, "the Makefile defines no such target"))
    return found


def exit_codes(source: str) -> set[int]:
    codes: set[int] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            if any(isinstance(t, ast.Name) and t.id.startswith("EXIT_") for t in node.targets):
                codes.add(node.value.value)
    return codes


def unknown_exit_codes(text: str, file: str, known: set[int]) -> list[Finding]:
    found: list[Finding] = []
    seen: set[int] = set()
    for number, line in enumerate(text.splitlines(), 1):
        for match in EXIT_PHRASE.finditer(line):
            code = int(match.group(1))
            if code != 0 and code not in known and code not in seen:
                seen.add(code)
                found.append(finding("unknown-exit-code", file, number, f"exit {code}", f"no EXIT_* constant in factory/jira.py or factory/as_agent.py has the value {code} (known: {sorted(known)})"))
    return found


def hook_registration(root: Path) -> list[Finding]:
    settings = (root / ".claude/settings.json").read_text(encoding="utf-8")
    registered = set(re.findall(r"factory/hooks/[\w.\-]+\.py", settings))
    found: list[Finding] = []
    for path in sorted((root / "factory/hooks").glob("*.py")):
        name = f"factory/hooks/{path.name}"
        if "__main__" in path.read_text(encoding="utf-8") and name not in registered:
            found.append(finding("hook-registration", ".claude/settings.json", 1, name, "the hook has a main block and settings.json does not run it"))
    for name in sorted(registered):
        if not (root / name).is_file():
            found.append(finding("hook-registration", ".claude/settings.json", 1, name, "settings.json runs a hook that does not exist"))
    return found


def documents(root: Path) -> list[Path]:
    """The rule and process documents. Session exports and postmortems are data, not rules."""
    files = [root / name for name in TOP_DOCS if (root / name).is_file()]
    for folder in DOC_FOLDERS:
        files += sorted((root / folder).glob("*.md"))
    return files


def run(root: Path) -> list[Finding]:
    jira_source = (root / "factory/jira.py").read_text(encoding="utf-8")
    known = exit_codes(jira_source) | exit_codes((root / "factory/as_agent.py").read_text(encoding="utf-8")) | {2}
    subcommands = jira_subcommands(jira_source)
    targets = make_targets((root / "Makefile").read_text(encoding="utf-8"))
    found: list[Finding] = []
    for path in documents(root):
        file = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        found += dead_paths(text, file, root)
        found += dead_commands(text, file, subcommands, targets)
        found += unknown_exit_codes(text, file, known)
    return found + hook_registration(root)


def main(argv: list[str], out: TextIO = sys.stdout) -> int:
    parser = argparse.ArgumentParser(description="Deterministic checks of the rules health check (CHB-167)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    found = run(parser.parse_args(argv).root)
    json.dump({"findings": found}, out, indent=2)
    out.write("\n")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
