DB_URL := postgresql://postgres:level3@localhost:5432/level3

.DEFAULT_GOAL := help

.PHONY: help dev lint fmt typecheck check rebuild-db wipe-db wipe-capabilities clean docker-up docker-down

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

dev: ## Run the agent locally
	./run.sh

lint: ## Run linter
	uv run ruff check src/

fmt: ## Auto-fix and format code
	uv run ruff check --fix src/
	uv run ruff format src/

typecheck: ## Run type checker
	uv run mypy src/

check: lint typecheck ## Run lint + typecheck

rebuild-db: ## Drop and recreate the database schema
	psql $(DB_URL) -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	psql $(DB_URL) -f schema.sql

wipe-db: ## Truncate all tables
	psql $(DB_URL) -c "TRUNCATE capabilities, conversations, tasks RESTART IDENTITY;"

wipe-capabilities: ## Remove all capabilities
	psql $(DB_URL) -c "TRUNCATE capabilities RESTART IDENTITY;"
	find src/level3/capabilities -name '*.py' ! -name '__init__.py' -delete

clean: rebuild-db wipe-capabilities ## Rebuild DB and wipe capabilities

docker-up: ## Start services with Docker Compose
	docker compose build agent
	docker compose up -d

docker-down: ## Stop Docker Compose services
	docker compose down
