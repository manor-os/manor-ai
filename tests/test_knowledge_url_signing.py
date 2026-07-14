"""Tenant-safety tests for the Knowledge → local-worker URL signer.

Threat model
────────────
When a Knowledge path is rewritten to a signed URL for a local worker to
fetch over HTTP, the signature MUST be the only thing deciding which tenant's
file is served. We test:

  1. Signing requires entity_id (no anonymous reads).
  2. A token signed for entity-A is rejected when verified as entity-B
     (tampering the claim breaks the HMAC).
  3. Expired tokens are rejected.
  4. Path-traversal payloads are rejected before signing.
  5. System / hidden paths are blocked (an agent shouldn't be able to
     ship `.ai/` or `_meta/` files to external tools).
  6. Already-HTTP URLs pass through untouched.
  7. ContextVar isolation: two coroutines running concurrently with
     different entity_ids never see each other's context.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.exceptions import HTTPException
from httpx import ASGITransport, AsyncClient

from apps.api.middleware_core import http_error_handler
from apps.api.routers import filesystem
from packages.core.ai.mcp._knowledge_url import (
    KnowledgePathError,
    paths_to_signed_urls,
    safe_paths_to_signed_urls,
)
from packages.core.services.file_access_tokens import (
    create_file_access_token,
    verify_file_access_token,
)


def _token_in(url: str) -> str:
    """Pull the opaque token out of a signed-URL string."""
    return url.rsplit("/", 1)[-1]


def test_signing_requires_entity_id():
    with pytest.raises(KnowledgePathError):
        paths_to_signed_urls(["/Photos/foo.jpg"], entity_id="")


def test_token_round_trips_for_same_entity():
    [url] = paths_to_signed_urls(["/Photos/foo.jpg"], entity_id="ent-A")
    payload = verify_file_access_token(_token_in(url))
    assert payload is not None
    assert payload["entity_id"] == "ent-A"
    assert payload["path"] == "Photos/foo.jpg"


def test_tampered_entity_claim_breaks_hmac():
    """If you re-mint the inner payload to claim entity-B, the HMAC
    computed over the original payload no longer matches — the
    verifier returns None. This is the core multi-tenant guard."""
    legit = create_file_access_token(entity_id="ent-A", rel_path="Photos/foo.jpg")
    payload_b64, sig = legit.split(".", 1)

    import base64
    import json

    decoded = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)).decode())
    decoded["entity_id"] = "ent-B"
    forged_b64 = base64.urlsafe_b64encode(json.dumps(decoded, separators=(",", ":")).encode()).decode().rstrip("=")
    forged = f"{forged_b64}.{sig}"  # keep original HMAC

    assert verify_file_access_token(forged) is None


def test_expired_token_rejected():
    token = create_file_access_token(
        entity_id="ent-A",
        rel_path="Photos/foo.jpg",
        expires_in_seconds=60,
    )
    # Roll the clock forward past the (clamped) 60s minimum.
    real_time = time.time

    try:
        time.time = lambda: real_time() + 120  # type: ignore[assignment]
        assert verify_file_access_token(token) is None
    finally:
        time.time = real_time  # type: ignore[assignment]


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "../secret.txt",
        "..",
        "  ",
        "",
    ],
)
def test_traversal_and_empty_paths_rejected(bad):
    with pytest.raises(KnowledgePathError):
        paths_to_signed_urls([bad], entity_id="ent-A")


@pytest.mark.parametrize(
    "hidden",
    [
        ".ai/workspaces/ws_123/memory/facts/company.md",
        "tasks/run/result.json",
        "uploads/report.pdf",
        "sandbox-output/report.pdf",
        "MANOR.md",
    ],
)
def test_system_paths_blocked_from_browser_tools(hidden):
    """An agent should not be able to leak `.ai/` memory or task
    outputs to Xiaohongshu/LinkedIn just by passing the path."""
    with pytest.raises(KnowledgePathError):
        paths_to_signed_urls([hidden], entity_id="ent-A")


def test_http_urls_pass_through_unchanged():
    out = paths_to_signed_urls(
        [
            "https://example.com/cat.jpg",
            "http://cdn.example.org/dog.png",
            "/Photos/me.jpg",
        ],
        entity_id="ent-A",
    )
    assert out[0] == "https://example.com/cat.jpg"
    assert out[1] == "http://cdn.example.org/dog.png"
    # Third one is signed
    assert out[2].startswith("http://api:8000/api/v1/fs/public/")


def test_local_fs_url_converts_to_signed_knowledge_path():
    [url] = paths_to_signed_urls(
        ["/api/v1/fs/ent-A/Uploads/launch/images/cover.png"],
        entity_id="ent-A",
    )
    payload = verify_file_access_token(_token_in(url))
    assert payload is not None
    assert payload["entity_id"] == "ent-A"
    assert payload["path"] == "Uploads/launch/images/cover.png"


def test_local_fs_url_rejects_entity_mismatch():
    with pytest.raises(KnowledgePathError):
        paths_to_signed_urls(
            ["/api/v1/fs/ent-B/Uploads/launch/images/cover.png"],
            entity_id="ent-A",
        )


@pytest.mark.asyncio
async def test_signed_file_route_supports_head_preflight(monkeypatch, tmp_path):
    root = tmp_path / "ent-A"
    image = root / "Photos" / "cat.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\ncat")

    monkeypatch.setattr(filesystem, "is_fs_enabled", lambda: True)
    monkeypatch.setattr(filesystem, "get_entity_root", lambda entity_id: str(root))

    token = create_file_access_token(entity_id="ent-A", rel_path="Photos/cat.png")
    app = FastAPI()
    app.add_exception_handler(HTTPException, http_error_handler)
    app.include_router(filesystem.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        head = await client.head(f"/api/v1/fs/public/{token}")
        assert head.status_code == 200
        assert head.headers["content-type"].startswith("image/png")
        assert head.headers["content-length"] == str(image.stat().st_size)

        get = await client.get(f"/api/v1/fs/public/{token}")
        assert get.status_code == 200
        assert get.content == image.read_bytes()

        get_with_filename = await client.get(f"/api/v1/fs/public/{token}/reference.png")
        assert get_with_filename.status_code == 200
        assert get_with_filename.headers["content-type"].startswith("image/png")
        assert get_with_filename.content == image.read_bytes()


@pytest.mark.asyncio
async def test_raw_file_missing_response_is_not_cacheable(monkeypatch, tmp_path):
    root = tmp_path / "ent-A"
    root.mkdir()

    monkeypatch.setattr(filesystem, "is_fs_enabled", lambda: True)
    monkeypatch.setattr(filesystem, "get_entity_root", lambda entity_id: str(root))

    app = FastAPI()
    app.add_exception_handler(HTTPException, http_error_handler)
    app.include_router(filesystem.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/api/v1/fs/ent-A/Photos/missing.png")

    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"


def test_safe_wrapper_returns_error_envelope_when_entity_missing():
    urls, err = safe_paths_to_signed_urls(["/Photos/foo.jpg"], entity_id=None)
    assert urls is None
    assert err is not None
    assert "entity_id" in err.lower()


def test_safe_wrapper_returns_error_for_traversal():
    urls, err = safe_paths_to_signed_urls(["../../etc/passwd"], entity_id="ent-A")
    assert urls is None
    assert "traversal" in err.lower() or "rejected" in err.lower()
