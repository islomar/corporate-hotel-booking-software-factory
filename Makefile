.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup check-npm dev down up check check-backend check-frontend check-deps test e2e format mutate review-agreement infra-plan infra-apply

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

setup: check-npm ## Install local dependencies and the repo git hooks
	cd backend && uv sync --locked
	cd frontend && npm ci
	cd e2e && npm ci && npx playwright install chromium
	ln -sf ../../scripts/git-hooks/pre-commit .git/hooks/pre-commit

# Keep 11.10.0 equal to MIN_NPM in scripts/run_e2e.py.
check-npm: ## Fail if npm is too old to honour min-release-age in .npmrc
	@v=$$(npm --version); printf '%s\n' 11.10.0 "$$v" | sort -V -C || \
		{ echo "npm $$v ignores min-release-age in .npmrc. Use Node 24 from .nvmrc (npm >= 11.10)."; exit 1; }

dev: ## Run backend and frontend with hot reload (http://localhost:5173), Grafana (http://localhost:3000)
	docker compose up --build backend frontend lgtm

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
	python3 -I -m unittest discover -s factory/tests
	python3 -I scripts/check_playwright_image.py
	python3 -I scripts/check_sessions_redacted.py
	python3 -I scripts/check_release_age.py backend/uv.lock

test: check ## Alias for check

# Installs what is missing and starts the production container when nothing answers on
# :8080. With APP_URL set it tests that URL and starts nothing. See scripts/run_e2e.py.
e2e: ## Run Playwright (starts the production container if needed; APP_URL=<url> tests a running app)
	@python3 -I scripts/run_e2e.py

format: ## Format all code
	cd backend && uv run --locked ruff format . && uv run --locked ruff check --fix .
	cd frontend && npm run -s format

mutate: ## Mutation testing for the backend
	cd backend && uv run --locked mutmut run

# SINCE=YYYY-MM-DD changes the first merge date. See factory/review/agreement.py and ADR-ea8c, stage 1.
review-agreement: ## Owner agreement with the review verdict on merged PRs (ADR-ea8c stage 1)
	python3 -I factory/review/agreement.py $(if $(SINCE),--since $(SINCE))

TF_ENV = GOOGLE_OAUTH_ACCESS_TOKEN=$$(CLOUDSDK_ACTIVE_CONFIG_NAME=chb gcloud auth print-access-token) \
	TF_VAR_billing_account=$$(CLOUDSDK_ACTIVE_CONFIG_NAME=chb gcloud billing projects describe chb-factory-a92932 --format='value(billingAccountName)' | sed 's\#billingAccounts/\#\#')

# scripts/infra_guard.py refuses unless the checkout is main, equal to origin/main, with no local
# changes in infra/, Makefile or scripts/. The first line checks that before gcloud is asked for a token.
# A destroy or replace fails both targets unless ALLOW_DESTROY lists exactly those addresses.
infra-plan: ## Terraform plans for GCP and GitHub, saved in infra/.plans/ (from main only)
	@python3 -I scripts/infra_guard.py git
	export $(TF_ENV) GITHUB_TOKEN=$$(gh auth token) && test -n "$$TF_VAR_billing_account" && \
	python3 -I scripts/infra_guard.py plan

infra-apply: ## Apply the plans saved by infra-plan, only if made from the current main
	@python3 -I scripts/infra_guard.py git
	export $(TF_ENV) GITHUB_TOKEN=$$(gh auth token) && \
	python3 -I scripts/infra_guard.py apply
