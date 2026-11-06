#!/usr/bin/env python3
"""Run the Playwright tests with everything they need. `make e2e` calls this script.

Steps:
1. Node must match the major version in .nvmrc.
2. `npm ci` in e2e/ when node_modules is missing or older than package-lock.json.
3. The Playwright browser is downloaded when it is missing.
4. The app:
   - APP_URL set: test that URL. Never start a container.
   - APP_URL unset: reuse the app on http://localhost:8080 if /api/health answers.
     Otherwise build and start the production container, test it, and remove it.
5. Playwright runs. Its exit code is the exit code of this script.

The script runs Playwright from e2e/node_modules, never through a bare `npx`: without
node_modules, `npx playwright` downloads an unpinned package from the registry.
"""

from __future__ import annotations

import enum
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import FrameType

ROOT = Path(__file__).resolve().parents[1]
E2E = ROOT / "e2e"
PLAYWRIGHT = E2E / "node_modules/.bin/playwright"
DEFAULT_APP_URL = "http://localhost:8080"
COMPOSE = ["docker", "compose", "--profile", "prod"]
HEALTH_TIMEOUT_SECONDS = 60


class E2eError(Exception):
    """A problem the developer can fix. The message says how."""


class Probe(enum.Enum):
    APP = "app"  # /api/health answers {"status": "ok"}
    OTHER = "other"  # something answers, but it is not this app
    DOWN = "down"  # nothing answers


def node_problem(version: str | None, nvmrc: str) -> str | None:
    required = int(nvmrc.strip().lstrip("v").split(".")[0])
    if version is None:
        return f"Node is not installed. e2e needs Node {required} (.nvmrc): run `nvm install`."
    found = version.strip()
    if int(found.lstrip("v").split(".")[0]) >= required:
        return None
    return (
        f"Node {found} is too old: e2e needs Node {required} (.nvmrc).\n"
        f"Run `nvm use` in the repository root, then `make e2e` again."
    )


def probe(url: str) -> Probe:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/health", timeout=5) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        error.close()
        return Probe.OTHER
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return Probe.DOWN
    except ValueError:
        return Probe.OTHER
    return Probe.APP if isinstance(body, dict) and body.get("status") == "ok" else Probe.OTHER


def choose(url: str, *, explicit: bool, found: Probe) -> str:
    """Return "reuse" or "start", or raise E2eError with the fix."""
    if found is Probe.APP:
        return "reuse"
    if explicit:
        state = "is not this app" if found is Probe.OTHER else "does not answer"
        raise E2eError(
            f"APP_URL={url} {state} (no {{\"status\": \"ok\"}} at {url}/api/health).\n"
            "Start the app there, fix APP_URL, or unset APP_URL to let `make e2e` "
            "start the production container."
        )
    if found is Probe.OTHER:
        raise E2eError(
            f"Port 8080 is used by something else: {url}/api/health does not answer as this app.\n"
            "Find it with `ss -ltnp 'sport = :8080'` (Linux) or `lsof -i :8080` (macOS) and stop "
            "it, or set APP_URL to a running copy of the app."
        )
    return "start"


def deps_are_stale(e2e: Path) -> bool:
    installed = e2e / "node_modules/.package-lock.json"
    if not installed.exists():
        return True
    return installed.stat().st_mtime < (e2e / "package-lock.json").stat().st_mtime


def missing_browser_dirs(dry_run: str) -> list[str]:
    locations = re.findall(r"Install location:\s+(\S.*)", dry_run)
    return [path.strip() for path in locations if not Path(path.strip()).exists()]


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def node_version() -> str | None:
    try:
        return subprocess.run(
            ["node", "--version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def install_deps() -> None:
    if deps_are_stale(E2E):
        say("Installing e2e dependencies (npm ci in e2e/)")
        subprocess.run(["npm", "ci"], cwd=E2E, check=True)
    dry_run = subprocess.run(
        [PLAYWRIGHT, "install", "--dry-run", "chromium"],
        cwd=E2E,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if missing_browser_dirs(dry_run):
        say("Downloading the Playwright browser (chromium, once per Playwright version)")
        subprocess.run([PLAYWRIGHT, "install", "chromium"], cwd=E2E, check=True)


def check_docker() -> None:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True)
    except OSError:
        raise E2eError(
            "Docker is not installed. Install Docker, or set APP_URL to a running app."
        ) from None
    except subprocess.CalledProcessError:
        raise E2eError(
            "Docker is not running, and nothing answers on http://localhost:8080.\n"
            "Start Docker, or start the app another way and set APP_URL."
        ) from None


def start_app(url: str) -> None:
    say("Nothing answers on port 8080: building and starting the production container")
    subprocess.run([*COMPOSE, "up", "--build", "--detach", "app"], cwd=ROOT, check=True)
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while probe(url) is not Probe.APP:
        if time.monotonic() > deadline:
            subprocess.run([*COMPOSE, "logs", "--tail", "50", "app"], cwd=ROOT)
            raise E2eError(
                f"The container did not answer on {url}/api/health within "
                f"{HEALTH_TIMEOUT_SECONDS} s. Its last log lines are above."
            )
        time.sleep(1)
    say(f"App is up on {url}")


def stop_app() -> None:
    say("Removing the production container that `make e2e` started")
    subprocess.run([*COMPOSE, "rm", "--stop", "--force", "app"], cwd=ROOT)


def _terminate(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def run() -> int:
    problem = node_problem(node_version(), (ROOT / ".nvmrc").read_text())
    if problem:
        raise E2eError(problem)
    install_deps()

    explicit = bool(os.environ.get("APP_URL"))
    url = os.environ.get("APP_URL") or DEFAULT_APP_URL
    action = choose(url, explicit=explicit, found=probe(url))
    started = False
    signal.signal(signal.SIGTERM, _terminate)
    try:
        if action == "start":
            check_docker()
            started = True
            start_app(url)
        else:
            say(f"Testing the app that already runs on {url}")
        result = subprocess.run(
            [PLAYWRIGHT, "test", *sys.argv[1:]], cwd=E2E, env={**os.environ, "APP_URL": url}
        )
        return result.returncode
    finally:
        if started:
            stop_app()


def main() -> int:
    try:
        return run()
    except E2eError as error:
        print(f"\nmake e2e: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        command = " ".join(str(part) for part in error.cmd)
        print(f"\nmake e2e: `{command}` failed with exit code {error.returncode}.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
