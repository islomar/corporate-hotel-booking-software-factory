#!/usr/bin/env python3
"""Fail when a Playwright container image in the workflows differs from @playwright/test.

Dependabot bumps @playwright/test in e2e/package-lock.json but not the Playwright images
that the e2e job of ci.yml and the smoke job of deploy.yml run in. A browser build that
differs from the test runner breaks those jobs, so this check stops the mismatch in the
pull request instead. Both workflows must pin the same image digest.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "e2e/package-lock.json"
WORKFLOWS = (ROOT / ".github/workflows/ci.yml", ROOT / ".github/workflows/deploy.yml")
IMAGE = re.compile(
    r"mcr\.microsoft\.com/playwright:v(?P<version>[^\s@-]+)-(?P<distro>[a-z]+)(?P<digest>@sha256:[0-9a-f]{64})?"
)


def errors(locked: str, name: str, text: str) -> list[str]:
    """GitHub error annotations for the Playwright images in one workflow file."""
    images = list(IMAGE.finditer(text))
    if not images:
        return [f"::error file={name}::no mcr.microsoft.com/playwright image found"]
    found = []
    for image in images:
        tag = f"v{locked}-{image['distro']}"
        if image["version"] != locked:
            found.append(
                f"::error file={name}::Playwright image v{image['version']} "
                f"does not match @playwright/test {locked}. Set the image to {tag} and its digest:\n"
                f"  docker buildx imagetools inspect mcr.microsoft.com/playwright:{tag} --format '{{{{.Manifest.Digest}}}}'"
            )
        elif not image["digest"]:
            found.append(f"::error file={name}::pin mcr.microsoft.com/playwright:{tag} by digest")
    return found


def main(lockfile: Path = LOCKFILE, workflows: tuple[Path, ...] = WORKFLOWS) -> int:
    locked = json.loads(lockfile.read_text())["packages"]["node_modules/@playwright/test"][
        "version"
    ]
    found = []
    pinned = set()
    for workflow in workflows:
        name = str(workflow.relative_to(ROOT) if workflow.is_relative_to(ROOT) else workflow)
        text = workflow.read_text()
        found += errors(locked, name, text)
        pinned |= {image[0] for image in IMAGE.finditer(text) if image["digest"]}
    if len(pinned) > 1:
        found.append(
            "::error::the workflows pin different Playwright images, use one: "
            + ", ".join(sorted(pinned))
        )
    for error in found:
        print(error)
    if not found:
        print(f"Playwright images match @playwright/test {locked}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
