---
name: recruiting-ledger
description: Read, query, and record candidate, employee, and contractor lifecycle state and events in the active internal Workspace Recruiting Ledger.
---

# Recruiting Ledger

Use the Recruiting Ledger as the internal Workspace database for candidate,
employee, and contractor lifecycle facts. It is not available on external
customer surfaces.

## Read and query

- Use `query_ledger` with `ledger="recruiting_ledger"` for targeted current or
  event queries, filtering, grouping, and pagination.
- Use `read_recruiting_ledger` for the complete current projection, recent
  lifecycle events, or exact record state.
- Use `visualize_workspace_ledgers` only when a summary is appropriate and does
  not expose unnecessary personal information.

Read the stable record before changing stage, status, manager, department,
role, or location.

## Record

Use one opaque stable `record_key` per subject. Keep `subject_type`,
`display_name`, `role_title`, `department`, `location`, `manager_ref`, `stage`,
and `status` in their typed fields. Use `payload` only for additional
operational facts and preserve established snake_case names.

Append lifecycle facts with a stable event name, actual `occurred_at`, and a
deterministic `idempotency_key`. Use `clear_fields` for explicit corrections;
omitting a field preserves its current value. Keep sensitive source material
in `evidence_refs` rather than copying it into the event payload.

Never infer protected characteristics, candidate suitability, interview
outcomes, employment status, compensation, or approvals. Record only verified
facts and authorized decisions, minimize personal data, and avoid exposing
internal recruiting records on external surfaces.
