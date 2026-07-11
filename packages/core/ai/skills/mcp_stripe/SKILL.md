---
name: mcp_stripe
description: Operate Stripe through the official remote Stripe MCP (mcp.stripe.com). Use when the user asks to manage Stripe customers, products/prices, payment links, invoices, refunds, subscriptions, coupons, or disputes, check their balance, or look up Stripe documentation.
version: 1.0.0
---

# Stripe Runtime Skill

Use this skill to operate **Stripe** through the official **remote** Stripe MCP at `mcp.stripe.com` (`mcp__stripe__*`). This is real money movement — treat every write as high-impact.

> Tools are discovered from `mcp.stripe.com` at runtime; the set below is the published Stripe MCP surface and may evolve. If a tool you expect isn't present, list what's available rather than guessing a name.

## When To Use

Use Stripe when the user wants to read or manage their Stripe account: customers, products/prices, payment links, invoices, refunds, subscriptions, coupons, disputes, balance, or to search Stripe's docs/knowledge base.

## Connection

Stripe connects via **OAuth** to the remote MCP. On an auth error, stop and ask the user to (re)connect Stripe. **Be sure which mode the connection is in (test vs live)** — actions in live mode move real money. If unsure, say so before any write.

## Core Tools

Read / lookup:
- `retrieve_balance`, `list_customers`, `list_products`, `list_prices`, `list_invoices`, `list_payment_intents`, `list_subscriptions`, `list_coupons`, `list_disputes`, `search_documentation` (Stripe docs/knowledge base).

Create / configure:
- `create_customer`, `create_product`, `create_price`, `create_payment_link`, `create_invoice`, `create_invoice_item`, `finalize_invoice`, `create_coupon`.

Money / lifecycle (highest impact — see Guardrails):
- `create_refund`, `cancel_subscription`, `update_subscription`, `update_dispute`.

## Common Recipes

**Create a payment link for a product**
1. `create_product` → `create_price` (amount + currency). 2. `create_payment_link` for that price. 3. Return the URL.

**Invoice a customer**
1. `list_customers` / `create_customer`. 2. `create_invoice` (draft) → `create_invoice_item` for each line. 3. **Confirm the amounts.** 4. `finalize_invoice` to issue it.

**Refund a payment**
1. `list_payment_intents` to find the charge. 2. **Confirm the exact payment + amount with the user.** 3. `create_refund`.

**Answer a "how do I…" Stripe question**
1. `search_documentation` and cite the result.

## Guardrails

- **`create_refund` moves money back to a customer — never run it without explicit confirmation of the exact payment and amount.** No speculative or test refunds in live mode.
- **`finalize_invoice` issues a real invoice** (can trigger charging/collection) — confirm line items and customer first; `create_invoice` alone leaves it as a safe draft.
- `cancel_subscription` / `update_subscription` change a customer's billing — confirm which subscription and the effect (proration, immediate vs period-end).
- `create_payment_link` produces a live, shareable checkout — confirm price/currency before sharing.
- `update_dispute` submits evidence to a dispute with deadlines — get the evidence right; you usually can't resubmit.
- **Verify test vs live mode** before any write; call out when an action is irreversible.
- Don't expose secret keys; the OAuth connection handles auth.

## Edge Cases & Errors

- Amounts are in the smallest currency unit (e.g. cents) — get the unit right or you'll over/undercharge.
- A draft invoice (`create_invoice`) isn't sent until `finalize_invoice`; don't tell the user it's issued before then.
- Idempotency: a failed-but-maybe-applied write should be re-checked with a `list_*` before retrying, so you don't double-charge/double-refund.
- Auth/permission errors (restricted key scope, mode mismatch) → stop and tell the user; don't retry blindly.
