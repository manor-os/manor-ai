"""One-shot backfill: translate existing Workspaces' authored text for each viewer's language.

New edits are translated as they are saved (``services/workspace_translation``).
Workspaces written before that read in their authoring language only; this
queues the same background pass for them. A pass translates only what is
missing or changed and never writes over a translation a person made, so
re-running is safe.

Usage
-----
    # Dry-run: list what each Workspace would translate, write nothing
    PYTHONPATH=. python scripts/backfill_workspace_translations.py --dry-run

    # Queue every Workspace (or one account / one Workspace)
    PYTHONPATH=. python scripts/backfill_workspace_translations.py
    PYTHONPATH=. python scripts/backfill_workspace_translations.py --entity 01J...
    PYTHONPATH=. python scripts/backfill_workspace_translations.py --workspace 01J...

    # Translate inline instead of queueing (no worker needed)
    PYTHONPATH=. python scripts/backfill_workspace_translations.py --workspace 01J... --inline
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def _workspace_ids(entity_id: str | None, workspace_id: str | None) -> list[str]:
    from sqlalchemy import select

    from packages.core.database import create_worker_session
    from packages.core.models.workspace import Workspace

    query = select(Workspace.id).where(Workspace.deleted_at.is_(None)).order_by(Workspace.id)
    if entity_id:
        query = query.where(Workspace.entity_id == entity_id)
    if workspace_id:
        query = query.where(Workspace.id == workspace_id)
    async with create_worker_session()() as db:
        return list((await db.execute(query)).scalars())


async def _pending(workspace_id: str) -> dict[str, int]:
    from packages.core.database import create_worker_session
    from packages.core.services.workspace_translation import _load, workspace_anchors

    async with create_worker_session()() as db:
        loaded = await _load(db, workspace_id)
    if loaded is None:
        return {}
    counts: dict[str, int] = {}
    for anchor in workspace_anchors(*loaded):
        for language, texts in anchor.wanted().items():
            counts[language] = counts.get(language, 0) + len(texts)
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--entity")
    parser.add_argument("--workspace")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--inline", action="store_true", help="translate here instead of queueing")
    args = parser.parse_args()

    from packages.core.services.workspace_translation import (
        schedule_workspace_translation,
        translate_workspace_text_once,
    )

    ids = await _workspace_ids(args.entity, args.workspace)
    queued = 0
    for workspace_id in ids:
        pending = await _pending(workspace_id)
        if not pending:
            continue
        print(workspace_id, pending)
        queued += 1
        if args.dry_run:
            continue
        if args.inline:
            print("  ", await translate_workspace_text_once(workspace_id))
        else:
            schedule_workspace_translation(workspace_id)
    print(f"{len(ids)} Workspaces checked, {queued} {'to translate' if args.dry_run else 'translated' if args.inline else 'queued'}")


if __name__ == "__main__":
    asyncio.run(main())
