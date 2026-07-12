---
sidebar_position: 11
title: Development
---

# Development

## Setup

```bash
cp .env.example .env
pip install ".[dev]"
cd apps/web
npm ci
cd ../..
./scripts/dev.sh infra
./scripts/dev.sh init
```

Run services:

```bash
./scripts/dev.sh api
./scripts/dev.sh web
./scripts/dev.sh worker
```

## Tests

```bash
make test
make test-regression
make test-e2e
make test-all
```

The default test target excludes slow, manual, network, Docker, and cloud-marked
tests.

## Frontend

```bash
cd apps/web
npm ci
npm run dev
npm run build
```

## Style

Python is checked with Ruff. TypeScript uses strict mode.

```bash
make lint
make format
```

## Contributions

See `CONTRIBUTING.md` in the repository root. Keep changes focused, add tests
for behavior changes, and update docs when setup or user-facing behavior
changes.
