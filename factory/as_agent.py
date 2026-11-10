#!/usr/bin/env python3
"""Run a command as the factory's GitHub App (ADR-d7b7).

Usage: factory/bin/as-agent <command> [args...]
Example: factory/bin/as-agent gh pr create --fill

The App's private key is read at run time from the 1Password vault chb-factory, with the
service account token from the GNOME keyring (op_service_account.py), and never written to
the repository. The service account token goes only to that `op` call, never to the
command. Without the token the script exits 3 and runs nothing. The script mints a one-hour installation token scoped to this repository,
then runs the command with:
- GH_TOKEN set, so `gh` acts as the App;
- git author and committer set to the App's bot user;
- git pushing over HTTPS with the token, through `gh auth git-credential`.

The token never reaches stdout. Only the command's own output does.

Auto-merge (CHB-120): after `gh pr create` (or `gh pr new`) opens a PR, the script runs
`gh pr merge --auto --squash <PR URL>` with the same token. It skips that step for a draft,
for a base branch other than main (a stacked PR), and when AGENT_NO_AUTO_MERGE=1. For
`gh pr create` stdout is captured to read the PR URL and printed again, so `gh` does not
prompt: pass --title and --body (or --fill). If the PR opens but auto-merge fails, the
script exits 5 (EXIT_NO_AUTO_MERGE) and prints the command to retry. Do not open the PR again.

Environment overrides:
- AGENT_APP_CLIENT_ID: the App's client ID (public).
- AGENT_NO_AUTO_MERGE=1: open a PR without auto-merge.
- AGENT_APP_KEY_COMMAND: command that prints the private key, instead of the default
  `op` call. It runs without the service account token: an explicit opt-in to another
  key source, for example the owner's desktop 1Password (with its pop-up).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python3 -I does not add it

import op_service_account  # noqa: E402

CLIENT_ID = "Iv23li3OLZUlWY7ZMLdr"
APP_SLUG = "chb-factory-agent"
REPOSITORY = "islomar/corporate-hotel-booking-software-factory"
KEY_COMMAND = f"op document get chb-factory-agent.2026-11-06.private-key --vault {op_service_account.VAULT}"
KEY_TIMEOUT_SECONDS = 60
EXIT_NO_KEY = 3
EXIT_NO_AUTO_MERGE = 5
OPT_OUT_VARIABLE = "AGENT_NO_AUTO_MERGE"
PR_URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
API = "https://api.github.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_signing_input(client_id: str, now: int) -> str:
    """Header and claims of a GitHub App JWT. iat is backdated 60 s for clock drift."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iat": now - 60, "exp": now + 540, "iss": client_id}
    return ".".join(_b64url(json.dumps(p, separators=(",", ":")).encode()) for p in (header, claims))


def noreply_email(login: str, user_id: int) -> str:
    return f"{user_id}+{login}@users.noreply.github.com"


def agent_env(base: Mapping[str, str], token: str, bot_login: str, bot_id: int) -> dict[str, str]:
    dropped = {"GITHUB_TOKEN", op_service_account.TOKEN_VARIABLE}
    env = {k: v for k, v in base.items() if k not in dropped}
    email = noreply_email(bot_login, bot_id)
    env |= {
        "GH_TOKEN": token,
        "GIT_AUTHOR_NAME": bot_login,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": bot_login,
        "GIT_COMMITTER_EMAIL": email,
    }
    config = [
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        # An empty helper drops helpers from other config files for github.com.
        ("credential.https://github.com.helper", ""),
        ("credential.https://github.com.helper", "!gh auth git-credential"),
    ]
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for i, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def _sign(signing_input: str, private_key: bytes) -> str:
    with tempfile.NamedTemporaryFile(mode="wb") as key_file:
        os.chmod(key_file.name, 0o600)
        key_file.write(private_key)
        key_file.flush()
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_file.name],
            input=signing_input.encode(),
            capture_output=True,
            check=True,
        ).stdout
    return f"{signing_input}.{_b64url(signature)}"


def _api(method: str, path: str, auth: str, body: object | None = None) -> dict[str, object]:
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


class AppKeyError(Exception):
    """The App key could not be read. The message never holds the key or a token."""


def read_private_key(
    key_command: list[str],
    base_env: Mapping[str, str],
    token_reader: Callable[[], str] = op_service_account.read_token,
    run: Callable[..., Any] = subprocess.run,
) -> bytes:
    """The App key, from the default `op` call run with the service account token in its environment only."""
    try:
        key = op_service_account.run_op(key_command, base_env, token_reader, run)
    except op_service_account.ServiceAccountError as error:
        raise AppKeyError(str(error)) from None
    if not key.strip():
        raise AppKeyError(f"{key_command[0]} printed no key")
    return key


