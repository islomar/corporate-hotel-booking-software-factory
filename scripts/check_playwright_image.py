#!/usr/bin/env python3
"""Fail when the Playwright container image in deploy.yml differs from @playwright/test.

Dependabot bumps @playwright/test in e2e/package-lock.json but not the `container:` image
of the smoke job. A browser build that differs from the test runner breaks the smoke test
after the deploy, so this check stops the mismatch in the pull request instead.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "e2e/package-lock.json"
WORKFLOW = ROOT / ".github/workflows/deploy.yml"
IMAGE = re.compile(
    r"mcr\.microsoft\.com/playwright:v(?P<version>[^\s@-]+)-(?P<distro>[a-z]+)(?P<digest>@sha256:[0-9a-f]{64})?"
)


def main() -> int:
    locked = json.loads(LOCKFILE.read_text())["packages"]["node_modules/@playwright/test"][
        "version"
    ]
    images = list(IMAGE.finditer(WORKFLOW.read_text()))
    if not images:
        print(
            f"::error file={WORKFLOW.relative_to(ROOT)}::no mcr.microsoft.com/playwright image found"
        )
        return 1
    failed = False
    for image in images:
        tag = f"v{locked}-{image['distro']}"
        if image["version"] != locked:
            failed = True
            print(
                f"::error file={WORKFLOW.relative_to(ROOT)}::Playwright image v{image['version']} "
                f"does not match @playwright/test {locked}. Set the image to {tag} and its digest:\n"
                f"  docker buildx imagetools inspect mcr.microsoft.com/playwright:{tag} --format '{{{{.Manifest.Digest}}}}'"
            )
        elif not image["digest"]:
            failed = True
            print(
                f"::error file={WORKFLOW.relative_to(ROOT)}::pin mcr.microsoft.com/playwright:{tag} by digest"
            )
    if not failed:
        print(f"Playwright image matches @playwright/test {locked}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
