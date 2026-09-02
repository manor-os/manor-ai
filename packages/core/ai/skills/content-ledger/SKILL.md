---
name: content-ledger
description: Read, query, reserve, and record content identities and lifecycle events in the active Workspace Content Ledger so AI workflows avoid duplicate work and preserve publication history.
---

# Content Ledger

Use the Content Ledger as the Workspace's append-only content identity and
lifecycle database. The AI and publishing tools create or distribute content;
the Ledger prevents duplicate identities and records verified lifecycle facts.

## Read and query

- Use `query_ledger` with `ledger="content_ledger"` for filtered records,
  grouping, pagination, and event analysis.
- Use `read_content_ledger` when exact reservations, identity history, legacy
  identities, or the complete configured ledger are required.
- Use `visualize_workspace_ledgers` only when a visual summary helps.

Read before proposing or reserving content when duplicate topic, asset, title,
source, or external publication identity matters.

## Reserve and record

Use `record_content_ledger` with:

- `action="reserve"` before creating work for a new stable identity.
- `action="event"` only after a lifecycle fact has occurred.
- `action="backfill_used"` only when importing an already-used identity with
  evidence.

Use a stable `identity_key`, an execution-specific `run_key`, and the returned
`reservation_id` for later events. Preserve the Workspace-configured directory,
legacy directories, identity fields, content kind, and file type. Use stable
event names across runs and attach `evidence_refs` for source material,
artifacts, approvals, or publication receipts.

Do not mark content generated, approved, published, delivered, or failed before
the responsible system confirms it. Never invent external IDs, URLs,
performance results, approvals, or publication state.
