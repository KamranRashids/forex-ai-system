# ============================================================================
# Forex AI System — developer workflow
# Run targets inside WSL/Linux where docker/make/python3/node are available.
# ============================================================================

COMPOSE      := docker compose
BACKEND_DIR  := backend
FRONTEND_DIR := frontend
VENV         := $(BACKEND_DIR)/.venv
BIN          := $(CURDIR)/$(VENV)/bin

.DEFAULT_GOAL := help
.PHONY: help dev dev-down dev-destroy prod-up prod-down logs ps \
	backend-venv format-backend lint-backend typecheck-backend test-backend \
	test-backend-unit test-backend-integration coverage-backend migrate-backend \
	install-frontend lint-frontend typecheck-frontend build-frontend \
	verify clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

dev: ## Build and start the dev stack (postgres, redis, api, web) in background
	$(COMPOSE) up -d --build

dev-down: ## Stop the dev stack (keeps volumes)
	$(COMPOSE) down --remove-orphans

dev-destroy: ## Stop the dev stack AND delete volumes (data loss!)
	$(COMPOSE) down -v --remove-orphans

prod-up: ## Start the prod-style stack behind nginx (TLS terminated externally)
	$(COMPOSE) -f docker-compose.yml -f docker-compose.prod.yml up -d --build

prod-down: ## Stop the prod-style stack
	$(COMPOSE) -f docker-compose.yml -f docker-compose.prod.yml down --remove-orphans

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

ps: ## Show container status and health
	$(COMPOSE) ps

backend-venv: ## Create backend/.venv and install runtime + dev dependencies
	python3 -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e './$(BACKEND_DIR)[dev]'

format-backend: ## Format backend code with ruff
	cd $(BACKEND_DIR) && $(BIN)/ruff format .

lint-backend: ## Lint backend code with ruff
	cd $(BACKEND_DIR) && $(BIN)/ruff check .

typecheck-backend: ## Type-check backend code with mypy
	cd $(BACKEND_DIR) && $(BIN)/mypy app

test-backend: ## Run backend tests (integration tests skip if no local PostgreSQL)
	cd $(BACKEND_DIR) && $(BIN)/pytest

test-backend-unit: ## Run unit suite with the strict coverage gate
	cd $(BACKEND_DIR) && $(BIN)/pytest tests/unit --cov=app --cov-report=term-missing

test-backend-integration: ## Run integration tests (requires docker compose postgres+redis up)
	cd $(BACKEND_DIR) && $(BIN)/pytest tests/integration

coverage-backend: ## Open HTML coverage report for the unit suite
	cd $(BACKEND_DIR) && $(BIN)/pytest tests/unit --cov=app --cov-report=html && $(BIN)/python -m webbrowser -t htmlcov/index.html 2>/dev/null || true

migrate-backend: ## Apply database migrations using backend/.venv (host-side DATABASE_URL)
	cd $(BACKEND_DIR) && $(BIN)/alembic upgrade head

install-frontend: ## Install frontend node modules (npm ci)
	cd $(FRONTEND_DIR) && npm ci

lint-frontend: ## Lint frontend with eslint
	cd $(FRONTEND_DIR) && npm run lint

typecheck-frontend: ## Type-check frontend with tsc
	cd $(FRONTEND_DIR) && npx tsc --noEmit

build-frontend: ## Production build of the frontend
	cd $(FRONTEND_DIR) && npm run build

verify: lint-backend format-backend typecheck-backend test-backend lint-frontend typecheck-frontend ## Run every local quality gate

clean: ## Remove caches and build artifacts (keeps .venv and node_modules)
	rm -rf $(BACKEND_DIR)/.pytest_cache $(BACKEND_DIR)/.mypy_cache $(BACKEND_DIR)/.ruff_cache
	rm -rf $(FRONTEND_DIR)/.next $(FRONTEND_DIR)/out $(FRONTEND_DIR)/.eslintcache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
