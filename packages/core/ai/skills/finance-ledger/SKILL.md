---
name: finance-ledger
description: Read, query, and record validated income, expense, invoice, bill, payment, refund, transfer, adjustment, and journal-entry facts in the active Workspace Finance Ledger.
---

# Finance Ledger

Use the Finance Ledger as the Workspace's append-only financial database. The
AI may plan or operate connected financial workflows, but the Ledger stores
only verified entries and their evidence.

## Read and query

- Use `query_ledger` with `ledger="finance_ledger"` for filtered records,
  grouping, totals, pagination, and date ranges.
- Use `read_finance_ledger` when exact entries, reversals, supersessions, or the
  complete validated projection are required.
- Use `visualize_workspace_ledgers` only for a useful user-facing summary.

Read before recording when the source record, invoice, payment, or
idempotency key may already exist.

## Record

Use `record_finance_ledger` only after the amount, currency, direction,
account, entry type, and source fact are known. Store money in integer minor
units and currency as an ISO 4217 code. Use stable references for accounts,
counterparties, invoices, payments, source systems, and source record IDs.

Use a deterministic `idempotency_key` for the same financial fact. Attach
`evidence_refs` to receipts, invoices, statements, approvals, or source-system
records. Use `payload` only for additional facts not represented by typed
fields, preserving established snake_case names.

Never edit history conceptually. Correct a recorded entry with
`reverses_entry_id` or `supersedes_entry_id` as appropriate. Do not mark a
planned, pending, paid, posted, or reconciled state without evidence. Never
invent amounts, dates, tax, fees, account mappings, or reconciliation results.
