"""Short-lived signed URLs for provider-readable entity files."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
) -> str:
    """Create an opaque token for a single entity FS file."""
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "path": normalize_rel_path(rel_path),
        "exp": int(time.time()) + max(60, int(expires_in_seconds)),
    }
    payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_file_access_token(token: str) -> dict[str, str] | None:
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
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    entity_id = str(payload.get("entity_id") or "").strip()
    rel_path = normalize_rel_path(str(payload.get("path") or ""))
    if not entity_id or not rel_path or rel_path == ".." or rel_path.startswith("../"):
        return None
    return {"entity_id": entity_id, "path": rel_path}
