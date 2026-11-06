#!/usr/bin/env python3
"""Run a command as the factory's GitHub App (ADR-d7b7).

Usage: factory/bin/as-agent <command> [args...]
Example: factory/bin/as-agent gh pr create --fill

The App's private key is read from 1Password at run time and never written to the
repository. The script mints a one-hour installation token scoped to this repository,
then runs the command with:
- GH_TOKEN set, so `gh` acts as the App;
- git author and committer set to the App's bot user;
- git pushing over HTTPS with the token, through `gh auth git-credential`.

The token never reaches stdout. Only the command's own output does.

Environment overrides:
- AGENT_APP_CLIENT_ID: the App's client ID (public).
- AGENT_APP_KEY_COMMAND: command that prints the private key.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Mapping

CLIENT_ID = "Iv23li3OLZUlWY7ZMLdr"
APP_SLUG = "chb-factory-agent"
REPOSITORY = "islomar/corporate-hotel-booking-software-factory"
KEY_COMMAND = "op document get chb-factory-agent.2026-11-06.private-key"
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
    env = {k: v for k, v in base.items() if k != "GITHUB_TOKEN"}
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


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    key_command = shlex.split(os.environ.get("AGENT_APP_KEY_COMMAND", KEY_COMMAND))
    private_key = subprocess.run(key_command, capture_output=True, check=True).stdout
    token = installation_token(os.environ.get("AGENT_APP_CLIENT_ID", CLIENT_ID), private_key)
    bot_login = f"{APP_SLUG}[bot]"
    bot = _api("GET", f"/users/{urllib.request.quote(bot_login)}", f"token {token}")
    env = agent_env(os.environ, token, bot_login, int(str(bot["id"])))
    return subprocess.run(argv, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
