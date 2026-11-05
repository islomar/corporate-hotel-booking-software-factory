<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-af5b: Stack and repository layout](#adr-af5b-stack-and-repository-layout)
  - [Context](#context)
  - [Decision](#decision)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-af5b: Stack and repository layout

- Status: accepted
- Date: 2026-11-05

## Context

The business requirements come from the Codurance kata "Corporate Hotel Booking".
The repository also hosts the software factory that builds the app.

## Decision

- Backend: Python 3.15 with FastAPI, managed with uv. Ruff, strict Pyright, pytest, mutmut.
- Frontend: React with TypeScript on Vite. oxlint, Prettier, Vitest, Testing Library.
- End-to-end: Playwright against the production container.
- One repository: `backend/`, `frontend/`, `e2e/`, `factory/`, `docs/`.
- In production, FastAPI serves the built frontend. One container, one origin, no CORS.

## Consequences

- Python 3.15.2 was checked on 2026-11-05: FastAPI, pydantic-core, pyright and mutmut install.
  If a later dependency lacks 3.15 wheels, fall back to 3.14 and supersede this record.
- `factory/` may move to its own repository later.
