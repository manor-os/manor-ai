"""Post-deploy step: derive provider trial access for backfilled grants.

Migration 20261015_01 backfills one ``provider_trial`` WorkspaceCollaboration
for every accepted delivery still in trial, but leaves its authority
fingerprint empty and writes no derived rows, so those providers have no
access (fail closed) until this runs. It fills the fingerprint and re-derives
the collaborator membership and owner seat for every live access grant, one
grant per commit. Re-running is safe: already-reconciled grants are rewritten
to the same state.

Usage
-----
    # Dry run: count the grants it would visit, write nothing
    PYTHONPATH=. python scripts/reconcile_provider_trial_access.py --dry-run

    # Apply
    PYTHONPATH=. python scripts/reconcile_provider_trial_access.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from packages.core.services.external_collaboration_access import (
    provider_trial_reconcile_candidates,
    reconcile_provider_trial_access,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("reconcile_provider_trial_access")


async def run(db, *, dry_run: bool) -> dict[str, int]:
    candidates = await provider_trial_reconcile_candidates(db)
    summary = {
        "grants": len(candidates),
        "missing_fingerprint": sum(1 for _, missing in candidates if missing),
        "reconciled": 0,
    }
    logger.info(
        "%sfound %d grants to reconcile (%d backfilled trial grants without a fingerprint)",
        "DRY " if dry_run else "", summary["grants"], summary["missing_fingerprint"],
    )
    if not dry_run:
        summary["reconciled"] = await reconcile_provider_trial_access(db)
        logger.info("reconciled %d grants", summary["reconciled"])
    return summary


async def main(*, dry_run: bool) -> int:
    from packages.core.config import get_settings

    engine = create_async_engine(get_settings().DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as db:
            await run(db, dry_run=dry_run)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, no DB writes")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run)))
