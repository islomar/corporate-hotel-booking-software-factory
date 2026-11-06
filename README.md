<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
## Contents

- [Corporate Hotel Booking: software factory experiment](#corporate-hotel-booking-software-factory-experiment)
  - [Quick start](#quick-start)
  - [API documentation](#api-documentation)
  - [Layout](#layout)
  - [Process](#process)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Corporate Hotel Booking: software factory experiment

[![CI](https://github.com/islomar/corporate-hotel-booking-software-factory/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/islomar/corporate-hotel-booking-software-factory/actions/workflows/ci.yml)
[![Deploy](https://github.com/islomar/corporate-hotel-booking-software-factory/actions/workflows/deploy.yml/badge.svg?branch=main)](https://github.com/islomar/corporate-hotel-booking-software-factory/actions/workflows/deploy.yml)
[![Production version](https://img.shields.io/badge/dynamic/json?url=https://chb-1027266991110.europe-west1.run.app/api/health&query=%24.version&label=production&color=blue)](https://chb-1027266991110.europe-west1.run.app)
![Python](https://img.shields.io/badge/python-3.15-3776AB?logo=python&logoColor=white)
![React](https://img.shields.io/badge/react-19-61DAFB?logo=react&logoColor=black)

- Production: <https://chb-1027266991110.europe-west1.run.app>
- API reference (OpenAPI): <https://chb-1027266991110.europe-west1.run.app/api/docs>

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

## API documentation

FastAPI generates an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) schema from the route type hints and pydantic models.
The schema cannot drift from the code because the code is its source.

| Path | Content |
|---|---|
| `/api/docs` | Swagger UI: browse and call the endpoints |
| `/api/redoc` | ReDoc: read-only reference |
| `/api/openapi.json` | The raw schema, for clients and contract checks |

Locally the same paths work on <http://localhost:8000> (`make dev`) and <http://localhost:8080> (`make up`).

## Layout

| Path | Content |
|---|---|
| `backend/` | FastAPI app, Python 3.15, uv |
| `frontend/` | React + TypeScript, Vite |
| `e2e/` | Playwright tests against the production container |
| `factory/` | Agent workflows, orchestrator, notifier |
| `scripts/` | Git hook and dependency checks |
| `docs/decisions/` | Decision records |
| `docs/specs/` | Product specs |
| `docs/knowledge/` | Learnings |
| `docs/postmortems/` | One per ticket |
| `docs/sessions/` | Curated agent session logs |

## Process

Pushes to `main` run CI. When CI passes, the deploy workflow builds the image, deploys it to Cloud Run, checks that `/api/health` reports the new commit, and runs Playwright against the live URL.

- Tasks live in Jira, space `CHB`.
- Setup and factory work is tracked in Jira too. [`TODO.md`](TODO.md) lists the epics.
- One ticket, one short-lived branch, one pull request. See `docs/decisions/ADR-0faf-short-lived-branches.md`.
- Merged is not done. See `docs/decisions/ADR-ad3e-definition-of-done.md`.
- Dependabot updates arrive weekly. Safe ones merge without a person once the setup in `docs/decisions/ADR-ebe6-unattended-dependency-updates.md` is complete.
