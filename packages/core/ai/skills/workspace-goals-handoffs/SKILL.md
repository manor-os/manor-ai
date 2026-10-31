---
name: workspace-goals-handoffs
description: Read and measure Workspace goals/stats, request Strategist planning and handle human input, Task blockers or configuration review decisions.
version: 1.0.0
---

# Workspace Goals and Human Handoffs

Read get_goal_status/get_stat_status for the current definitions, values,
measurement metadata and revisions. Unknown values are not zero. update_goal_value
records a verified manual measurement with goal_id and numeric value.
record_stat_assessment requires current stat_id/expected_revision, a stable
idempotency_key, status, reasoning, versioned document evidence and explicit gaps.
Only a returned persisted observation_id proves an assessment was saved.
To track audience numbers (latest video views, comments, likes, followers,
active users), call add_assessed_metrics with templates; it creates the stats and
one scheduled agent assessment. Report whether the schedule is running.
Changes to goal definitions or measurement sources use operation get_current,
then a reviewed draft with goal.upsert/goal.measurement_sources.update; goals.replace
replaces the collection. Apply only after approval and verify the saved definitions.

For planning, reprioritization, goal-driven next steps or a general request to
continue Workspace work, call request_strategist_review. Strategy drives missing
core facts through Need Input; do not interview the user field-by-field. Extra
requirements on an existing Task belong on that Task instead of a new review.

Inspect Pending Human Requests and Open Task Blockers in the current context.
When a reply answers human_input/needs_input, call resolve_hitl for its actual
message_id/hitl_id with inner action="respond" and response (or structured
payload.fields/answers and attachment references). For workspace_operation_review,
use inner action="approve" or "reject" only when the user actually makes that
decision. An ordinary answer is not approval. Workflow decisions, login,
reconciliation and tool authorization use their dedicated controls.

For Task input, use answer_task_blocker with the real request_id. The paused
Task resumes with its own tools; do not re-delegate while its blocker is open.
A bare retry or unrelated reply is not an answer. Re-read the owning object
after a resolution/approved configuration apply. Rejection/cancellation must not
start work. visualize_ledgers displays current ledger evidence and does not
create measurements, execute jobs or change ledger configuration.
