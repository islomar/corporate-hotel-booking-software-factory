<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [To do](#to-do)
  - [Decisions pending](#decisions-pending)
  - [Open](#open)
    - [Phase 1: base](#phase-1-base)
    - [Infrastructure and observability](#infrastructure-and-observability)
    - [Phase 2: guardrails](#phase-2-guardrails)
    - [Phase 3: skills](#phase-3-skills)
    - [Phase 4: implement workflow](#phase-4-implement-workflow)
    - [Phase 5: spec](#phase-5-spec)
    - [Phase 6: manual calibration](#phase-6-manual-calibration)
    - [Phase 7: orchestrator](#phase-7-orchestrator)
    - [Phase 8: raise the dial](#phase-8-raise-the-dial)
    - [Learning evals](#learning-evals)
  - [Done](#done)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# To do

Setup and factory work for this repository.
Product work (the kata features) lives in Jira, space `CHB`.
This file is the only tracker for the project outside Jira.

When a task is done, tick it, move it to Done, and add the commit hash or the date.

## Decisions pending

- [ ] **Security baseline and authentication.** Target OWASP ASVS level 2. The app stays public: anyone can sign up. Proposal: Google Identity Platform for login, authorization by role in the app.
- [ ] **Agent GitHub identity.** Proposal: a GitHub App installed only on this repository. Blocks the review rules on `main`.

## Open

### Phase 1: base

- [ ] Turn on the `main` ruleset: PR required, checks `backend`, `frontend`, `e2e`, no force push, no bypass. Managed by IaC once the tool is chosen.
- [ ] Import the resources created by hand into IaC:
  - GCP project services, Artifact Registry repository and cleanup policy.
  - Service accounts and roles, Workload Identity pool and provider.
  - Budget, Cloud Run service, GitHub repository variables.
- [ ] Review Dependabot PRs #2 (uvicorn) and #3 (jsdom 30). CI passes on both. Dependabot closed #1 and #4 under the new ignore rules.
- [ ] Add a scrubbed export of the planning session to `docs/sessions/`, with a check that blocks private data (billing IDs, emails, local paths).

### Infrastructure and observability

- [ ] Create the Terraform state bucket by hand (versioned). See ADR-aae8.
- [ ] `infra/` with Terraform; CI runs `plan` on pull requests.
- [ ] OpenTelemetry in the backend, `grafana/otel-lgtm` in `make dev`. See ADR-c17d.
- [ ] Uptime check, SLO and alert policies in Terraform.
- [ ] Unattended Dependabot updates: review the PR from the Dependabot setup.

### Phase 2: guardrails

- [ ] `CODEOWNERS` for `.claude/`, `factory/`, `.github/`, `infra/`.
- [ ] Claude Code hooks and deny rules in `.claude/settings.json`.
- [ ] Devcontainer with an egress allow list for unattended runs.
- [ ] Jira workflow: To Do, In Progress, In Review, Validating, Done, Discarded.

### Phase 3: skills

- [ ] grill-me, to-prd, to-issues, ticket-gate, score-pr, verify-ac, postmortem.

### Phase 4: implement workflow

- [ ] Ticket to PR workflow with typed outputs (Claude Code workflows or the Agent SDK).

### Phase 5: spec

- [ ] Spec the kata with the user: base scope and variation 5. Variations 3 and 4 later.

### Phase 6: manual calibration

- [ ] Run about 10 tickets by hand through the workflow. Write a postmortem per ticket.

### Phase 7: orchestrator

- [ ] systemd timer, SQLite state, JSONL logs.
- [ ] Notifications: desktop (`notify-send`), Android (ntfy with a private topic and token), Telegram bot.

### Phase 8: raise the dial

- [ ] Auto-merge for low-risk tickets.

### Learning evals

- [ ] Build an eval set that replays finished tickets through a changed workflow or skill, and compare scores.

## Done

- [x] Decisions: Terraform (ADR-aae8), OpenTelemetry (ADR-c17d). 2026-11-06.
- [x] Raw session exports stay in the user's private notes folder (the only tracking exception). Curated copies go to `docs/sessions/`. 2026-11-06.
- [x] Walking skeleton: FastAPI health endpoint, React page, Docker, compose, Makefile, CI. Commit `6a66330`.
- [x] GCP project, Artifact Registry, deploy and runtime service accounts, Workload Identity Federation, 5 EUR budget alert. 2026-11-05.
- [x] First manual Cloud Run deploy with public access. 2026-11-06.
- [x] Jira CLI (`acli`) authenticated. 2026-11-06.
- [x] Deploy workflow after CI on `main`. Commit `094132b`.
- [x] API docs under `/api/docs`, README badges and production URL. Commit `1e9d79f`.
- [x] First automated deploy: CI, deploy, health version check, live smoke test in the Playwright image. Commit `1e9d79f`.
