---
name: mcp_shopify
description: Operate the user's Shopify store through the Shopify MCP. Use when the user asks to look up shop info, products, orders, or customers, or to create/update products, tag orders, add customers, or adjust inventory.
version: 1.0.0
---

# Shopify Runtime Skill

Use this skill to operate the user's **connected Shopify store** through the Shopify MCP (`mcp__shopify__*`).

## When To Use

Use Shopify when the user asks about their store, products, orders, customers, or inventory, or wants to create/update catalog items, tag orders, or change stock levels.

## Connection

Authenticates via Shopify OAuth. On an auth error, stop and ask the user to reconnect their store. `get_shop` confirms which store is connected (currency, domain) before acting.

## Core Tools

Read:
- `get_shop` — store profile (currency, domain, plan).
- `list_products` / `get_product` (req `product_id`).
- `list_orders` / `get_order` (req `order_id`).
- `list_customers`.

Write (high-impact — see Guardrails):
- `create_product` — required: `title`.
- `update_product` — required: `product_id` (title, price, status, etc.).
- `add_order_tags` — required: `order_id`, `tags`.
- `create_customer`.
- `adjust_inventory` — required: `inventory_item_id`, `location_id`, `delta` (relative change, +/-).

## Common Recipes

**Order lookup / tagging**
1. `list_orders` (filter to the target) → `get_order` to read line items + status.
2. To flag/segment, `add_order_tags` with the order's `order_id` and the agreed `tags`.

**Update a product price/status**
1. `get_product` to read current values (confirm against `get_shop` currency).
2. **Confirm the new value with the user**, then `update_product` with `product_id`.

**Restock / correct inventory**
1. `get_product` to find the `inventory_item_id`; identify the `location_id`.
2. Confirm the **delta** (it is relative, not an absolute count). 3. `adjust_inventory`.

## Guardrails

- **Storefront-affecting writes require explicit confirmation**: `update_product` (esp. price/status — wrong values are publicly visible and affect sales), `adjust_inventory`, `create_product`. Echo the exact field + new value before writing.
- **`adjust_inventory` `delta` is relative** (+10 / -3), not a target quantity — confirm you have the direction and magnitude right; a sign error oversells or hides stock.
- Confirm against `get_shop` currency before quoting/setting prices.
- Don't bulk-edit the catalog or orders unprompted; act on the specific items the user named.

## Edge Cases & Errors

- IDs differ: a product has a `product_id`, but inventory adjustments key on `inventory_item_id` + `location_id` — resolve these from `get_product` first.
- A store can have multiple locations — confirm the `location_id` before adjusting stock.
- `update_product` to `status: active` publishes it to the storefront — treat publishing as high-impact.
- Auth errors → stop and ask the user to reconnect.
