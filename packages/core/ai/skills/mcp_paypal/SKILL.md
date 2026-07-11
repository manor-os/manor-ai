---
name: mcp_paypal
description: Operate PayPal through the official remote PayPal MCP (mcp.paypal.com). Use when the user asks to create/send invoices, manage orders and payments, issue refunds, handle disputes, manage products or subscriptions, track shipments, or read transactions.
version: 1.0.0
---

# PayPal Runtime Skill

Use this skill to operate **PayPal** through the official **remote** PayPal MCP at `mcp.paypal.com` (`mcp__paypal__*`). This is real money movement — treat every write as high-impact.

> Tools are served by `mcp.paypal.com` at runtime; the set below is the published PayPal MCP surface and may evolve. If an expected tool is missing, report what's available rather than inventing a name.

## When To Use

Use PayPal when the user wants to manage their PayPal merchant account: invoices, orders/payments, refunds, disputes, products, subscription plans/subscriptions, shipment tracking, or transaction history.

## Connection

PayPal connects via **OAuth** to the remote MCP. On an auth error, stop and ask the user to (re)connect. Confirm whether the connection is **sandbox vs live** — live actions move real money.

## Core Tools

Invoicing:
- `create_invoice`, `list_invoices`, `get_invoice`, `send_invoice`, `send_invoice_reminder`, `cancel_sent_invoice`, `generate_invoice_qr_code`.

Orders / payments / refunds (highest impact — see Guardrails):
- `create_order`, `get_order`, `pay_order`, `create_refund`, `get_refund`.

Disputes:
- `list_disputes`, `get_dispute`, `accept_dispute_claim`.

Catalog & subscriptions:
- `create_product`, `list_products`, `show_product_details`, `create_subscription_plan`, `list_subscription_plans`, `show_subscription_plan_details`, `create_subscription`, `show_subscription_details`, `update_subscription`, `cancel_subscription`.

Shipping & reporting:
- `create_shipment_tracking`, `get_shipment_tracking`, `update_shipment_tracking`, `list_transactions`, `get_merchant_insights`.

## Common Recipes

**Invoice a customer**
1. `create_invoice` (draft) with line items + recipient. 2. **Confirm amounts/recipient.** 3. `send_invoice` to deliver it; `send_invoice_reminder` later if unpaid.

**Refund a captured payment**
1. `list_transactions` / `get_order` to locate the payment. 2. **Confirm the exact payment + amount.** 3. `create_refund`; verify with `get_refund`.

**Set up a subscription**
1. `create_product` → `create_subscription_plan`. 2. `create_subscription` for the customer. 3. `show_subscription_details` to confirm.

## Guardrails

- **`create_refund` returns money to a buyer — never run it without explicit confirmation of the exact payment and amount.**
- **`pay_order` captures/settles a payment** (money moves) — confirm before executing; don't pay an order speculatively.
- `send_invoice` emails a real invoice and requests payment — confirm recipient + amounts; `create_invoice` alone is a safe draft. `cancel_sent_invoice` after sending is a customer-visible action.
- `accept_dispute_claim` concedes a dispute (you forfeit the funds) — only with explicit user approval.
- `cancel_subscription` / `update_subscription` change a buyer's recurring billing — confirm which subscription and the effect.
- **Verify sandbox vs live** before any write; call out irreversible actions.
- Don't expose credentials; the OAuth connection handles auth.

## Edge Cases & Errors

- A draft invoice isn't delivered until `send_invoice`; don't claim it was sent before then.
- Refund/capture failures may be partially applied — re-check with `get_refund` / `get_order` / `list_transactions` before retrying so you don't double-process.
- Subscriptions depend on a product + plan existing first — create them in order.
- Auth/permission/mode-mismatch errors → stop and tell the user; don't retry blindly.
