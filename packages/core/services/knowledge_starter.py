"""Helpers for setup-generated workspace starter knowledge documents."""
from __future__ import annotations

import re
from typing import Any


STARTER_DOCUMENT_ACTIVE_STATUSES = {"scheduled", "generating", "ready"}


def starter_knowledge_task_key(group_name: str | None) -> str:
    """Return the Strategist task key reserved for a starter doc request."""
    primary = re.split(r"\s+(?:&|and)\s+", str(group_name or "knowledge"), maxsplit=1)[0]
    primary = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "", primary)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", primary.lower())
    slug = re.sub(r"_+", "_", slug).strip("_") or "knowledge"
    return f"seed_{slug[:64]}_knowledge"


def with_starter_document_settings(
    settings: dict[str, Any] | None,
    *,
    group_name: str,
    status: str = "scheduled",
    document_id: str | None = None,
) -> dict[str, Any]:
    """Merge starter-doc metadata into a DocumentGroup settings dict."""
    out = dict(settings or {})
    starter = dict(out.get("starter_document") or {})
    starter.setdefault("mode", "auto_generate")
    starter.setdefault("task_key", starter_knowledge_task_key(group_name))
    starter["status"] = status
    if document_id:
        starter["document_id"] = document_id
    out["starter_document"] = starter
    out["generate_starter_doc"] = True
    return out


def starter_document_state(
    settings: dict[str, Any] | None,
    *,
    group_name: str,
    document_count: int = 0,
) -> dict[str, str] | None:
    """Return normalized starter-doc state for Strategist context."""
    cfg = dict(settings or {})
    starter = dict(cfg.get("starter_document") or {})
    has_starter = bool(starter) or bool(cfg.get("generate_starter_doc"))
    if not has_starter:
        return None
    status = str(starter.get("status") or "").strip().lower()
    if not status:
        status = "ready" if document_count > 0 else "scheduled"
    return {
        "status": status,
        "task_key": str(
            starter.get("task_key") or starter_knowledge_task_key(group_name)
        ),
    }
