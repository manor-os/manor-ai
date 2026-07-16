---
name: manor-ci-triage
description: Use when Manor GitHub Actions, .github/workflows/ci.yml, OSS smoke/regression jobs, web source smoke, frontend build, lint, or public CI failure logs need diagnosis or repair.
---

# Manor CI Triage

Use this skill to diagnose public Manor CI failures by matching each failing job to the closest local command.

## Job map

| CI job | Local probe |
| --- | --- |
| Lint | `ruff check packages/ apps/ tests/`; `ruff format --check packages/ apps/ tests/`; `git diff --check` |
| External API versions | `python scripts/check_api_versions.py` |
| Frontend Build | `npm --prefix apps/web ci`; `npm --prefix apps/web run build` |
| Web source smoke | `npm --prefix apps/web ci`; `npm --prefix apps/web run test:source` |
| Python smoke | `.venv/bin/python -m pytest tests/ -m "oss_smoke" -q --tb=short -p no:warnings` |
| Python regression | `.venv/bin/python -m pytest tests/ -m "oss_smoke or oss_regression" -q --tb=short -p no:warnings` |
| Docs site | `npm --prefix docs-site ci`; `npm --prefix docs-site run build` |

## Procedure

1. Identify the exact failing job, step, command, and first actionable error. Do not start by editing workflow YAML.
2. Compare CI command to `Makefile`, `pyproject.toml` markers, and `.github/workflows/ci.yml`.
3. For Python CI, check `oss_smoke` and `oss_regression` markers before changing tests. Smoke should stay fast; regression should be broader.
4. Reproduce locally with the closest command. If services are required, use the same environment variables as CI.
5. Fix the underlying command/test/code. Only relax CI when the job definition is demonstrably wrong.

## CI guardrails

- Keep advisory lint advisory unless the repo-wide baseline is intentionally burned down.
- Do not make smoke and regression equivalent unless explicitly requested.
- Do not hide failures with `continue-on-error` except for already-advisory jobs.
- Avoid network/manual/docker/e2e tests in default PR gates.

## Report back

Include:

- failing job and command,
- root cause,
- files changed,
- local verification command and result,
- any remaining CI-only blocker.
