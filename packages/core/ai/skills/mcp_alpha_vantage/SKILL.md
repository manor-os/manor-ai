---
name: mcp_alpha_vantage
description: Read global equity quotes, daily price history, company overviews, and reported fundamentals through Alpha Vantage.
version: 1.0.0
---

# Alpha Vantage Runtime Skill

Use this skill for bounded, read-only market and company research.

## Workflow

1. Use `search_symbols` when the ticker or exchange is ambiguous.
2. Use `get_quote` for the latest returned quote and `get_daily_series` for history.
3. Use `get_company_overview` for normalized company metadata and `get_fundamentals` for one explicitly requested reported dataset.
4. Report symbol, exchange, currency, data date, and source limitations.

## Guardrails

- Do not infer real-time execution prices from Alpha Vantage responses.
- Request one fundamentals dataset at a time to avoid unnecessary API-credit use.
- Distinguish reported fundamentals from estimates and calculated conclusions.
- This Integration is read-only and does not authorize trades.
