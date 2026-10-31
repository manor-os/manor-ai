#!/usr/bin/env python3
"""Fill the Blueprint release history and name each install's release.

Every step is idempotent; rerunning writes nothing new.

    python scripts/backfill_blueprint_registry.py              # packages + current releases
    python scripts/backfill_blueprint_registry.py --git-history  # + platform configs from git
    python scripts/backfill_blueprint_registry.py --adopt        # + _blueprint.package on installs
    python scripts/backfill_blueprint_registry.py --all --dry-run
    python scripts/backfill_blueprint_registry.py --adopt --synthesize-baselines

``--git-history`` reads this checkout's git history, so run it from a full
clone (not the slim image) with DATABASE_URL pointing at the target database.

``--synthesize-baselines`` gives each exact install without a v3 baseline one
built from its release, which changes what its next upgrade preview offers.
It is not part of ``--all``; run it once ``--adopt`` has named the releases.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path

from packages.core.blueprints.registry_backfill import (
    adopt_workspace,
    adoptable_workspace_ids,
    backfill_registry,
    recover_git_history,
    synthesizable_workspace_ids,
    synthesize_workspace_baseline,
)
from packages.core.database import async_session

ROOT = Path(__file__).resolve().parents[1]
ADOPT_BATCH = 100


async def _per_workspace(db, workspace_ids, step, *, dry_run: bool) -> dict[str, int]:
    outcomes: Counter[str] = Counter()
    for start in range(0, len(workspace_ids), ADOPT_BATCH):
        for workspace_id in workspace_ids[start:start + ADOPT_BATCH]:
            outcomes[await step(db, workspace_id)] += 1
        # Short transactions: each batch holds its Workspace rows locked.
        if not dry_run:
            await db.commit()
    return dict(outcomes)


async def run(*, git_history: bool, adopt: bool, synthesize_baselines: bool, dry_run: bool) -> dict:
    report: dict = {"dry_run": dry_run}
    async with async_session() as db:
        report["backfill"] = await backfill_registry(db)
        if git_history:
            report["git_history"] = await recover_git_history(db, ROOT)
        if not dry_run:
            await db.commit()

        if adopt:
            report["adopt"] = await _per_workspace(
                db, await adoptable_workspace_ids(db), adopt_workspace, dry_run=dry_run,
            )
        if synthesize_baselines:
            report["synthesize_baselines"] = await _per_workspace(
                db, await synthesizable_workspace_ids(db), synthesize_workspace_baseline, dry_run=dry_run,
            )
        # A dry run reports from one transaction and writes nothing.
        await db.rollback()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--git-history", action="store_true")
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--all", action="store_true", help="--git-history and --adopt")
    parser.add_argument(
        "--synthesize-baselines", action="store_true",
        help="build missing v3 baselines for exact installs; changes their next upgrade preview",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without committing")
    args = parser.parse_args()
    report = asyncio.run(run(
        git_history=args.git_history or args.all,
        adopt=args.adopt or args.all,
        synthesize_baselines=args.synthesize_baselines,
        dry_run=args.dry_run,
    ))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
