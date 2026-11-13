#!/usr/bin/env python3
"""Run the Playwright tests with everything they need. `make e2e` calls this script.

The host needs Docker and Python 3, nothing else: `npm ci` and Playwright run in the pinned
Playwright image of the e2e job in .github/workflows/ci.yml (PLAYWRIGHT_IMAGE, read from there),
as the host user, so no file in e2e/ is owned by root. Steps:
1. Docker must answer.
2. `npm ci` in e2e/, inside the image, when node_modules is missing or older than package-lock.json.
3. The app:
   - APP_URL set: test that URL. Never start a container.
   - APP_URL unset: reuse the app on http://localhost:8080 if /api/health answers.
     Otherwise build and start the production container, test it, and remove it.
4. Playwright runs in the image, on the host network. Its exit code is the exit code of this script.

Playwright runs from e2e/node_modules, never through a bare `npx`: without node_modules,
`npx playwright` downloads an unpinned package from the registry.
"""

from __future__ import annotations

import enum
import http.client
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
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
PLAYWRIGHT = "./node_modules/.bin/playwright"  # a path inside the image
DEFAULT_APP_URL = "http://localhost:8080"
COMPOSE = ["docker", "compose", "--profile", "prod"]
HEALTH_TIMEOUT_SECONDS = 60


class E2eError(Exception):
    """A problem the developer can fix. The message says how."""


class Action(enum.Enum):
    REUSE = "reuse"
    START = "start"


class Probe(enum.Enum):
    APP = "app"  # /api/health answers {"status": "ok"}
    OTHER = "other"  # something answers, but it is not this app
    DOWN = "down"  # nothing answers


def playwright_image(workflow: str) -> str:
    """The PLAYWRIGHT_IMAGE of a workflow file, which must be pinned by digest."""
    match = re.search(r"^\s*PLAYWRIGHT_IMAGE:\s*(\S+)\s*$", workflow, re.MULTILINE)
    if match is None:
        raise E2eError(f"No PLAYWRIGHT_IMAGE found in {CI_WORKFLOW.relative_to(ROOT)}.")
    if "@sha256:" not in match[1]:
        raise E2eError(f"PLAYWRIGHT_IMAGE {match[1]} is not pinned by digest (@sha256:...).")
    return match[1]


def ci_image() -> str:
    return playwright_image(CI_WORKFLOW.read_text())


def docker_command(image: str, command: list[str], *, uid: int, gid: int) -> list[str]:
    """`docker run` of a command in the image, with the flags of the e2e job in ci.yml."""
    return [
        "docker", "run", "--rm", "--network", "host", "--ipc", "host",
        "--user", f"{uid}:{gid}", "-e", "HOME=/tmp", "-e", "CI",
        "-e", f"APP_URL={os.environ.get('APP_URL') or DEFAULT_APP_URL}",
        "-v", f"{E2E}:/e2e", "-w", "/e2e", image, *command,
    ]  # fmt: skip


def probe(url: str) -> Probe:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/health", timeout=5) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        error.close()
        return Probe.OTHER
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return Probe.DOWN
    except (http.client.HTTPException, ValueError):
        return Probe.OTHER
    return Probe.APP if isinstance(body, dict) and body.get("status") == "ok" else Probe.OTHER


def choose(url: str, *, explicit: bool, found: Probe) -> Action:
    """Return what to do with the app, or raise E2eError with the fix."""
    if found is Probe.APP:
        return Action.REUSE
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
    return Action.START


def deps_are_stale(e2e: Path) -> bool:
    installed = e2e / "node_modules/.package-lock.json"
    if not installed.exists():
        return True
    return installed.stat().st_mtime < (e2e / "package-lock.json").stat().st_mtime


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def in_image(image: str, command: list[str]) -> list[str]:
    return docker_command(image, command, uid=os.getuid(), gid=os.getgid())


def install_deps(image: str) -> None:
    if deps_are_stale(E2E):
        say("Installing e2e dependencies (npm ci in the Playwright image)")
        subprocess.run(in_image(image, ["npm", "ci"]), check=True)


def check_docker() -> None:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True)
    except OSError:
        raise E2eError(
            "Docker is not installed. `make e2e` runs npm and Playwright in a Docker image."
        ) from None
    except subprocess.CalledProcessError:
        raise E2eError(
            "Docker is not running. `make e2e` runs npm and Playwright in a Docker image.\n"
            "Start Docker and run `make e2e` again."
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


def run(args: list[str] | None = None) -> int:
    check_docker()
    image = ci_image()
    install_deps(image)

    explicit = bool(os.environ.get("APP_URL"))
    url = os.environ.get("APP_URL") or DEFAULT_APP_URL
    if not url.startswith(("http://", "https://")):
        raise E2eError(f"APP_URL={url} must start with http:// or https://.")
    action = choose(url, explicit=explicit, found=probe(url))
    started = False
    signal.signal(signal.SIGTERM, _terminate)
    try:
        if action is Action.START:
            started = True
            start_app(url)
        else:
            say(f"Testing the app that already runs on {url}")
        say(f"Running Playwright in {image}")
        result = subprocess.run(in_image(image, [PLAYWRIGHT, "test", *(args or [])]), cwd=ROOT)
        return result.returncode
    finally:
        if started:
            # A second Ctrl-C must not cut the teardown short and leave the container running.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            stop_app()


def main() -> int:
    try:
        return run(sys.argv[1:])
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
