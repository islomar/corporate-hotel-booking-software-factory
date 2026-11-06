.DEFAULT_GOAL := help
SHELL := /bin/bash
APP_URL ?= http://localhost:8080

.PHONY: help setup check-npm dev down up check check-backend check-frontend check-deps test e2e format mutate

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

setup: check-npm ## Install local dependencies and the repo git hooks
	cd backend && uv sync --locked
	cd frontend && npm ci
	cd e2e && npm ci && npx playwright install chromium
	ln -sf ../../scripts/git-hooks/pre-commit .git/hooks/pre-commit

check-npm: ## Fail if npm is too old to honour min-release-age in .npmrc
	@v=$$(npm --version); printf '%s\n' 11.10.0 "$$v" | sort -V -C || \
		{ echo "npm $$v ignores min-release-age in .npmrc. Use Node 24 from .nvmrc (npm >= 11.10)."; exit 1; }

dev: ## Run backend and frontend with hot reload (http://localhost:5173)
	docker compose up --build backend frontend

down: ## Stop all containers
	docker compose --profile prod down

up: ## Run the production image locally (http://localhost:8080)
	docker compose --profile prod up --build app

check: check-backend check-frontend check-deps ## Lint, typecheck and test everything (the pre-commit gate)

check-backend:
	cd backend && uv run --locked ruff format --check . && uv run --locked ruff check . && uv run --locked pyright && uv run --locked pytest

check-frontend:
	cd frontend && npm run -s format:check && npm run -s lint && npm run -s typecheck && npm test

check-deps:
	python3 -I -m unittest discover -s scripts/tests
	python3 -I scripts/check_playwright_image.py
	python3 -I scripts/check_release_age.py backend/uv.lock

test: check ## Alias for check

e2e: ## Run Playwright against APP_URL (default: the container from `make up`)
	cd e2e && APP_URL=$(APP_URL) npx playwright test

format: ## Format all code
	cd backend && uv run --locked ruff format . && uv run --locked ruff check --fix .
	cd frontend && npm run -s format

mutate: ## Mutation testing for the backend
	cd backend && uv run --locked mutmut run
