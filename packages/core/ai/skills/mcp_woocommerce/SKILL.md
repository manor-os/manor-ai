---
name: mcp_woocommerce
description: Operate the user's WooCommerce store through the WooCommerce MCP. Use when the user asks to look up or manage products, orders, customers, stock, or order status on their WooCommerce (WordPress) shop.
version: 1.0.0
---

# WooCommerce Runtime Skill

Use this skill to operate the user's **connected WooCommerce store** through the WooCommerce MCP (`mcp__woocommerce__*`). For Shopify use `mcp_shopify`.

## When To Use

Use WooCommerce when the user asks about their WooCommerce/WordPress shop's products, orders, customers, or stock, or wants to create/update products or change an order's status.

## Connection

Authenticates with the store's WooCommerce REST API credentials. On an auth error, stop and ask the user to reconnect the store.

## Core Tools

Read:
- `list_products` / `get_product` (req `product_id`).
- `list_orders` / `get_order` (req `order_id`).
- `list_customers` / `get_customer` (req `customer_id`).

Write (high-impact — see Guardrails):
- `create_product` (req `name`), `update_product` (req `product_id`).
- `set_stock` (req `product_id`,`stock_quantity` — an **absolute** quantity).
- `update_order_status` (req `order_id`,`status` — drives the fulfill / cancel / refund flow).

## Common Recipes

**Fulfill or cancel an order**
1. `get_order` to read items + current status. 2. **Confirm the new status with the user.** 3. `update_order_status` with the target `status` (e.g. `completed`, `cancelled`, `refunded`).

**Update a product**
1. `get_product` to read current values. 2. Confirm the change. 3. `update_product`.

**Set stock**
1. `get_product` to read current managed stock. 2. Confirm the **target absolute quantity**. 3. `set_stock`.

## Guardrails

- **Storefront-affecting writes require explicit confirmation**: `update_product` (esp. price), `create_product`, `set_stock`.
- **`update_order_status` can trigger fulfillment, cancellation, or refunds** depending on the store's configuration — never change status speculatively; confirm the exact target status and its consequence with the user.
- **`set_stock` is an absolute quantity** (not a delta) — confirm the final number; a wrong value oversells or hides stock.
- Act only on the specific items the user named; don't bulk-edit unprompted.

## Edge Cases & Errors

- WooCommerce order statuses are store-defined (`processing`, `completed`, `on-hold`, `cancelled`, `refunded`, …) — use a valid status string; verify against `get_order` if unsure.
- A product may have variations; confirm you're editing the right product/variation `product_id`.
- Auth errors → stop and ask the user to reconnect.
