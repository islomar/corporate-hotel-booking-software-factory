.DEFAULT_GOAL := help
SHELL := /bin/bash
APP_URL ?= http://localhost:8080

.PHONY: help setup dev down up check check-backend check-frontend test e2e format mutate

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

setup: ## Install local dependencies and the repo git hooks
	cd backend && uv sync
	cd frontend && npm ci
	cd e2e && npm ci && npx playwright install chromium
	ln -sf ../../scripts/git-hooks/pre-commit .git/hooks/pre-commit

dev: ## Run backend and frontend with hot reload (http://localhost:5173)
	docker compose up --build backend frontend

down: ## Stop all containers
	docker compose --profile prod down

up: ## Run the production image locally (http://localhost:8080)
	docker compose --profile prod up --build app

check: check-backend check-frontend ## Lint, typecheck and test everything (the pre-commit gate)

check-backend:
	cd backend && uv run ruff format --check . && uv run ruff check . && uv run pyright && uv run pytest

check-frontend:
	cd frontend && npm run -s format:check && npm run -s lint && npm run -s typecheck && npm test

test: check ## Alias for check

e2e: ## Run Playwright against APP_URL (default: the container from `make up`)
	cd e2e && APP_URL=$(APP_URL) npx playwright test

format: ## Format all code
	cd backend && uv run ruff format . && uv run ruff check --fix .
	cd frontend && npm run -s format

mutate: ## Mutation testing for the backend
	cd backend && uv run mutmut run
