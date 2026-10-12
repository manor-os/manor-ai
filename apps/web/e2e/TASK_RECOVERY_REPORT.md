# Task execution / recovery browser QA

Date: 2026-09-17 (America/Los_Angeles).

Final run: **8 passed in 52.0s**. No captured browser `pageerror` events.
Python fixture lint and `git diff --check` also passed.

## Isolated merge-candidate verification

On 2026-09-18, the task-only patch was reapplied to `dev` at `0e58d2b22` in a separate
worktree. The original checkout and its unrelated modifications were retained.
Merge resolution preserves the newer service-deployment authorization checks
and the subagent `request_input` flow; bound Actions expose only execution of
the approved action or failure submission.

- Browser suite rerun on this isolated source: **8 passed**; desktop/mobile
  screenshots inspected again with the same no-worker/no-publication boundary.
- Focused backend and overlap regressions: **291 passed**.
- Cross-node and runtime-contract regressions: **402 passed**.
- Web production build and **1,523 frontend source tests passed**.
- TypeScript Worker SDK typecheck and transport smoke test passed.

The backend suite uses `DEPLOYMENT_MODE=oss EMAIL_ENABLED=false` with a dedicated
local test database. An earlier Cloud-mode invocation was stopped because
Cloud's strong-password requirement rejected existing short-password signup
fixtures; that invocation is not counted as successful verification.

## Environment and evidence boundary

- Current working-tree web source served by Vite at `127.0.0.1:18921`.
- Current working-tree API at `127.0.0.1:18920`, new isolated PostgreSQL database.
- Real Chromium, login form, authenticated HTTP routes, database transactions,
  Task/Chat decision services, and Celery publication into an in-memory broker.
- Historical Plans and Supervisor audit records are test prerequisites, seeded
  with the real models and shared decision-card services. No browser API response
  interception is used.
- Workspaces are paused and have only a synthetic local Agent subscription.
  No downstream worker, external integration, or publishing account runs.
- Existing Docker services, production task, and production data were unchanged.

## Verified cases

| Case | Result |
| --- | --- |
| Ownerless team Task first start from Task status control | One Planner job; owner remains unset; state survives reload |
| Task detail retry after a mechanically completed historical Plan | One Planner job; old Plan retained; prior output passed to replan; duplicate retry rejected |
| Workspace Chat retry, desktop | Readable `publish post` label instead of raw `publish_post`; Enter submits; one job; resolved card survives reload |
| Workspace Chat retry, 390 × 844 | Same persisted result; no page horizontal overflow; visible actionable controls |
| Inactive team | Actionable failure; no job; Task remains waiting; retry card stays usable |
| Cancel recovery | Cancelled Task; no job; shared card remains resolved |
| Approve approval-type Task | Exact approved decision persists; shared Chat card closes; replay has no additional effect |
| Request changes on approval-type Task | Revision decision persists; shared Chat card closes; replay has no additional effect |

The resolved Chat-card endpoint intentionally acknowledges an already-resolved
card with HTTP 200 and its original decision; the closed Task approval endpoint
returns HTTP 409. Both paths were checked for unchanged decisions and no dispatch.

Post-reload checks wait for positive rendered Task/card content, not merely the
absence of a retry button. Desktop/mobile before-and-after screenshots were
visually inspected. Early fixture/selector failures were corrected before the
final complete run; no product-code changes were needed in this QA turn.

## Not proven by this run

- Live model planning, action-tool selection, or Supervisor model behavior.
- Downstream worker execution, broker-consumer delivery, Plan/Step external-action
  approvals, provider credentials, or successful publication on X.
- Production deployment or repair of the original online Task.

This is browser + real API admission/recovery verification, not a claim of full
external-publication end-to-end success. Earlier backend contract tests cover
worker/receipt mechanics separately.

## Reproduce

From the repository root, with the local PostgreSQL on 5434 and Redis on 6389:

```sh
PYTHONPATH=. .venv/bin/python apps/web/e2e/support/task-recovery-server.py
```

In another terminal:

```sh
E2E_TASK_RECOVERY=1 E2E_PORT=18921 E2E_API=http://127.0.0.1:18920 \
PLAYWRIGHT_HTML_OPEN=never npm --prefix apps/web run test:e2e -- \
  task-recovery-live.spec.ts --workers=1 --trace=on --reporter=line,html
```

Evidence: `apps/web/playwright-report/index.html` and the per-case screenshots
and traces under `apps/web/test-results/task-recovery-live-*`.
The support script creates a new uniquely named test database on each start
and retains it for inspection; it never resets an existing database.
