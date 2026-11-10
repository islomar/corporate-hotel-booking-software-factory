#!/usr/bin/env python3
"""Run the Terraform guard tests of infra/github without credentials (CHB-158).

`infra/github/tests/guards.tftest.hcl` checks every precondition in `guards.tf` against stubbed
GitHub responses (CHB-134). This script runs it from `make check` and CI, so a change that breaks
a guard fails a check the PR shows. Steps:
1. Terraform must be the version in `infra/github/.terraform-version`.
2. `terraform init -backend=false` and `terraform test`, with a scratch TF_DATA_DIR so the checkout
   gets no `.terraform` folder, and with every GitHub and Google credential removed from the
   environment, so the test cannot reach a real account.
`init` downloads the pinned provider from the public registry: it needs network, not credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = "infra/github"
CREDENTIAL_VARIABLES = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_OWNER",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
)
Runner = Callable[[Sequence[str], Path, Mapping[str, str]], "subprocess.CompletedProcess[str]"]


def run_command(command: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=dict(env), text=True, check=False)  # noqa: S603


def capture_command(command: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=dict(env), text=True, capture_output=True, check=False)  # noqa: S603


def scrubbed_env(environ: Mapping[str, str], data_dir: str) -> dict[str, str]:
    """The environment of the test run: no credentials, a scratch data dir, no prompts."""
    env = {
        name: value
        for name, value in environ.items()
        if name not in CREDENTIAL_VARIABLES and not name.startswith("TF_VAR_")
    }
    env["TF_DATA_DIR"] = data_dir
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    return env


def pinned_version(root: Path) -> str:
    return (root / TF_DIR / ".terraform-version").read_text(encoding="utf-8").strip()


def installed_version(root: Path, env: Mapping[str, str], capture: Runner = capture_command) -> str | None:
    """The version `terraform` reports in infra/github, or None when it is missing or unreadable."""
    try:
        done = capture(["terraform", "version", "-json"], root / TF_DIR, env)
    except FileNotFoundError:
        return None
    try:
        return json.loads(done.stdout)["terraform_version"]
    except (ValueError, KeyError, TypeError):
        return None


def main(
    root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    run: Runner = run_command,
    capture: Runner = capture_command,
) -> int:
    pinned = pinned_version(root)
    with tempfile.TemporaryDirectory(prefix="chb-guard-tests-") as data_dir:
        env = scrubbed_env(os.environ if environ is None else environ, data_dir)
        found = installed_version(root, env, capture)
        if found != pinned:
            print(
                f"::error::Terraform {pinned} is required (infra/github/.terraform-version), found {found or 'none'}. "
                f"Fix: tfenv install {pinned} (CHB-158)."
            )
            return 1
        for command in (
            ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
            ["terraform", "test", "-no-color"],
        ):
            if run(command, root / TF_DIR, env).returncode != 0:
                print(f"::error::`{' '.join(command)}` failed. A guard in infra/github/guards.tf is broken (CHB-158).")
                return 1
    print("every Terraform guard test passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
