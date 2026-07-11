---
name: mcp_quickbooks
description: Operate the user's QuickBooks Online company through the QuickBooks MCP. Use when the user asks to look up or create customers and invoices, send an invoice, review payments/bills/accounts/vendors, or run a financial report.
version: 1.0.0
---

# QuickBooks Runtime Skill

Use this skill to operate the user's **connected QuickBooks Online** company through the QuickBooks MCP (`mcp__quickbooks__*`). Every call requires a `realm_id` (the company identifier).

## When To Use

Use QuickBooks when the user asks about their accounting data — customers, invoices, payments, items, accounts, vendors, bills — or wants to create/send an invoice or run a financial report.

## Connection

Authenticates via Intuit OAuth. Every tool needs `realm_id` (the connected company). On an auth error, stop and ask the user to reconnect. `get_company_info` confirms the company (name, fiscal year, currency).

## Core Tools

Read / query:
- `get_company_info` (req `realm_id`).
- `query_customers` / `get_customer`, `query_invoices` / `get_invoice`, `query_payments` / `get_payment`.
- `query_items`, `query_accounts`, `query_vendors`, `query_bills`.
- `run_report` (req `realm_id`,`report_name` — e.g. `ProfitAndLoss`, `BalanceSheet`).
- `custom_query` (req `realm_id`,`sql` — raw QBO SQL).

Write (high-impact — see Guardrails):
- `create_customer` (req `realm_id`,`display_name`).
- `create_invoice` (req `realm_id`,`customer_id`).
- `send_invoice` (req `realm_id`,`invoice_id`) — **emails the invoice to the customer**.

## Common Recipes

**Create and send an invoice**
1. `query_customers` → the `customer_id` (or `create_customer`). 2. `query_items` for the line-item references. 3. **Show the full invoice (customer, line items, amounts) and get explicit approval.** 4. `create_invoice`, then `send_invoice` only after the user confirms sending.

**Run a P&L**
1. Confirm the period. 2. `run_report` with `report_name: ProfitAndLoss` and the date range; summarize.

## Guardrails

- **This is the company's financial system of record. Treat every write as high-impact.** Confirm exact details before `create_customer` / `create_invoice`.
- **`send_invoice` emails a real customer and creates a payable obligation — never send without explicit approval** of recipient + amounts. Creating an invoice and sending it are two deliberate steps.
- `custom_query` runs raw SQL — keep it read-only (SELECT); do not use it to mutate data.
- Amounts/currency: confirm against `get_company_info` currency; double-check totals before creating an invoice.

## Edge Cases & Errors

- `realm_id` is mandatory on every call — resolve the connected company first; don't guess it.
- QBO SQL (`custom_query`) is a SQL-like dialect, not full SQL — prefer the typed `query_*` tools when they cover the need.
- Report names are fixed identifiers (`ProfitAndLoss`, `BalanceSheet`, …) — use a valid one.
- Auth errors → stop and ask the user to reconnect.