def read_private_key_with_override(
    command: str, base_env: Mapping[str, str], run: Callable[..., Any] = subprocess.run
) -> bytes:
    """The App key from AGENT_APP_KEY_COMMAND. It never gets the service account token."""
    argv = shlex.split(command)
    env = {k: v for k, v in base_env.items() if k != op_service_account.TOKEN_VARIABLE}
    try:
        result = run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=KEY_TIMEOUT_SECONDS, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AppKeyError(f"could not run {argv[0]}: {type(error).__name__}") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise AppKeyError(f"AGENT_APP_KEY_COMMAND exited {result.returncode} or printed no key")
    return result.stdout


def installation_token(client_id: str, private_key: bytes) -> str:
    app_jwt = _sign(jwt_signing_input(client_id, int(time.time())), private_key)
    installation = _api("GET", f"/repos/{REPOSITORY}/installation", f"Bearer {app_jwt}")
    token = _api(
        "POST",
        f"/app/installations/{installation['id']}/access_tokens",
        f"Bearer {app_jwt}",
        {"repositories": [REPOSITORY.split("/")[1]]},
    )
    return str(token["token"])


def _is_pr_create(argv: list[str]) -> bool:
    return len(argv) >= 3 and os.path.basename(argv[0]) == "gh" and argv[1] == "pr" and argv[2] in ("create", "new")


def _option(args: list[str], *names: str) -> str | None:
    """The value of the last `--name value`, `--name=value` or `-n value` in args."""
    value = None
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                value = args[i + 1]
            elif name.startswith("--") and arg.startswith(name + "="):
                value = arg[len(name) + 1 :]
    return value


def auto_merge_skip_reason(argv: list[str], env: Mapping[str, str]) -> str | None:
    """Why this `gh pr create` gets no auto-merge, or None when it gets it."""
    if env.get(OPT_OUT_VARIABLE) == "1":
        return f"{OPT_OUT_VARIABLE}=1"
    args = argv[3:]
    if "--draft" in args or "-d" in args:
        return "draft PR"
    base = _option(args, "--base", "-B")
    if base not in (None, "main"):
        return f"base {base} is not main: only main has the review ruleset"
    if "--web" in args or "-w" in args or "--dry-run" in args:
        return "no PR opened from the command line"
    return None


def run(
    argv: list[str],
    env: Mapping[str, str],
    runner: Callable[..., Any] = subprocess.run,
    out: Any = None,
    err: Any = None,
) -> int:
    """Run argv with env. After `gh pr create`, enable auto-merge (squash) on the new PR."""
    out = out or sys.stdout
    err = err or sys.stderr
    if not _is_pr_create(argv):
        return runner(argv, env=env, check=False).returncode
    skip = auto_merge_skip_reason(argv, env)
    if skip:
        print(f"as-agent: auto-merge not enabled: {skip}.", file=err)
        return runner(argv, env=env, check=False).returncode
    created = runner(argv, env=env, check=False, stdout=subprocess.PIPE, text=True)
    output = created.stdout or ""
    out.write(output)
    out.flush()
    if created.returncode != 0:
        return created.returncode
    urls = PR_URL.findall(output)
    if not urls:
        print("as-agent: no PR URL in the output of gh pr create. Auto-merge not enabled.", file=err)
        return EXIT_NO_AUTO_MERGE
    merge = ["gh", "pr", "merge", "--auto", "--squash", urls[-1]]
    if runner(merge, env=env, check=False).returncode != 0:
        print(
            f"as-agent: PR {urls[-1]} is open, but auto-merge failed. Do not open it again. "
            f"Retry: factory/bin/as-agent {shlex.join(merge)}",
            file=err,
        )
        return EXIT_NO_AUTO_MERGE
    print(f"as-agent: auto-merge (squash) enabled on {urls[-1]}.", file=err)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    override = os.environ.get("AGENT_APP_KEY_COMMAND")
    try:
        if override:
            private_key = read_private_key_with_override(override, os.environ)
        else:
            private_key = read_private_key(shlex.split(KEY_COMMAND), os.environ)
    except AppKeyError as error:
        print(f"as-agent: no App key: {error}. STOP: do not use another identity. Report to the owner.", file=sys.stderr)
        return EXIT_NO_KEY
    token = installation_token(os.environ.get("AGENT_APP_CLIENT_ID", CLIENT_ID), private_key)
    bot_login = f"{APP_SLUG}[bot]"
    bot = _api("GET", f"/users/{urllib.request.quote(bot_login)}", f"token {token}")
    env = agent_env(os.environ, token, bot_login, int(str(bot["id"])))
    return run(argv, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
