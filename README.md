<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Corporate Hotel Booking: software factory experiment](#corporate-hotel-booking-software-factory-experiment)
  - [Quick start](#quick-start)
  - [Layout](#layout)
  - [Process](#process)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Corporate Hotel Booking: software factory experiment

A web app built from the Codurance kata [Corporate Hotel Booking](https://www.codurance.com/katas/corporate-hotel-booking), developed through an AI-agent software factory.

The experiment combines two sources:

- The 540 talk "Del humano en el bucle al desarrollo desatendido": skills, code workflows, an orchestrator, PR scoring, evidence per acceptance criterion.
- eferro's post [Turning up the dial](https://www.eferro.net/2026/10/turning-up-dial-seeing-forest-while.html): small safe steps, learnings in the repo, rules as checks, `validating` before done.

Delegation rises only as fast as the checks underneath it.

## Quick start

Requirements: Docker, GNU Make. For running checks outside Docker: [uv](https://docs.astral.sh/uv/) and Node 24.

```sh
make setup   # dependencies, Playwright browser, git pre-commit hook
make dev     # backend and frontend with hot reload: http://localhost:5173
make check   # lint, typecheck and tests (the pre-commit gate)
make up      # production image: http://localhost:8080
make e2e     # Playwright against the production image
make help    # all targets
```

## Layout

| Path | Content |
|---|---|
| `backend/` | FastAPI app, Python 3.15, uv |
| `frontend/` | React + TypeScript, Vite |
| `e2e/` | Playwright tests against the production container |
| `factory/` | Agent workflows, orchestrator, notifier |
| `docs/decisions/` | Decision records |
| `docs/specs/` | Product specs |
| `docs/knowledge/` | Learnings |
| `docs/postmortems/` | One per ticket |
| `docs/sessions/` | Curated agent session logs |

## Process

- Tasks live in Jira, space `CHB`.
- One ticket, one short-lived branch, one pull request. See `docs/decisions/ADR-0faf-short-lived-branches.md`.
- Merged is not done. See `docs/decisions/ADR-ad3e-definition-of-done.md`.
