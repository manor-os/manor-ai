---
name: mcp_twelve_data
description: Read current prices, quotes, bounded OHLCV history, and technical indicators through the read-only Twelve Data MCP.
version: 1.0.0
---

# Twelve Data Runtime Skill

Use this skill for read-only stocks, ETFs, forex, and crypto market data.

## Workflow

1. Resolve the exact symbol, exchange when needed, interval, timezone, and requested date range.
2. Use `get_price` for one current price, `get_quote` for current statistics, and `get_time_series` for OHLCV history.
3. Use `get_technical_indicator` only for one named indicator with explicit parameters.
4. Report the returned symbol, interval, timezone, timestamps, and currency.

## Guardrails

- Do not present a technical indicator as a guaranteed signal or recommendation.
- Keep series bounded and do not silently substitute a different symbol or exchange.
- State when the provider reports delayed, sparse, or unavailable data.
- This Integration cannot place orders or access brokerage accounts.
