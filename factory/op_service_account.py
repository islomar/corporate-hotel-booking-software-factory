"""The 1Password service account of the factory agents (CHB-97).

The service account chb-factory-agent can only read the vault chb-factory, which holds
the GitHub App key and the Jira token. Its token lives in the GNOME keyring. as-agent
and factory/bin/jira read it on every call and pass it to their own `op` subprocess
only, as OP_SERVICE_ACCOUNT_TOKEN. `op` then never asks the owner's desktop app, so no
approval pop-up appears.

There is no fallback to the owner's desktop 1Password: it would bring back the pop-ups
and the owner's full 1Password rights.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from typing import Any

VAULT = "chb-factory"
KEYRING_LOOKUP = ["secret-tool", "lookup", "service", "chb-factory", "key", "op-service-account-token"]
TOKEN_VARIABLE = "OP_SERVICE_ACCOUNT_TOKEN"
# A Connect server in the environment takes precedence over the service account in `op`.
DROPPED_VARIABLES = ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN")
TIMEOUT_SECONDS = 30  # a locked keyring can open an unlock dialog and wait
OP_TIMEOUT_SECONDS = 60

Runner = Callable[..., Any]


class ServiceAccountError(Exception):
    """The service account token is not in the keyring, or the keyring cannot be read."""


def read_token(run: Runner = subprocess.run) -> str:
    """The service account token from the GNOME keyring. Never part of an error message."""
    hint = f"store it with: secret-tool store --label=... {' '.join(KEYRING_LOOKUP[2:])}"
    try:
        result = run(
            KEYRING_LOOKUP, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        raise ServiceAccountError("secret-tool is not installed (package libsecret-tools)") from None
    except subprocess.TimeoutExpired:
        raise ServiceAccountError(f"secret-tool did not answer in {TIMEOUT_SECONDS} s (is the keyring locked?)") from None
    except OSError as error:
        raise ServiceAccountError(f"could not run secret-tool: {type(error).__name__}") from None
    if result.returncode != 0:
        raise ServiceAccountError(
            f"no 1Password service account token in the keyring (secret-tool exited {result.returncode}; "
            f"keyring locked or item absent; {hint})"
        )
    token = result.stdout.decode().strip()
    if not token:
        raise ServiceAccountError(f"the keyring item for the 1Password service account token is empty ({hint})")
    return token


def op_env(base: Mapping[str, str], token: str) -> dict[str, str]:
    """Environment for one `op` subprocess: `base` plus the token. `base` is not changed."""
    env = {key: value for key, value in base.items() if key not in DROPPED_VARIABLES}
    env[TOKEN_VARIABLE] = token
    return env


def run_op(
    command: list[str],
    base_env: Mapping[str, str],
    token_reader: Callable[[], str] = read_token,
    run: Runner = subprocess.run,
    timeout: int = OP_TIMEOUT_SECONDS,
) -> bytes:
    """Stdout of `command` (an `op` call) run with the token in its environment only.

    Reads the token first: without it, `command` never runs. No error message holds output.
    """
    env = op_env(base_env, token_reader())
    try:
        result = run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceAccountError(f"could not run {command[0]}: {type(error).__name__}") from None
    if result.returncode != 0:
        raise ServiceAccountError(
            f"{command[0]} exited {result.returncode} (service account token expired or revoked?)"
        )
    return result.stdout
