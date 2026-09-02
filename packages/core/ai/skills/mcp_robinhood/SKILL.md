---
name: mcp_robinhood
description: Use the connected user's official Robinhood Trading MCP for portfolio, watchlist, and market research.
---

# Robinhood

Discover the current account's tools with `search_tools` before using them.
There are no guessed fallback schemas. If discovery or authorization fails,
ask the user to connect or reconnect Robinhood in Integrations.

Keep every request on the selected user's authorized account. Do not treat a
platform client ID, another user's token, or a successful configuration check
as account authorization. OAuth's `internal` scope is not a read-only grant.

Prefer account and market-data reads for research. A request for monitoring,
analysis, or a summary does not authorize orders, cancellations, watchlist
changes, transfers, or account opening. Writes must follow Manor's existing
approval policy with the exact account and arguments; do not bypass the gate.
After an ambiguous financial operation, inspect its status before retrying.

Robinhood account access does not grant a commercial redistribution license.
Report data timestamps and distinguish actual results from proposed actions.
