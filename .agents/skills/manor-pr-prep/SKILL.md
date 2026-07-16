---
name: manor-pr-prep
description: Use when preparing a Manor branch or change set for pull request, final review, commit, merge readiness, PR body, verification summary, or checking that tests/docs/self-hosted compatibility requirements match the touched files.
---

# Manor PR Prep

Use this skill to make a Manor branch reviewable without losing work or overstating verification.

## Preflight

1. Inspect `git status --short` and `git diff --name-only`.
2. Separate user changes from current-task changes. Do not stage unrelated files.
3. Read `.github/PULL_REQUEST_TEMPLATE.md`.
4. Identify risk areas:
   - API/runtime: `apps/api`, `packages/core`, migrations, OpenAPI.
   - frontend: `apps/web`.
   - docs-site: `docs-site`.
   - local runtime: `docker-compose.yml`, `.env.example`, worker/sandbox settings.
   - CI/docs: `.github/workflows`, README, docs, public media.

## Verification matrix

Run only relevant checks, but make the selection explicit.

| Touched area | Minimum check |
| --- | --- |
| Python API/core/tests | targeted `.venv/bin/python -m pytest ... -q --tb=short -p no:warnings` |
| CI marker changes | targeted `.venv/bin/python -m pytest tests/test_ci_first_phase.py -q --tb=short -p no:warnings` |
| Frontend | `npm --prefix apps/web ci` if dependencies are absent, then `npm --prefix apps/web run build` and/or `npm --prefix apps/web run test:source` |
| docs-site | `npm --prefix docs-site ci` if dependencies are absent, then `npm --prefix docs-site run build` |
| Formatting only | `git diff --check` plus area-specific format check |

If a check cannot run because dependencies are absent, say that directly and provide the blocker.

## PR body guidance

Use the template:

- Summary: what changed and why.
- Verification: commands actually run and results.
- Self-Hosted Compatibility: explain whether self-hosted behavior changed.
- Notes: migrations, screenshots, release risk, follow-ups.

## Commit/stage safety

- Stage only files in scope.
- Do not amend, rebase, force-push, or delete branches unless requested.
- Before claiming readiness, rerun or cite fresh verification from this turn.

## Handoff checklist

- Dirty worktree contains only expected files or is clearly explained.
- New public assets are tracked.
- No ignored build artifacts are staged.
- PR risks are named, not buried.
