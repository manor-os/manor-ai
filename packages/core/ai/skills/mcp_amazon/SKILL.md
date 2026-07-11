---
name: mcp_amazon
description: Operate the user's Amazon Selling Partner account through the Amazon MCP. Use when the user asks to look up Amazon orders or order items, search the Amazon catalog, check FBA inventory, or read/update their own listings.
version: 1.0.0
---

# Amazon (Selling Partner) Runtime Skill

Use this skill to operate the user's **connected Amazon Selling Partner** account through the Amazon MCP (`mcp__amazon__*`).

## When To Use

Use Amazon when the user asks about their Amazon orders, the catalog, FBA inventory, or their own listings (read or update).

## Connection

Authenticates via Amazon Selling Partner OAuth, scoped to a marketplace. On an auth error, stop and ask the user to reconnect. Listings are keyed by your **SKU**; catalog items by **ASIN**.

## Core Tools

Orders:
- `get_orders` (filter by date/status), `get_order` (req `order_id`), `get_order_items` (req `order_id`).

Catalog & inventory:
- `search_catalog_items` (keywords/identifiers), `get_catalog_item` (req `asin`).
- `get_inventory_summaries` — FBA inventory for the marketplace.

Listings (high-impact — see Guardrails):
- `get_listing_item` (req `sku`) — read one of your listings.
- `patch_listing` (req `sku`,`product_type`,`patches`) — **partial** update (e.g. price, quantity).
- `put_listing` (req `sku`,`product_type`,`attributes`) — **create or fully replace** a listing.

## Common Recipes

**Check recent orders**
1. `get_orders` (filter by date/status). 2. `get_order` / `get_order_items` for detail.

**Adjust a listing's price**
1. `get_listing_item` (`sku`) to read current attributes + `product_type`. 2. **Confirm the new price with the user.** 3. `patch_listing` with a minimal `patches` set — do not use `put_listing` for a small change.

## Guardrails

- **`put_listing` creates or FULLY REPLACES a listing** — any attribute you omit can be wiped. Prefer `patch_listing` for targeted changes; only use `put_listing` when deliberately recreating a listing, and confirm the full attribute set first.
- **Listing edits are publicly visible and affect Buy Box / sales** — confirm the exact field + value (esp. price/quantity) before `patch_listing` / `put_listing`.
- Read `get_listing_item` first to learn the required `product_type` and current attributes; a wrong `product_type` rejects the update.
- Act on the specific SKU the user named; never bulk-edit listings unprompted.

## Edge Cases & Errors

- SKU (your listing) vs ASIN (catalog) are different identifiers — don't mix them.
- SP-API is marketplace-scoped; results reflect the connected marketplace only.
- Listing updates may be processed asynchronously — confirm via `get_listing_item` rather than assuming immediate effect.
- Auth errors → stop and ask the user to reconnect.
