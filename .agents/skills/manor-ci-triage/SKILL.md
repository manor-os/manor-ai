---
name: manor-ci-triage
description: Use when Manor GitHub Actions, .github/workflows/ci.yml, OSS smoke/regression jobs, web source smoke, frontend build, lint, or public CI failure logs need diagnosis or repair.
---

# Manor CI Triage

Use this skill to diagnose public Manor CI failures by matching each failing job to the closest local command.

Read the repository [testing policy](../../testing.md) before changing tests or
gates. Classify product regressions, obsolete assertions, test defects, and
environment failures before choosing a fix.

## Job map

| CI job | Local probe |
| --- | --- |
| Lint | `ruff check packages/ apps/ tests/`; `ruff format --check packages/ apps/ tests/`; `git diff --check` |
| External API versions | `python scripts/check_api_versions.py` |
| Frontend Build | `npm --prefix apps/web ci`; `npm --prefix apps/web run build` |
| Web source smoke | `npm --prefix apps/web ci`; `npm --prefix apps/web run test:source` |
| Python smoke | `make test-setup`, then `make test-smoke`; use `scripts/run_python_tests.py smoke <paths>` for focused diagnosis. |
| Python regression | Copy the exact marker expression, environment, and any shard arguments from the checked-out workflow's failing job; regression selection differs between repository variants. |
| Docs site | `npm --prefix docs-site ci`; `npm --prefix docs-site run build` |

## Procedure

1. Identify the exact failing job, step, command, and first actionable error. Do not start by editing workflow YAML.
2. Compare CI command to `Makefile`, `pyproject.toml` markers, and `.github/workflows/ci.yml`.
3. For Python CI, inspect the failing job's actual marker expression before changing tests. Do not assume regression selects only `oss_smoke` and `oss_regression`. Smoke should stay fast; regression should be broader.
4. Reproduce locally with the closest command. If services are required, use the same environment variables as CI.
5. Fix the underlying command/test/code. Only relax CI when the job definition is demonstrably wrong.
6. For intermittent failures, retain the SHA, shard selection/order, environment,
   and setup/call/teardown evidence. Compare local Makefile selection with the
   actual CI lane; media and exported OSS selections differ. Reproduce the
   failing combination before treating a rerun as evidence of a fix.
7. For slow CI, compare workflow wall time, cumulative job time, and the
   critical path. Use Python reports to separate setup/call/teardown work
   from installation and collection overhead. Check shard weight coverage;
   follow the testing policy's duration refresh procedure before adding
   runners. Validate fixture optimizations against their real isolation and
   persistence contracts, then measure the complete workflow at the final SHA.

## CI guardrails

- Keep advisory lint advisory unless the repo-wide baseline is intentionally burned down.
- Do not make smoke and regression equivalent unless explicitly requested.
- Do not hide failures with `continue-on-error` except for already-advisory jobs.
- Keep live-provider and unprovisioned manual/Docker scenarios out of default
  PR gates. Focused, isolated browser smoke may remain a critical gate; test
  layer alone is not a reason to remove it.

## Report back

Include:

- failing job and command,
- root cause,
- files changed,
- local verification command and result,
- any remaining CI-only blocker.
