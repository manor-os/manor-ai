TAG ?= prod
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then printf '%s' .venv/bin/python; else printf '%s' python3; fi)
PYTEST_DEFAULT_MARKERS ?= not e2e and not manual and not slow and not network and not docker and not cloud
PYTEST_REGRESSION_MARKERS ?= not manual and not network and not docker and not cloud
PYTEST_ENV = TEST_DATABASE_URL="postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test" MANOR_FS_ENABLED=false PYTHONPATH=.

.PHONY: dev test test-smoke test-regression test-manual test-e2e test-all test-ws test-embedding lint build clean docker-up

# Development
dev-api:
	PYTHONPATH=. uvicorn apps.api.main:app --reload --port 8000

dev-web:
	cd apps/web && npm run dev

dev-infra:
	docker compose up -d postgres redis minio

# Testing
test:
	$(PYTEST_ENV) $(PYTHON) -m pytest tests/ -m "$(PYTEST_DEFAULT_MARKERS)" -q --tb=short -p no:warnings

test-smoke: test

test-regression:
	$(PYTEST_ENV) $(PYTHON) -m pytest tests/ -m "$(PYTEST_REGRESSION_MARKERS)" -q --tb=short -p no:warnings


test-manual:
	$(PYTEST_ENV) $(PYTHON) -m pytest tests/ -m "manual" -q --tb=short -p no:warnings

test-e2e:
	$(PYTEST_ENV) $(PYTHON) -m pytest tests/ -m "e2e" -q --tb=short -p no:warnings

test-all:
	$(PYTEST_ENV) $(PYTHON) -m pytest tests/ -q --tb=short

test-ws:
	PYTHONPATH=. $(PYTHON) -m pytest tests/test_ws.py -q --tb=short

test-embedding:
	$(PYTEST_ENV) $(PYTHON) -m pytest tests/test_embedding.py -q --tb=short

# Linting
lint:
	ruff check packages/ apps/ tests/
	cd apps/web && npx tsc --noEmit

format:
	ruff format packages/ apps/ tests/

# Build
build-web:
	cd apps/web && npm run build

build-docker:
	docker build -f docker/Dockerfile.api -t manor-api:$(TAG) .
	docker build -f docker/Dockerfile.web -t manor-web:$(TAG) .
	docker build -f docker/Dockerfile.sandbox-service -t manor-sandbox-service:$(TAG) .
	docker build -f docker/Dockerfile.sandbox -t sandbox-skill:latest .

docker-up:
	docker compose up --build -d


# Database
db-init:
	PYTHONPATH=. python3 scripts/init_db.py

db-migrate:
	@DATABASE_URL_SYNC=$${DATABASE_URL_SYNC:-postgresql://manor:manor_secret@localhost:5434/manor} PYTHONPATH=. uv run alembic upgrade heads

# OpenAPI
openapi:
	PYTHONPATH=. python3 scripts/export_openapi.py docs/openapi.json

# Clean
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf apps/web/dist apps/web/node_modules/.cache
