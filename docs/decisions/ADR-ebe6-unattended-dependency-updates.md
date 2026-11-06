<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-ebe6: Unattended dependency updates](#adr-ebe6-unattended-dependency-updates)
  - [Context](#context)
  - [Decision](#decision)
    - [Dependabot (`.github/dependabot.yml`)](#dependabot-githubdependabotyml)
    - [Release age enforced outside Dependabot](#release-age-enforced-outside-dependabot)
    - [Supply chain checks in CI](#supply-chain-checks-in-ci)
    - [Lockfile-only installs](#lockfile-only-installs)
    - [Auto-merge (`.github/workflows/dependabot-auto-merge.yml`)](#auto-merge-githubworkflowsdependabot-auto-mergeyml)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-ebe6: Unattended dependency updates

- Status: accepted
- Date: 2026-11-06

## Context

Dependabot opened one PR per ecosystem and directory, at random times, with no release-age delay beyond its 3-day default.
Every PR waited for a human.
ADR-0faf defers auto-merge to low-risk tickets that pass every check.
Dependency updates are a separate category: a bot writes them, their risk depends on the version jump, and CI measures it.

Supply chain attacks often publish a malicious version and get caught within days.
A release-age delay keeps those versions out.

## Decision

### Dependabot (`.github/dependabot.yml`)

- Ecosystems: `github-actions`, `uv` (`backend/`), `npm` (`frontend/`, `e2e/`), `docker` (`Dockerfile`), `docker-compose` (`compose.yaml`).
- Schedule: weekly, Monday 06:00 Europe/Madrid. At most 5 open PRs per block.
- Versioning: `increase` for npm and uv. This is an app: the manifest floor follows the lockfile.
- Cooldown: 7 days, 14 for majors. GitHub Actions, Docker and Docker Compose support only `default-days`.
- Groups:
  - Coupled packages move together: React and its types, Vite and its plugins, Vitest and its test libraries, FastAPI with Starlette and pydantic.
  - Groups take minor and patch updates only.
  - A major gets its own PR, or a `-major` group for coupled packages.
  - uv does not support `groups.dependency-type`, so backend groups use name patterns.
  - ruff and uvicorn are 0.x and stay out of groups. A 0.x minor needs a human and must not block a group.
  - `@playwright/test` has its own group (see the Playwright image section below).
- Ignore rules carry a comment with the reason and the revisit trigger.

### Release age enforced outside Dependabot

| Tool | Setting | Where it applies |
|---|---|---|
| npm | `min-release-age=7` in `frontend/.npmrc` and `e2e/.npmrc` | `npm install` with npm 11.10 or later |
| uv | `exclude-newer = "7 days"` in `backend/pyproject.toml` | `uv lock`; `uv.lock` records the span `P7D` |
| CI | `scripts/check_release_age.py` | `backend` job: every `uv.lock` upload time. `frontend` and `e2e` jobs: new `package-lock.json` entries against the npm registry |

The settings apply at lock time, not at install time.
`npm ci` installed a version published one day earlier with the window set.
`uv sync --locked` and `--frozen` accepted a lock with fresh uploads.
The CI script closes that gap.

npm reads `min-release-age` in days, as an integer. `7d` fails with `Invalid time value`.
Node 24 ships npm 11.19.1, in CI and in the `node:24-alpine` image. `make setup` fails with npm older than 11.10.

To let a fresh security fix in, add the package to `min-release-age-exclude[]` in `.npmrc` or to `exclude-newer-package` in `pyproject.toml`.
Add a comment that says when to remove it.
The CI script reads the same exemptions.

### Supply chain checks in CI

These run as steps of the `backend`, `frontend` and `e2e` jobs, so auto-merge waits for them.

- `npm audit signatures` after each `npm ci`: registry signatures and attestations must verify.
- `danielroe/provenance-action` v0.2.0, pinned by commit: fails when a changed lockfile entry loses npm provenance, trusted publisher or staged publishing compared with the base branch.
  It has no runtime dependencies and calls only `registry.npmjs.org`.
- `scripts/check_playwright_image.py`: the smoke test image in `deploy.yml` must match `@playwright/test` in `e2e/package-lock.json` and carry a digest pin.
  Dependabot does not update `container:` images in workflows. A Playwright bump fails this check until a person moves the image in the same PR.

### Lockfile-only installs

- CI, `Dockerfile`, `compose.yaml` and `make setup` use `npm ci` and `uv sync --locked`.
- `uv run` takes `--locked`, so it never rewrites `uv.lock`.

### Auto-merge (`.github/workflows/dependabot-auto-merge.yml`)

- Trigger: `pull_request`, not `pull_request_target`. The job runs only when the PR author is `dependabot[bot]`.
- `dependabot/fetch-metadata` classifies every dependency in the PR:
  - Safe: patch and minor updates from 1.0 up, and GitHub Actions commit bumps without a version.
  - Needs a human: majors, minors of 0.x versions, Playwright, and updates without a known type.
- Safe PRs get auto-merge (squash) and an approval from a GitHub App token.
- The workflow enables auto-merge only when the `main` ruleset requires `backend`, `frontend` and `e2e`.
  Without required checks, auto-merge would merge before CI.
- Labels: `dependencies:auto-merge`, `dependencies:needs-human`, `dependencies:safe` (safe, but a person merges).

Auto-merge needs the App token.
Events created with `GITHUB_TOKEN` start no new workflow runs, except `workflow_dispatch` and `repository_dispatch`.
A merge done with `GITHUB_TOKEN` would not run CI on `main`, and the deploy listens to CI.
GitHub App tokens do not have this limit.
Runs triggered by Dependabot see only Dependabot secrets, so the App credentials go there.
Until the App exists, safe PRs get `dependencies:safe` and a person merges them.

## Consequences

- Fewer PRs: one per group and week, plus majors and 0.x updates.
- Most backend updates need a human, because fastapi, uvicorn and ruff are 0.x.
- Adding `exclude-newer` re-locked the backend to versions at least 7 days old. Nine packages moved back one release, and the ruff floor moved from 0.17.4 to 0.17.3.
- A security fix younger than 7 days fails the release-age check until a person adds an exemption.
  Dependabot cooldown does not delay security updates.
- Unattended merge starts only when three things exist: the GitHub App with its Dependabot secrets, the `main` ruleset with required checks, and the `allow auto-merge` repository setting.
  If the ruleset requires an approval, the App approval must count, or the ruleset needs a rule for Dependabot PRs.
- When an update breaks something, the fix includes a test that catches the break. See `docs/knowledge/dependency-update-breaks-get-a-test.md`.
- Findings from the setup: `docs/knowledge/release-age-applies-at-lock-time.md`.
