#!/usr/bin/env python3
"""How capability searches turned out over a recent window (read-only).

Reads tool_call_logs only (nothing is recorded for this): each search's query
is re-ranked against the current catalog and followed by the model's next
calls in the same conversation.

  PYTHONPATH=. python scripts/discovery_search_report.py --days 7 [--entity <id>]

Prints outcome counts, the found rate, and missed queries with the tool the
model eventually used: candidate example queries for that capability's search
metadata (packages/core/ai/runtime/capability_search_metadata.json, or a
Skill's config.json "search").
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from packages.core.database import async_session
from packages.core.services.discovery_search_report import discovery_search_report


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--entity", default=None)
    parser.add_argument("--max-misses", type=int, default=50)
    args = parser.parse_args()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    async with async_session() as db:
        report = await discovery_search_report(
            db, since=since, entity_id=args.entity, max_misses=args.max_misses,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
