.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup check-npm dev down clean-dev up check check-backend check-frontend check-deps lint-backend lint-frontend test-backend test-frontend test-scripts test e2e format mutate review-agreement infra-plan infra-apply

help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

setup: check-npm ## Install local dependencies and the repo git hooks
	cd backend && uv sync --locked
	cd frontend && npm ci
	cd e2e && npm ci && npx playwright install chromium
	ln -sf ../../scripts/git-hooks/pre-commit .git/hooks/pre-commit

# Keep 11.10.0 equal to MIN_NPM in scripts/run_e2e.py.
check-npm: ## Fail if npm is too old to honour min-release-age in .npmrc
	@v=$$(npm --version); printf '%s\n' 11.10.0 "$$v" | sort -V -C || \
		{ echo "npm $$v ignores min-release-age in .npmrc. Use Node 24 from .nvmrc (npm >= 11.10)."; exit 1; }

# The mkdir runs first: a missing frontend/node_modules would be created by Docker as root,
# and `npm ci` on the host then fails (CHB-138).
dev: ## Run backend and frontend with hot reload (http://localhost:5173), Grafana (http://localhost:3000)
	mkdir -p frontend/node_modules
	docker compose up --build backend frontend lgtm

down: ## Stop all containers
	docker compose --profile prod down

# The volumes are this checkout's compose project only (named after its folder). See CHB-138.
clean-dev: ## Remove this checkout's containers and volumes, and loop-detector state older than 7 days
	docker compose --profile prod down --volumes --remove-orphans
	python3 -I factory/hooks/warn_edit_loops.py --prune

up: ## Run the production image locally (http://localhost:8080)
	docker compose --profile prod up --build app

check: check-backend check-frontend check-deps ## Lint, typecheck and test everything (the pre-commit gate)

check-backend: lint-backend test-backend

check-frontend: lint-frontend test-frontend

lint-backend:
	cd backend && uv run --locked ruff format --check . && uv run --locked ruff check . && uv run --locked pyright

lint-frontend:
	cd frontend && npm run -s format:check && npm run -s lint && npm run -s typecheck

test-backend:
	cd backend && uv run --locked pytest

test-frontend:
	cd frontend && npm test

test-scripts:
	python3 -I -m unittest discover -s scripts/tests
	python3 -I -m unittest discover -s factory/tests

check-deps: test-scripts
	python3 -I scripts/check_playwright_image.py
	python3 -I scripts/check_sessions_redacted.py
	python3 -I scripts/check_doctoc_title.py
	python3 -I scripts/check_release_age.py backend/uv.lock
	python3 -I scripts/check_guard_tests.py

test: test-backend test-frontend test-scripts ## Run only the tests: backend, frontend, scripts and factory (no lint, types or repository checks)

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
	@test -n "$$TF_VAR_alert_email" || { echo "Set the alert address first: export TF_VAR_alert_email=<address> (see infra/README.md)"; exit 1; }
	export $(TF_ENV) GITHUB_TOKEN=$$(gh auth token) && test -n "$$TF_VAR_billing_account" && \
	python3 -I scripts/infra_guard.py plan

infra-apply: ## Apply the plans saved by infra-plan, only if made from the current main
	@python3 -I scripts/infra_guard.py git
	export $(TF_ENV) GITHUB_TOKEN=$$(gh auth token) && \
	python3 -I scripts/infra_guard.py apply
