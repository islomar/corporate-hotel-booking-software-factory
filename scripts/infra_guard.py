#!/usr/bin/env python3
"""Guard `make infra-plan` and `make infra-apply` (CHB-99).

On 2026-11-06 a plan from an outdated branch showed 4 destroys of live resources.
This script refuses to plan or apply unless the checkout is `main`, equal to
`origin/main`, with no local changes to the files that drive Terraform.

  git     Check the checkout only. The Makefile runs it before it asks gcloud for a token.
  plan    Check the checkout, then for each directory: init, plan to a saved file,
          summarise it, and fail when a resource would be destroyed or replaced.
  apply   Check the checkout and every saved plan (made from the current commit,
          unchanged since, no unapproved destroy), then apply those saved plans.

A destroy or replace passes only when ALLOW_DESTROY lists exactly the addresses
that the plans destroy or replace, comma-separated.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRS = ("gcp", "github")
PLANS = ROOT / "infra/.plans"
BRANCH = "main"
REMOTE = "origin"
# Local changes here change what Terraform plans, or bypass this guard.
WATCHED = ("infra", "Makefile", "scripts")

# (args, capture stdout) -> (exit code, stdout). Without capture, output goes to the terminal.
Runner = Callable[[list[str], bool], tuple[int, str]]


class Refused(Exception):
    """The guard refuses. The message says why and gives the command that fixes it."""


def run(args: list[str], capture: bool = True) -> tuple[int, str]:
    result = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE if capture else None)
    return result.returncode, (result.stdout or "").strip()


def git(runner: Runner, *args: str) -> str:
    code, out = runner(["git", *args], True)
    if code != 0:
        raise Refused(f"`git {' '.join(args)}` failed (exit {code}).")
    return out


def check_git(runner: Runner) -> str:
    """Return the commit to plan or apply. Raise Refused when the checkout is not main == origin/main."""
    branch = git(runner, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        raise Refused(
            f"Terraform runs only from `{BRANCH}`. The checkout is on `{branch}`.\n"
            f"Fix: git switch {BRANCH} && git pull --ff-only"
        )
    changes = git(runner, "status", "--porcelain", "--untracked-files=all", "--", *WATCHED)
    if changes:
        raise Refused(
            f"Local changes in {', '.join(WATCHED)} would change the plan:\n{changes}\n"
            "Fix: commit them on a branch and open a PR, or set them aside with: git stash push --include-untracked"
        )
    git(runner, "fetch", "--quiet", REMOTE, BRANCH)
    counts = git(runner, "rev-list", "--left-right", "--count", f"HEAD...{REMOTE}/{BRANCH}").split()
    ahead, behind = int(counts[0]), int(counts[1])
    if behind and not ahead:
        raise Refused(f"Local `{BRANCH}` is {behind} commit(s) behind `{REMOTE}/{BRANCH}`.\nFix: git pull --ff-only")
    if ahead:
        raise Refused(
            f"Local `{BRANCH}` has {ahead} commit(s) that are not on `{REMOTE}/{BRANCH}`"
            + (f" and is {behind} behind" if behind else "")
            + ". `main` changes only through merged PRs.\n"
            f"Inspect: git log {REMOTE}/{BRANCH}..{BRANCH}\n"
            f"Fix (keeps them on a backup branch): git branch {BRANCH}-backup && git reset --keep {REMOTE}/{BRANCH}"
        )
    return git(runner, "rev-parse", "HEAD")


@dataclass
class Summary:
    add: list[str] = field(default_factory=list)
    change: list[str] = field(default_factory=list)
    destroy: list[str] = field(default_factory=list)
    replace: list[str] = field(default_factory=list)
    forget: list[str] = field(default_factory=list)

    def counts(self) -> str:
        return (
            f"{len(self.add)} to add, {len(self.change)} to change, {len(self.destroy)} to destroy, "
            f"{len(self.replace)} to replace, {len(self.forget)} to forget"
        )

    def destructive(self) -> list[str]:
        return self.destroy + self.replace


def summarise(plan: dict) -> Summary:
    """Classify the resource changes of `terraform show -json <plan>`."""
    summary = Summary()
    for resource in plan.get("resource_changes", []):
        actions = resource["change"]["actions"]
        address = resource["address"]
        if sorted(actions) == ["create", "delete"]:
            summary.replace.append(address)
        elif actions == ["delete"]:
            summary.destroy.append(address)
        elif actions == ["create"]:
            summary.add.append(address)
        elif actions == ["update"]:
            summary.change.append(address)
        elif actions == ["forget"]:
            summary.forget.append(address)
        elif actions not in (["no-op"], ["read"]):
            raise Refused(f"Unknown Terraform actions {actions} for {address}. Update scripts/infra_guard.py.")
    return summary


def allowed(env: str | None) -> set[str]:
    return {a.strip() for a in (env or "").split(",") if a.strip()}


def check_destroys(summaries: dict[str, Summary], allow: set[str]) -> None:
    """Raise Refused unless ALLOW_DESTROY equals the set of destroyed and replaced addresses."""
    destructive = {f"{d}: {a}": a for d, s in summaries.items() for a in s.destructive()}
    found = set(destructive.values())
    if found == allow:
        return
    lines = []
    if found:
        lines.append("These resources would be destroyed or replaced:")
        lines += [f"  {entry}" for entry in sorted(destructive)]
    if allow - found:
        lines.append("ALLOW_DESTROY lists addresses that the plans do not destroy or replace:")
        lines += [f"  {a}" for a in sorted(allow - found)]
    if found:
        lines.append(
            "An unexpected destroy usually means the checkout is missing merged code. Do not apply it.\n"
            "If every destroy is intended, plan and apply again with exactly these addresses:\n"
            f"  make infra-plan ALLOW_DESTROY='{','.join(sorted(found))}'"
        )
    else:
        lines.append("Fix: run again without ALLOW_DESTROY.")
    raise Refused("\n".join(lines))


def plan_files(plans: Path, directory: str) -> tuple[Path, Path]:
    return plans / f"{directory}.tfplan", plans / f"{directory}.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def terraform(runner: Runner, directory: str, *args: str, capture: bool = False) -> str:
    code, out = runner(["terraform", f"-chdir=infra/{directory}", *args], capture)
    if code != 0:
        raise Refused(f"`terraform {' '.join(args)}` failed in infra/{directory} (exit {code}).")
    return out


def show(runner: Runner, directory: str, plan: Path) -> Summary:
    return summarise(json.loads(terraform(runner, directory, "show", "-json", str(plan), capture=True)))


def plan(runner: Runner, env: dict[str, str], plans: Path = PLANS) -> None:
    commit = check_git(runner)
    plans.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for directory in DIRS:
        tfplan, record = plan_files(plans, directory)
        tfplan.unlink(missing_ok=True)
        record.unlink(missing_ok=True)
        terraform(runner, directory, "init", "-input=false", capture=True)
        terraform(runner, directory, "plan", "-input=false", f"-out={tfplan}")
        summary = summaries[directory] = show(runner, directory, tfplan)
        record.write_text(json.dumps({"commit": commit, "plan_sha256": sha256(tfplan), **summary.__dict__}, indent=2))
    shown = plans.relative_to(ROOT) if plans.is_relative_to(ROOT) else plans
    print(f"\nPlans for commit {commit[:12]}, saved in {shown}/ (apply them only with make infra-apply):")
    for directory, summary in summaries.items():
        print(f"  {directory}: {summary.counts()}")
    check_destroys(summaries, allowed(env.get("ALLOW_DESTROY")))
    apply_command = "make infra-apply"
    if env.get("ALLOW_DESTROY"):
        apply_command += f" ALLOW_DESTROY='{env['ALLOW_DESTROY']}'"
    print(f"Read the plans above. If they are what you expect, run: {apply_command}")


def apply(runner: Runner, env: dict[str, str], plans: Path = PLANS) -> None:
    commit = check_git(runner)
    summaries = {}
    for directory in DIRS:
        tfplan, record = plan_files(plans, directory)
        if not tfplan.exists() or not record.exists():
            raise Refused(f"No saved plan for infra/{directory}.\nFix: make infra-plan")
        saved = json.loads(record.read_text())
        if saved.get("commit") != commit:
            raise Refused(
                f"The saved plan for infra/{directory} was made from commit {str(saved.get('commit'))[:12]}, "
                f"not from the current `{BRANCH}` ({commit[:12]}).\nFix: make infra-plan"
            )
        if saved.get("plan_sha256") != sha256(tfplan):
            raise Refused(f"{tfplan} changed after infra-plan wrote it.\nFix: make infra-plan")
        summaries[directory] = show(runner, directory, tfplan)
    check_destroys(summaries, allowed(env.get("ALLOW_DESTROY")))
    for directory in DIRS:
        tfplan, record = plan_files(plans, directory)
        terraform(runner, directory, "apply", "-input=false", str(tfplan))
        tfplan.unlink()
        record.unlink()


def red(text: str) -> str:
    return f"\033[1;31m{text}\033[0m" if sys.stderr.isatty() else text


def main(argv: list[str], runner: Runner = run, env: dict[str, str] | None = None) -> int:
    commands = {"git": lambda r, e: check_git(r), "plan": plan, "apply": apply}
    if len(argv) != 1 or argv[0] not in commands:
        print(f"usage: infra_guard.py {{{'|'.join(commands)}}}", file=sys.stderr)
        return 2
    try:
        commands[argv[0]](runner, dict(os.environ) if env is None else env)
    except Refused as refusal:
        sys.stdout.flush()
        print(red(f"infra guard refused ({argv[0]}):\n{refusal}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
