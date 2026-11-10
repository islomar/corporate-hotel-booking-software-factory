#!/usr/bin/env python3
"""Run the Terraform guard tests of infra/github and infra/gcp without credentials (CHB-158, CHB-81).

`infra/github/tests/guards.tftest.hcl` checks every precondition in `guards.tf` against stubbed
GitHub responses (CHB-134). `infra/gcp/tests/guards.tftest.hcl` does the same for the CI
identities, with a mocked Google provider (CHB-81). This script runs both from `make check` and
CI, so a change that breaks a guard fails a check the PR shows. Per folder:
1. Terraform must be the version in the folder's `.terraform-version`.
2. `terraform init -backend=false` and `terraform test`, with a scratch TF_DATA_DIR so the checkout
   gets no `.terraform` folder, and with every GitHub and Google credential removed from the
   environment, so the test cannot reach a real account.
`init` downloads the pinned provider from the public registry: it needs network, not credentials.

Then one static check, which Terraform cannot do: the planner service account and its pool may be
named only in `planner.tf` and `guards.tf`. A role bound to the planner anywhere else would skip
the allowlist guard.
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
TF_DIRS = (TF_DIR, "infra/gcp")
PLANNER_FILES = ("planner.tf", "guards.tf")
PLANNER_REFERENCES = ("google_service_account.planner", "google_iam_workload_identity_pool.github_pr")
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


def pinned_version(root: Path, tf_dir: str = TF_DIR) -> str:
    return (root / tf_dir / ".terraform-version").read_text(encoding="utf-8").strip()


def installed_version(
    root: Path, env: Mapping[str, str], capture: Runner = capture_command, tf_dir: str = TF_DIR
) -> str | None:
    """The version `terraform` reports in `tf_dir`, or None when it is missing or unreadable."""
    try:
        done = capture(["terraform", "version", "-json"], root / tf_dir, env)
    except FileNotFoundError:
        return None
    try:
        return json.loads(done.stdout)["terraform_version"]
    except (ValueError, KeyError, TypeError):
        return None


def stray_planner_references(root: Path) -> list[str]:
    """Files of infra/gcp, other than the planner's own, that name the planner or its pool."""
    found = []
    for path in sorted((root / "infra/gcp").glob("*.tf")):
        if path.name in PLANNER_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if any(reference in text for reference in PLANNER_REFERENCES):
            found.append(f"infra/gcp/{path.name}")
    return found


def main(
    root: Path = ROOT,
    environ: Mapping[str, str] | None = None,
    run: Runner = run_command,
    capture: Runner = capture_command,
) -> int:
    stray = stray_planner_references(root)
    if stray:
        print(
            f"::error::{', '.join(stray)} name the planner service account or its pool. "
            f"Bind planner roles in infra/gcp/planner.tf only, so guards.tf checks them (CHB-81)."
        )
        return 1
    with tempfile.TemporaryDirectory(prefix="chb-guard-tests-") as data_dir:
        env = scrubbed_env(os.environ if environ is None else environ, data_dir)
        for tf_dir in TF_DIRS:
            pinned = pinned_version(root, tf_dir)
            found = installed_version(root, env, capture, tf_dir)
            if found != pinned:
                print(
                    f"::error::Terraform {pinned} is required ({tf_dir}/.terraform-version), found {found or 'none'}. "
                    f"Fix: tfenv install {pinned} (CHB-158)."
                )
                return 1
            for command in (
                ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                ["terraform", "test", "-no-color"],
            ):
                if run(command, root / tf_dir, env).returncode != 0:
                    print(f"::error::`{' '.join(command)}` failed in {tf_dir}. A guard there is broken (CHB-158).")
                    return 1
    print("every Terraform guard test passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
