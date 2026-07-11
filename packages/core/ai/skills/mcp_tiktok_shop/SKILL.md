---
name: mcp_tiktok_shop
description: Operate the user's TikTok Shop through the TikTok Shop MCP. Use when the user asks to look up TikTok Shop orders or products, read order/product detail, or update SKU prices or inventory.
version: 1.0.0
---

# TikTok Shop Runtime Skill

Use this skill to operate the user's **connected TikTok Shop** through the TikTok Shop MCP (`mcp__tiktok_shop__*`). This is the commerce/seller surface — for posting TikTok videos use `mcp_tiktok`.

## When To Use

Use TikTok Shop when the user asks about their shop's orders or products, or wants to change SKU pricing or inventory.

## Connection

Authenticates via TikTok Shop OAuth. Most calls are shop-scoped — start with `get_authorized_shops` to get the shop identifier (returns each shop's cipher) this token can access. On an auth error, stop and ask the user to reconnect.

## Core Tools

Read:
- `get_authorized_shops` — shops this token can access.
- `search_orders`, `get_order_detail` (req `order_ids`).
- `search_products`, `get_product` (req `product_id`).

Write (high-impact — see Guardrails):
- `update_price` (req `product_id`,`skus` — per-SKU prices).
- `update_inventory` (req `product_id`,`skus` — per-SKU stock).

## Common Recipes

**Look up an order**
1. `get_authorized_shops` if the shop is ambiguous. 2. `search_orders` to find it. 3. `get_order_detail` with `order_ids`.

**Update SKU pricing**
1. `get_product` to read current SKUs + prices. 2. **Confirm the new per-SKU prices with the user.** 3. `update_price` with the `skus` payload.

**Adjust inventory**
1. `get_product` for current SKU stock. 2. Confirm the target quantities. 3. `update_inventory`.

## Guardrails

- **`update_price` and `update_inventory` are storefront-affecting and SKU-level** — confirm the exact SKU → value mapping before writing; a mismatched SKU edits the wrong variant.
- Read `get_product` first so you have the correct SKU IDs and current values; never guess SKU identifiers.
- Confirm the shop (cipher) when the token has access to multiple shops.
- Act only on the SKUs the user named; don't bulk-reprice unprompted.

## Edge Cases & Errors

- A product has multiple SKUs (variants); price/inventory updates are per-SKU, not per-product — be explicit about which SKUs.
- `get_order_detail` takes `order_ids` (can be several) — batch reads rather than looping one-by-one where possible.
- Auth errors → stop and ask the user to reconnect.
