---
name: mcp_square
description: Operate the user's Square account through the Square MCP. Use when the user asks to look up locations, catalog items, orders, or customers, create catalog items or customers, or read/adjust inventory counts on Square.
version: 1.0.0
---

# Square Runtime Skill

Use this skill to operate the user's **connected Square** account through the Square MCP (`mcp__square__*`).

## When To Use

Use Square when the user asks about their Square locations, catalog, orders, customers, or inventory, or wants to create a catalog item/customer or adjust stock.

## Connection

Authenticates via Square OAuth. On an auth error, stop and ask the user to reconnect. Most operations are location-scoped — start with `list_locations` to get the `location_id` you'll work against.

## Core Tools

Read:
- `list_locations`.
- `search_catalog_items`, `get_catalog_object` (req `object_id` — item/variation/etc.).
- `search_orders` (across locations), `get_order` (req `order_id`).
- `list_customers` / `get_customer` (req `customer_id`).
- `get_inventory` (req `catalog_object_id` — counts for a variation).

Write (high-impact — see Guardrails):
- `create_catalog_item` (req `name`,`price_amount` — minor units, e.g. cents).
- `create_customer`, `update_customer` (req `customer_id`).
- `adjust_inventory` (req `catalog_object_id`,`quantity`) at a location.

## Common Recipes

**Look up an order**
1. `search_orders` (scoped to a `location_id` from `list_locations`). 2. `get_order` for line items + totals.

**Add a catalog item**
1. Confirm name + price with the user (**`price_amount` is in minor units** — cents). 2. `create_catalog_item`.

**Correct inventory**
1. `get_inventory` for the variation's current count. 2. Confirm the adjustment. 3. `adjust_inventory` for the `catalog_object_id` at the location.

## Guardrails

- **Money is in minor units** (`price_amount` in cents) — confirm the human-readable price before `create_catalog_item`; a unit error is a 100x mistake.
- **Catalog/customer/inventory writes require confirmation**: `create_catalog_item`, `update_customer`, `adjust_inventory`.
- Inventory is **location-scoped** — confirm the `location_id` before adjusting; the same item has separate counts per location.
- Act on the specific item/customer the user named; don't bulk-edit unprompted.

## Edge Cases & Errors

- Catalog objects are addressed by `object_id` / `catalog_object_id` (variation-level for inventory) — resolve via search/get first.
- A merchant may have multiple locations — never assume one; confirm which.
- Auth errors → stop and ask the user to reconnect.
