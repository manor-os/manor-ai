#!/usr/bin/env python3
"""Audit URL-only MCP observation documents registered as artifacts.

Dry-run by default:
  PYTHONPATH=. python scripts/audit_misregistered_mcp_urls.py

Soft-delete confirmed candidates:
  PYTHONPATH=. python scripts/audit_misregistered_mcp_urls.py --apply

Legacy rows without origin metadata require an additional explicit opt-in:
  PYTHONPATH=. python scripts/audit_misregistered_mcp_urls.py --apply --include-legacy-source-only
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from packages.core.database import async_session
from packages.core.models.document import Document


OBSERVATION_SOURCES = frozenset({"browser_mcp", "chrome", "local_browser"})
OBSERVATION_TOOL_ACTIONS = frozenset({
    "get_content",
    "get_interactive_elements",
    "get_visible_dom",
    "get_web_content",
    "page_assets",
    "read_page",
    "visible_dom",
})


def _origin_tool_name(document: Any) -> str:
    metadata = getattr(document, "metadata_", None)
    origin = metadata.get("origin") if isinstance(metadata, dict) else None
    if not isinstance(origin, dict):
        return ""
    return str(origin.get("tool_name") or "").strip()


def _tool_action(tool_name: str) -> str:
    return tool_name.rsplit("__", 1)[-1] if tool_name else ""


def classify_candidate(
    document: Any,
    *,
    include_legacy_source_only: bool = False,
) -> str | None:
    if bool(getattr(document, "is_trashed", False)):
        return None
    if str(getattr(document, "source", "") or "") not in OBSERVATION_SOURCES:
        return None
    if getattr(document, "fs_path", None) is not None:
        return None
    if getattr(document, "file_size", None) is not None:
        return None

    file_url = str(getattr(document, "file_url", "") or "").strip()
    parsed_url = urlparse(file_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return None

    tool_name = _origin_tool_name(document)
    if _tool_action(tool_name) in OBSERVATION_TOOL_ACTIONS:
        return "observation_tool"
    if not tool_name and include_legacy_source_only:
        return "legacy_source_only"
    return None


def mark_candidate(
    document: Any,
    *,
    reason: str,
    cleaned_at: datetime,
) -> None:
    tool_name = _origin_tool_name(document)
    metadata = dict(getattr(document, "metadata_", None) or {})
    metadata["cleanup"] = {
        "reason": "mcp_observation_url_misregistered",
        "candidate_kind": reason,
        "cleaned_at": cleaned_at.isoformat(),
        "original_tool_name": tool_name or None,
    }
    document.metadata_ = metadata
    document.is_trashed = True
    document.trashed_at = cleaned_at
    document.trashed_by = "mcp-url-audit"


async def audit(
    *,
    entity_id: str | None,
    apply: bool,
    include_legacy_source_only: bool,
) -> dict[str, Any]:
    async with async_session() as db:
        query = select(Document).where(
            Document.source.in_(OBSERVATION_SOURCES),
            Document.fs_path.is_(None),
            Document.file_size.is_(None),
            Document.file_url.isnot(None),
            Document.is_trashed.is_(False),
        )
        if entity_id:
            query = query.where(Document.entity_id == entity_id)
        documents = list((await db.execute(query)).scalars().all())

        candidates: list[tuple[Document, str]] = []
        for document in documents:
            reason = classify_candidate(
                document,
                include_legacy_source_only=include_legacy_source_only,
            )
            if reason:
                candidates.append((document, reason))

        by_kind = Counter(reason for _, reason in candidates)
        by_host = Counter(urlparse(document.file_url or "").hostname or "unknown" for document, _ in candidates)
        print("Misregistered MCP URL document audit")
        print(f"  entity_id: {entity_id or '(all)'}")
        print(f"  mode: {'apply' if apply else 'dry-run'}")
        print(f"  candidates: {len(candidates)}")
        print(f"  by_kind: {dict(sorted(by_kind.items()))}")
        print(f"  by_host: {dict(sorted(by_host.items()))}")
        for document, reason in candidates[:100]:
            host = urlparse(document.file_url or "").hostname or "unknown"
            print(
                f"    doc={document.id} entity={document.entity_id} kind={reason} "
                f"tool={_origin_tool_name(document) or '(legacy)'} host={host} name={document.name!r}"
            )
        if len(candidates) > 100:
            print(f"    ... {len(candidates) - 100} more")

        if apply and candidates:
            cleaned_at = datetime.now(timezone.utc)
            touched_entities: set[str] = set()
            for document, reason in candidates:
                mark_candidate(document, reason=reason, cleaned_at=cleaned_at)
                touched_entities.add(document.entity_id)
            await db.commit()

            from packages.core.services.tool_cache_version import bump_tool_cache_version

            for touched_entity_id in touched_entities:
                await bump_tool_cache_version(touched_entity_id, "documents")
            print(f"  soft-deleted: {len(candidates)}")

        return {
            "mode": "apply" if apply else "dry-run",
            "candidate_count": len(candidates),
            "by_kind": dict(by_kind),
            "by_host": dict(by_host),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity-id", help="Limit the audit to one entity")
    parser.add_argument("--apply", action="store_true", help="Soft-delete matching documents")
    parser.add_argument(
        "--include-legacy-source-only",
        action="store_true",
        help="Also include URL-only browser documents that have no origin tool metadata",
    )
    args = parser.parse_args()
    asyncio.run(audit(
        entity_id=args.entity_id,
        apply=args.apply,
        include_legacy_source_only=args.include_legacy_source_only,
    ))


if __name__ == "__main__":
    main()
