"""Preview or apply metadata-only Knowledge filing repair for one Workspace.

PYTHONPATH=. .venv/bin/python scripts/repair_workspace_document_projection.py \
    --entity-id ENTITY --workspace-id WORKSPACE [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json

from packages.core.database import async_session
from packages.core.services.knowledge_sync import repair_workspace_document_projection


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Commit logical filing changes; default is a rolled-back preview")
    args = parser.parse_args()
    async with async_session() as db:
        result = await repair_workspace_document_projection(
            db, entity_id=args.entity_id, workspace_id=args.workspace_id,
        )
        if args.apply:
            await db.commit()
        else:
            await db.rollback()
    print(json.dumps({"applied": args.apply, **asdict(result)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
