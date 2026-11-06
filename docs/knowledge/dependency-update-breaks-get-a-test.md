<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [A dependency update that breaks something gets a test](#a-dependency-update-that-breaks-something-gets-a-test)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# A dependency update that breaks something gets a test

Agreed on 2026-11-06 with the unattended dependency updates (ADR-ebe6).

Rule: when a dependency update breaks the app, the build or a tool, the fix includes an automated test that fails on that break.
CI then catches the next regression of the same kind, and the next update of that dependency can merge without a person.

Examples:

- A new FastAPI version changes a response shape: add an API test that asserts the shape.
- A Vite or plugin update breaks the production build: the e2e job already builds the image; add the assertion that would have failed.
- A Playwright update needs a new browser image: `scripts/check_playwright_image.py` fails on the mismatch.

A fix without a test leaves the next update to a person.
