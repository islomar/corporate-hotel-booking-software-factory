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
    - [Supply-chain check instead of code-owner review (CHB-90)](#supply-chain-check-instead-of-code-owner-review-chb-90)
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
- The workflow enables auto-merge only when the `main` ruleset requires `backend`, `frontend`, `e2e` and `supply-chain`.
  Without required checks, auto-merge would merge before CI.
- Labels: `dependencies:auto-merge`, `dependencies:needs-human`, `dependencies:safe` (safe, but auto-merge is off: a person merges).

Auto-merge needs the App token.
Events created with `GITHUB_TOKEN` start no new workflow runs, except `workflow_dispatch` and `repository_dispatch`.
A merge done with `GITHUB_TOKEN` would not run CI on `main`, and the deploy listens to CI.
GitHub App tokens do not have this limit.
Runs triggered by Dependabot see only Dependabot secrets, so the App credentials go there.
Until the App exists, safe PRs get `dependencies:safe` and a person merges them.

### Supply-chain check instead of code-owner review (CHB-90)

CODEOWNERS made the owner the code owner of every manifest and lockfile.
The `main` ruleset requires code-owner review, and a GitHub App cannot be a code owner.
Every Dependabot PR therefore waited for the owner, auto-merge or not.

- Manifests, lockfiles, `Dockerfile`, `compose.yaml`, `.nvmrc` and `.python-version` leave CODEOWNERS.
  `.npmrc` stays: it holds the release-age window and its exemptions.
- A required check `supply-chain` (`.github/workflows/supply-chain.yml`, `scripts/check_supply_chain.py`) takes the code owner's place for these files.
- Changed files come from `git diff --no-renames --name-only -z base...head`.
  Without `-z`, git C-quotes paths with non-ASCII characters, and a quoted `"dép/package.json"` would not match `package.json`.
- The file list in the script is wider than the old CODEOWNERS list.
  It adds resolver settings (`uv.toml`, `pip.conf`, `pnpm-workspace.yaml`, `.pnpmfile.cjs`, `.yarnrc*`, `bunfig.toml`), other lockfiles and manifests, compose and Dockerfile variants, `.node-version`.
- The check passes when no supply-chain file changed, or the owner's latest deciding review approves the head commit, or the PR is Dependabot's:
  - the PR author is `dependabot[bot]`;
  - every commit has author `dependabot[bot]`, committer `web-flow` and a signature GitHub verified.
    GitHub records Dependabot commits this way (PRs #1 to #4). The committer is `web-flow`, not `dependabot[bot]`.
  - the API lists every commit of the PR (`pulls/{n}/commits` stops at 250).
- A change to release-age settings needs the owner even from Dependabot:
  `min-release-age*` keys in any `.npmrc`, top-level `exclude-newer*` keys in any `uv.toml`, `exclude-newer*` keys under `[tool.uv]` in any `pyproject.toml`.
  The script compares parsed settings, so a dependency floor bump in `pyproject.toml` does not count.
  A file that does not parse, or where `tool` or `tool.uv` is not a table, counts as a change.
- Data comes from git and the GitHub API. PR title, body and labels are never read.
- Triggers: `pull_request` and `pull_request_review` (`submitted`, `dismissed`), so an owner approval re-runs it.
  Both report a check run named `supply-chain` on the head commit. A per-PR concurrency group cancels the older run.
- The workflow and the script run from the PR's merge commit. Their integrity rests on CODEOWNERS: both have their own owner lines.
- A second ruleset, `dependabot-branches` (`infra/github/ruleset.tf`), restricts creation and update of `refs/heads/dependabot/**/*`.
  Its only bypass actor is Dependabot: `actor_type = "Integration"`, `actor_id = 29110` (`gh api /apps/dependabot`), mode `always`.
  GitHub lists Dependabot as an eligible bypass actor for rulesets.
  Without this ruleset, any actor with contents write (the agent App, the owner) could push to a Dependabot branch.
  Whether such a push could carry author `dependabot[bot]`, committer `web-flow` and a verified signature is unverified. The ruleset makes the question moot.

Consequences:

- Nobody but Dependabot can push to a Dependabot branch. "Update branch" fails there too.
  To refresh a Dependabot PR, comment `@dependabot rebase`. To change one, close it and open a new PR from another branch.
- Deletion stays open: deleting a branch cannot forge a commit, and `delete_branch_on_merge` must still remove merged Dependabot branches.
- GitHub Actions bumps change `.github/workflows/`, which stays in CODEOWNERS. They keep needing the owner.
  Next step, after CHB-86 removes `workflows: write` from the agent App: move `/.github/workflows/` out of CODEOWNERS and add it to the script's supply-chain set.
  Without that second part, Actions pins lose all protection.
- Unverified: that GitHub uses the latest `supply-chain` check run when the two events both report one. Test it on the first owner-approved supply-chain PR: the merge box must turn green after the approval.
- Owner-authored PRs cannot pass through owner approval, because GitHub blocks self-approval. The ruleset's required approval blocks them anyway.

## Consequences

- Fewer PRs: one per group and week, plus majors and 0.x updates.
- Most backend updates need a human, because fastapi, uvicorn and ruff are 0.x.
- Adding `exclude-newer` re-locked the backend to versions at least 7 days old. Nine packages moved back one release, and the ruff floor moved from 0.17.4 to 0.17.3.
- A security fix younger than 7 days fails the release-age check until a person adds an exemption.
  Dependabot cooldown does not delay security updates.
- Unattended merge starts only when these exist:
  the GitHub App with its Dependabot secrets, the `main` ruleset requiring `backend`, `frontend`, `e2e` and `supply-chain`, and the `allow auto-merge` setting.
  The ruleset requires 1 approval. The App approval must count (CHB-88).
  A user report says an App approval with contents read shows as "approved with read-only permissions" and does not count.
  The auto-merge token has contents write.
- When an update breaks something, the fix includes a test that catches the break. See `docs/knowledge/dependency-update-breaks-get-a-test.md`.
- Findings from the setup: `docs/knowledge/release-age-applies-at-lock-time.md`.
