---
name: mcp_stripe
description: Operate Stripe through the official remote Stripe MCP (mcp.stripe.com). Use when the user asks to read or update Stripe API resources, issue refunds, plan an integration, run reports, inspect the connected account, or search Stripe documentation.
version: 1.0.0
---

# Stripe Runtime Skill

Use this skill to operate **Stripe** through the official **remote** Stripe MCP at `mcp.stripe.com` (`mcp__stripe__*`). This is real money movement — treat every write as high-impact.

> Tools are discovered from `mcp.stripe.com` at runtime; the set below is the published Stripe MCP surface and may evolve. If a tool you expect isn't present, list what's available rather than guessing a name.

## When To Use

Use Stripe when the user wants to read or manage Stripe resources, issue refunds, generate reports, plan an integration, or search Stripe's docs/knowledge base.

## Connection

Stripe connects via **OAuth** to the remote MCP. On an auth error, stop and ask the user to (re)connect Stripe. **Be sure which mode the connection is in (test vs live)** — actions in live mode move real money. If unsure, say so before any write.

## Core Tools

Account and reads:
- `get_stripe_account_info`, `retrieve_balance`, and `list_*` inspect connected account resources.
- `search_stripe_resources` and `fetch_stripe_resources` locate Stripe resources.
- `search_stripe_documentation` answers implementation questions.

Writes:
- `create_customer`, `create_product`, `create_price`, `create_payment_link`, `create_coupon`, `create_invoice`, and `create_invoice_item` create resources.
- `finalize_invoice`, `update_dispute`, and `update_subscription` change lifecycle state.
- `create_refund` refunds a payment; `cancel_subscription` terminates a subscription.

Money / lifecycle (highest impact — see Guardrails):
- `create_refund`, `finalize_invoice`, `update_dispute`, `update_subscription`, and `cancel_subscription`.

## Common Recipes

**Create a payment link for a product**
1. `create_product`. 2. Confirm amount/currency. 3. `create_price`. 4. `create_payment_link`. 5. Return the URL.

**Invoice a customer**
1. Use `list_customers` or `create_customer`. 2. `create_invoice`. 3. `create_invoice_item`. 4. **Confirm the amounts.** 5. `finalize_invoice`.

**Refund a payment**
1. Discover and run the PaymentIntent read operation. 2. **Confirm the exact payment + amount with the user.** 3. `create_refund`.

**Answer a "how do I…" Stripe question**
1. `search_stripe_documentation` and cite the result.

## Guardrails

- **`create_refund` moves money back to a customer — never run it without explicit confirmation of the exact payment and amount.** No speculative or test refunds in live mode.
- **Create, update, finalize, refund, and cancel tools change real Stripe resources.** Show the exact effect and obtain confirmation before consequential writes.
- **Verify test vs live mode** before any write; call out when an action is irreversible.
- Don't expose secret keys; the OAuth connection handles auth.

## Edge Cases & Errors

- Amounts are in the smallest currency unit (e.g. cents) — get the unit right or you'll over/undercharge.
- A draft invoice is not issued until its finalize operation succeeds; do not report it as issued before then.
- Idempotency: a failed-but-maybe-applied write should be re-checked with `stripe_api_read` before retrying, so you don't double-charge or double-refund.
- Auth/permission errors (restricted key scope, mode mismatch) → stop and tell the user; don't retry blindly.
