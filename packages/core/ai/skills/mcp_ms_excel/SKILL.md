---
name: mcp_ms_excel
description: Read and edit the user's Excel workbooks through the Microsoft Excel MCP. Use when the user asks to read or write spreadsheet ranges/cells, work with Excel tables, manage worksheets or named ranges, or recalculate a workbook stored in OneDrive / SharePoint.
version: 1.0.0
---

# Microsoft Excel Runtime Skill

Use this skill to read and edit the user's **Excel workbooks** through the Excel MCP (`mcp__ms_excel__*`), backed by Microsoft Graph. Every call targets a workbook by `file_id` (the OneDrive/SharePoint item id).

## When To Use

Use Excel when the user asks to read or change spreadsheet data, append rows to a table, add/rename worksheets, work with named ranges, or recalculate a workbook. To find the workbook's `file_id` first, use `mcp_onedrive` (`search_files`).

## Connection

Authenticates via Microsoft OAuth. On an auth/scope error, stop and ask the user to reconnect. You need the workbook `file_id` up front — resolve it via OneDrive search if the user names a file.

## Core Tools

Worksheets:
- `list_worksheets` (req `file_id`), `add_worksheet`, `rename_worksheet` (req `…`,`worksheet`,`new_name`), `delete_worksheet` (req `…`,`worksheet`).

Cells / ranges:
- `read_range` (req `file_id`,`worksheet`,`address` — A1 like `A1:D20`), `read_used_range` (all populated cells on a sheet).
- `write_range` (req `…`,`address`,`values` — a 2D array), `update_cell` (single cell convenience), `clear_range` (req `…`,`address`).

Tables (structured):
- `list_tables`, `get_table_rows` (req `…`,`table`), `add_table_rows` (req `…`,`table`,`values` — append), `create_table` (promote a range, req `…`,`worksheet`,`address`).

Other:
- `calculate` (recalc, req `file_id`), `list_named_items`, `get_named_item_range` (req `…`,`name`).

## Common Recipes

**Read a region**
1. `list_worksheets` to confirm the sheet name. 2. `read_range` with an A1 `address`, or `read_used_range` to grab everything populated.

**Append rows to a table**
1. `list_tables` → the `table` name. 2. **Confirm the rows with the user.** 3. `add_table_rows` with a 2D `values` array (matching the table's columns).

**Bulk-write data**
1. `read_range`/`read_used_range` to understand current layout. 2. Confirm the target `address` won't clobber needed data. 3. `write_range` with a 2D `values` array. 4. `calculate` if formulas depend on the new values.

## Guardrails

- **`write_range`, `update_cell`, and `clear_range` overwrite existing cells in place — there is no undo here.** Read the target range first, confirm the exact `address`, and never write to a wider range than needed.
- **`values` must be a 2D array** whose shape matches the target range / table columns — a shape mismatch silently writes the wrong cells. State the shape before writing.
- `delete_worksheet` / `clear_range` are destructive — confirm first; prefer appending (`add_table_rows`) over overwriting.
- After writing values that feed formulas, run `calculate` so reads reflect the recalculated result.

## Edge Cases & Errors

- `address` is A1 notation (`Sheet`-scoped via the `worksheet` arg) — don't include the sheet name in the address.
- `read_used_range` can be large on big sheets — prefer a bounded `read_range` when you know the region.
- Table appends must match the table's column count/order; read `get_table_rows` once to learn the schema.
- Auth/scope errors → stop and ask the user to reconnect.
