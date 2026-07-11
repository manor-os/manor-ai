#!/bin/bash
# Start Manor AI in development mode
# Usage: ./scripts/dev.sh [api|web|worker|all]

set -e

MODE="${1:-all}"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

case "$MODE" in
  api)
    echo -e "${BLUE}Starting API server...${NC}"
    PYTHONPATH=. uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000
    ;;
  web)
    echo -e "${BLUE}Starting web dev server...${NC}"
    cd apps/web && npm run dev
    ;;
  worker)
    echo -e "${BLUE}Starting Celery worker + beat...${NC}"
    # -B embeds Celery Beat so scheduler.tick fires every 60s. Without
    # this, ScheduledJob rows never dispatch in local dev.
    PYTHONPATH=. celery -A packages.core.celery_app worker -B -l info -c 2
    ;;
  infra)
    echo -e "${BLUE}Starting infrastructure (postgres, redis, minio)...${NC}"
    docker compose up -d postgres redis minio
    echo -e "${GREEN}Waiting for services...${NC}"
    sleep 3
    echo -e "${GREEN}Infrastructure ready.${NC}"
    echo "  PostgreSQL: localhost:5434"
    echo "  Redis:      localhost:6389"
    echo "  MinIO:      localhost:9020 (console: localhost:9021)"
    ;;
  init)
    echo -e "${BLUE}Initializing database...${NC}"
    PYTHONPATH=. python3 scripts/init_db.py
    echo -e "${GREEN}Database initialized.${NC}"
    ;;
  test)
    echo -e "${BLUE}Running tests...${NC}"
    TEST_DATABASE_URL="postgresql+asyncpg://manor:manor_secret@localhost:5434/manor_test" \
    MANOR_FS_ENABLED=false \
    PYTHONPATH=. python3 -m pytest tests/ -v --tb=short
    ;;
  all)
    echo -e "${BLUE}Starting all services...${NC}"
    docker compose up -d postgres redis minio
    sleep 3
    echo -e "${GREEN}Infrastructure up. Starting API + Web...${NC}"
    echo "Run in separate terminals:"
    echo "  ./scripts/dev.sh api"
    echo "  ./scripts/dev.sh web"
    echo "  ./scripts/dev.sh worker  (optional)"
    ;;
  *)
    echo "Usage: $0 {api|web|worker|infra|init|test|all}"
    exit 1
    ;;
esac
