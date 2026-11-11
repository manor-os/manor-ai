#!/usr/bin/env python3
"""Run the App self-check over every existing Workspace page and report what it finds.

Read-only: each page is checked inside a read-only savepoint that is rolled
back, and nothing is committed. It runs the same check as
``test_workspace_app_view``, as each entity's owner, so its failure counts show
how often L1 would send a page back before WORKSPACE_APP_SELF_CHECK_BLOCKS is
turned on. Reads prove that a page's views read back, not that clicking works.

  PYTHONPATH=. python scripts/audit_workspace_app_pages.py --output report.json
  PYTHONPATH=. python scripts/audit_workspace_app_pages.py --designed-only --entity-id <id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from typing import Any

from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.user import User
from packages.core.models.workspace import Workspace
from packages.core.services.workspace_app_selfcheck import self_check_app_page

logger = logging.getLogger(__name__)


async def audit_page(workspace_id: str) -> dict[str, Any]:
    """One page's row; a page that cannot be checked says why and the audit goes on."""
    async with async_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        row = {"workspace_id": workspace.id, "entity_id": workspace.entity_id, "name": workspace.name,
               "page": "designed" if isinstance((workspace.operating_model or {}).get("app_view"), dict) else "default"}
        owner = (await db.execute(select(User).where(
            User.entity_id == workspace.entity_id, User.role == "owner", User.status == "active",
            User.deleted_at.is_(None),
        ).order_by(User.created_at, User.id).limit(1))).scalars().first()
        if owner is None:
            return {**row, "status": "skipped", "reason": "the entity has no active owner to read as"}
        try:
            report = await self_check_app_page(db, workspace, user=owner)
        except Exception as exc:  # noqa: BLE001 - one page must not end the audit
            logger.warning("App self-check of Workspace %s could not run", workspace_id, exc_info=True)
            return {**row, "status": "skipped", "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
        finally:
            await db.rollback()
    return {**row, **{key: report[key] for key in ("status", "summary", "counts", "errors", "warnings", "read_failures")}}


async def audit_pages(*, entity_id: str | None = None, workspace_id: str | None = None, designed_only: bool = False,
                      limit: int | None = None) -> dict[str, Any]:
    """Every selected page's row, and the totals to sample false positives from."""
    query = select(Workspace.id, Workspace.operating_model).where(Workspace.deleted_at.is_(None)).order_by(Workspace.id)
    if entity_id:
        query = query.where(Workspace.entity_id == entity_id)
    if workspace_id:
        query = query.where(Workspace.id == workspace_id)
    async with async_session() as db:
        rows = (await db.execute(query)).all()
    selected = [row.id for row in rows
                if not designed_only or isinstance((row.operating_model or {}).get("app_view"), dict)][:limit]
    pages = [await audit_page(selected_id) for selected_id in selected]
    totals = Counter(page["status"] for page in pages)
    return {
        "pages": len(pages),
        "passed": totals["passed"], "failed": totals["failed"], "skipped": totals["skipped"],
        # What each level would have sent back, apart: L1 is what the blocking setting turns on.
        "failed_static": sum(bool(page.get("errors")) for page in pages),
        "failed_reads": sum(bool(page.get("read_failures")) for page in pages),
        "limits": "Reads only: nothing was clicked or started.",
        "results": pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--entity-id")
    parser.add_argument("--workspace-id")
    parser.add_argument("--designed-only", action="store_true", help="skip Workspaces that show the default page")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", help="write the whole report here; only the totals are printed")
    args = parser.parse_args()
    report = asyncio.run(audit_pages(entity_id=args.entity_id, workspace_id=args.workspace_id,
                                     designed_only=args.designed_only, limit=args.limit))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
        report = {key: value for key, value in report.items() if key != "results"}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
