"""Canonical, provider-neutral publication receipts for Workflow runs.

Publishing nodes return provider-specific payloads.  A Workflow must not call
that a business success until it has an auditable receipt with a stable target,
the exact payload fingerprint, and explicit postcondition verification.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse


PUBLICATION_RECEIPT_SCHEMA = "publication-receipt/v1"
PUBLICATION_VERIFICATION_STATUSES = frozenset({
    "verified",
    "unverified",
    "failed",
})
_PAYLOAD_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class PublicationReceiptError(ValueError):
    """Raised when a publication receipt cannot prove its claimed outcome."""


def publication_payload_hash(payload: Any) -> str:
    """Return a deterministic SHA-256 fingerprint without retaining payload data."""
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PublicationReceiptError("Publication payload cannot be fingerprinted") from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _required_text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PublicationReceiptError(f"Publication receipt {key} is required")
    return value.strip()


def _optional_text(record: Mapping[str, Any], key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PublicationReceiptError(f"Publication receipt {key} must be text")
    return value.strip() or None


def _http_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PublicationReceiptError(
            "Publication receipt published_url must be an http(s) URL"
        )
    return value


def _iso_datetime(value: Any, *, fallback: datetime | None = None) -> str:
    if value in (None, ""):
        if fallback is None:
            raise PublicationReceiptError("Publication receipt published_at is required")
        parsed = fallback
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise PublicationReceiptError(
                "Publication receipt published_at must be an ISO 8601 datetime"
            ) from exc
    else:
        raise PublicationReceiptError(
            "Publication receipt published_at must be an ISO 8601 datetime"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_list(value: Any, *, key: str, limit: int = 50) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PublicationReceiptError(f"Publication receipt {key} must be a list")
    if len(value) > limit:
        raise PublicationReceiptError(
            f"Publication receipt {key} cannot contain more than {limit} items"
        )
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise PublicationReceiptError(
                f"Publication receipt {key} entries must be objects"
            )
        records.append(deepcopy(dict(item)))
    return records


def normalize_publication_receipt(
    value: Any,
    *,
    payload: Any = None,
    require_verified: bool = True,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate and normalize one platform publication result.

    ``verification_status`` is deliberately explicit.  A provider returning a
    green HTTP response is not silently promoted to ``verified``; the publisher
    atom must observe the platform postcondition and say so.
    """
    if not isinstance(value, Mapping):
        raise PublicationReceiptError("Publication receipt must be an object")
    record = dict(value)
    platform = _required_text(record, "platform")
    verification_status = _required_text(record, "verification_status").lower()
    if verification_status not in PUBLICATION_VERIFICATION_STATUSES:
        raise PublicationReceiptError(
            "Publication receipt verification_status must be verified, unverified, or failed"
        )
    if require_verified and verification_status != "verified":
        raise PublicationReceiptError(
            f"Publication was not verified (status: {verification_status})"
        )

    external_id = _optional_text(record, "external_id")
    published_url = _http_url(_optional_text(record, "published_url"))
    if verification_status == "verified" and not external_id and not published_url:
        raise PublicationReceiptError(
            "Verified publication requires external_id or published_url"
        )

    payload_hash = _optional_text(record, "payload_hash")
    if payload_hash is None and payload is not None:
        payload_hash = publication_payload_hash(payload)
    if payload_hash is None:
        raise PublicationReceiptError(
            "Publication receipt requires payload_hash or the original payload"
        )
    payload_hash = payload_hash.lower()
    if not _PAYLOAD_HASH_RE.fullmatch(payload_hash):
        raise PublicationReceiptError(
            "Publication receipt payload_hash must use sha256:<64 lowercase hex characters>"
        )

    now = observed_at or datetime.now(timezone.utc)
    evidence = _record_list(record.get("evidence"), key="evidence")
    if published_url and not evidence:
        evidence.append({"kind": "published_url", "url": published_url})
    elif external_id and not evidence:
        evidence.append({"kind": "external_id", "value": external_id})
    if verification_status == "verified" and not evidence:
        raise PublicationReceiptError("Verified publication requires evidence")

    fallback_used = _optional_text(record, "fallback_used") or "none"
    result = {
        "schema_version": PUBLICATION_RECEIPT_SCHEMA,
        "platform": platform,
        "verification_status": verification_status,
        "external_id": external_id,
        "published_url": published_url,
        "published_at": _iso_datetime(record.get("published_at"), fallback=now),
        "verified_at": _iso_datetime(record.get("verified_at"), fallback=now),
        "payload_hash": payload_hash,
        "fallback_used": fallback_used,
        "evidence": evidence,
        "attempts": _record_list(record.get("attempts"), key="attempts"),
    }
    for key in ("account", "content_type", "title"):
        optional = _optional_text(record, key)
        if optional:
            result[key] = optional
    return result


def publication_receipts_from_step_results(step_results: Any) -> list[dict[str, Any]]:
    """Return normalized receipt outputs already persisted by receipt nodes."""
    if not isinstance(step_results, Mapping):
        return []
    receipts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for result in step_results.values():
        if not isinstance(result, Mapping):
            continue
        output = result.get("output")
        candidates = output if isinstance(output, list) else [output]
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            if candidate.get("schema_version") != PUBLICATION_RECEIPT_SCHEMA:
                continue
            receipt = deepcopy(dict(candidate))
            identity = (
                str(receipt.get("platform") or ""),
                str(receipt.get("external_id") or ""),
                str(receipt.get("payload_hash") or ""),
            )
            if identity in seen:
                continue
            seen.add(identity)
            receipts.append(receipt)
    return receipts
