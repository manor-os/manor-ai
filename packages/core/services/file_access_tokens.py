"""Short-lived signed URLs for provider-readable entity files."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

from packages.core.config import get_settings
from packages.core.services.knowledge_visibility import normalize_rel_path


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload: str) -> str:
    secret = get_settings().JWT_SECRET_KEY.encode("utf-8")
    digest = hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(digest)


def create_file_access_token(
    *,
    entity_id: str,
    rel_path: str,
    expires_in_seconds: int = 900,
    user_id: str | None = None,
    conversation_id: str | None = None,
    workspace_id: str | None = None,
    source_sha256: str | None = None,
    source_kind: str | None = None,
    document_id: str | None = None,
) -> str:
    """Create an opaque token for a single entity FS file."""
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "path": normalize_rel_path(rel_path),
        "exp": int(time.time()) + max(60, int(expires_in_seconds)),
    }
    if user_id:
        payload["user_id"] = str(user_id)
    if any(value is not None for value in (conversation_id, workspace_id, source_sha256, source_kind, document_id)):
        payload.update(conversation_id=conversation_id, source_sha256=source_sha256)
        if workspace_id is not None:
            payload["workspace_id"] = workspace_id
        if source_kind is not None:
            payload["source_kind"] = source_kind
        if document_id is not None:
            payload["document_id"] = document_id
        valid = _valid_scoped_claims(payload)
        if rel_path != payload["path"] or not valid:
            raise ValueError("Scoped file tokens require an exact source, actor, conversation, and source digest")
        payload["exp"] = int(time.time()) + min(900, max(60, int(expires_in_seconds)))
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_file_access_token(token: str) -> dict[str, Any] | None:
    """Verify a file token and return entity/path, or None when invalid."""
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        return None
    try:
        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if int(payload.get("exp") or 0) < int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    entity_id = str(payload.get("entity_id") or "").strip()
    rel_path = normalize_rel_path(str(payload.get("path") or ""))
    if not entity_id or not rel_path or rel_path == ".." or rel_path.startswith("../"):
        return None
    verified: dict[str, Any] = {"entity_id": entity_id, "path": rel_path}
    user_id = str(payload.get("user_id") or "").strip()
    if user_id:
        verified["user_id"] = user_id
    if any(
        key in payload for key in ("conversation_id", "workspace_id", "source_sha256", "source_kind", "document_id")
    ):
        source_kind = payload.get("source_kind")
        valid = _valid_scoped_claims(payload)
        if payload["path"] != rel_path or not valid:
            return None
        verified.update(conversation_id=payload["conversation_id"], source_sha256=payload["source_sha256"])
        if "workspace_id" in payload:
            verified["workspace_id"] = payload["workspace_id"]
        if source_kind in {"workspace_document", "knowledge_document"}:
            verified.update(source_kind=source_kind, document_id=payload["document_id"])
    return verified


def _valid_scoped_claims(payload: dict[str, Any]) -> bool:
    if payload.get("source_kind") == "workspace_document":
        return _valid_workspace_document_claims(payload)
    if payload.get("source_kind") == "knowledge_document":
        return _valid_knowledge_document_claims(payload)
    return _valid_chat_claims(payload)


def _valid_knowledge_document_claims(payload: dict[str, Any]) -> bool:
    """Ordinary-chat Documents require actor, chat, identity and pinned bytes."""
    from packages.core.services.knowledge_visibility import is_user_visible_path

    # Workspace sources retain their existing, distinct strict contract.
    if "workspace_id" in payload:
        return False
    for key in ("entity_id", "user_id", "conversation_id", "document_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return False
    digest = payload.get("source_sha256")
    path = payload.get("path")
    return (
        isinstance(path, str) and is_user_visible_path(path)
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _valid_chat_claims(payload: dict[str, Any]) -> bool:
    """Context claims are one scope, never a fallback to a legacy bearer token."""
    from packages.core.services.ai_file_permissions import is_chat_upload_path

    for key in ("entity_id", "user_id", "conversation_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return False
    if "workspace_id" in payload:
        workspace_id = payload["workspace_id"]
        if not isinstance(workspace_id, str) or not workspace_id.strip() or workspace_id != workspace_id.strip():
            return False
    digest = payload.get("source_sha256")
    return (
        "source_kind" not in payload
        and "document_id" not in payload
        and isinstance(payload.get("path"), str)
        and is_chat_upload_path(payload["path"])
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _valid_workspace_document_claims(payload: dict[str, Any]) -> bool:
    """Workspace Knowledge claims are distinct from chat attachment claims."""
    for key in ("entity_id", "user_id", "conversation_id", "workspace_id", "document_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            return False
    digest = payload.get("source_sha256")
    return (
        payload.get("source_kind") == "workspace_document"
        and isinstance(payload.get("path"), str)
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )
