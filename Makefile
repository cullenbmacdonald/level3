DB_URL := postgresql://postgres:level3@localhost:5432/level3

.PHONY: dev lint fmt typecheck check rebuild-db wipe-db wipe-capabilities clean

dev:
	./run.sh

lint:
	uv run ruff check src/

fmt:
	uv run ruff check --fix src/
	uv run ruff format src/

typecheck:
	uv run mypy src/

check: lint typecheck

rebuild-db:
	psql $(DB_URL) -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	psql $(DB_URL) -f schema.sql

wipe-db:
	psql $(DB_URL) -c "TRUNCATE capabilities, conversations, tasks RESTART IDENTITY;"

wipe-capabilities:
	psql $(DB_URL) -c "TRUNCATE capabilities RESTART IDENTITY;"
	find src/level3/capabilities -name '*.py' ! -name '__init__.py' -delete

clean: rebuild-db wipe-capabilities
