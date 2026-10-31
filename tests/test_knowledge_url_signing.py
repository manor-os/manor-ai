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
import hashlib
import json

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


def test_actor_bound_signing_preserves_user_claim():
    [url] = paths_to_signed_urls(
        ["/Photos/foo.jpg"],
        entity_id="ent-A",
        user_id="user-123",
    )
    payload = verify_file_access_token(_token_in(url))
    assert payload is not None
    assert payload["user_id"] == "user-123"


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
    assert out[2].startswith("http://manor-api:8000/api/v1/fs/public/")


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


async def _ordinary_chat_document(db, root, suffix="jpg"):
    from packages.core.models.document import Document
    from packages.core.models.user import UserMembership
    from test_chat_attachment_read_access import seed_attachment

    user, conversation, _message, _workspace, upload, _path = await seed_attachment(db, root)
    user.role = "member"
    db.add(UserMembership(entity_id=user.entity_id, user_id=user.id, role="member", status="active"))
    path = f"prepared-media.{suffix}"
    source = root / user.entity_id / path
    source.write_bytes(b"authorized ordinary chat media")
    document = Document(
        entity_id=user.entity_id, name=path, fs_path=path, file_type=suffix,
        owner_id=user.id, visibility="private", source="ai_generated",
    )
    db.add(document)
    await db.commit()
    context = dict(entity_id=user.entity_id, user_id=user.id, conversation_id=conversation.id)
    return user, conversation, document, source, upload, context


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["jpg", "wav"])
async def test_ordinary_chat_document_capability_serves_only_pinned_media(
    db_session, client, tmp_path, monkeypatch, suffix,
):
    """The shared transport works for a poster and an unrelated audio input."""
    from packages.core.config import get_settings
    from packages.core.services.workspace_document_media import inspect_knowledge_document_media

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    _user, _conversation, document, source, _upload, context = await _ordinary_chat_document(
        db_session, tmp_path, suffix,
    )
    media = await inspect_knowledge_document_media(
        {"source_kind": "knowledge_document", "document_id": document.id},
        **context, workspace_id=None, tool_name="media_transport", allowed_suffixes={f".{suffix}"},
        max_bytes=1024,
    )
    assert media["document_id"] == document.id
    assert media["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    token = create_file_access_token(
        **context, rel_path=media["rel_path"], source_sha256=media["source_sha256"],
        source_kind="knowledge_document", document_id=document.id,
    )
    claims = verify_file_access_token(token)
    assert claims["source_kind"] == "knowledge_document"
    assert "workspace_id" not in claims
    url = f"/api/v1/fs/public/{token}/{source.name}"
    head = await client.head(url)
    assert head.status_code == 200
    assert head.headers["content-length"] == str(source.stat().st_size)
    assert head.headers["cache-control"] == "private, no-store"
    response = await client.get(url)
    assert response.status_code == 200
    assert response.content == source.read_bytes()
    partial = await client.get(url, headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.content == source.read_bytes()[:10]


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [
    "inactive_user", "inactive_conversation", "other_user", "other_entity",
    "workspace_conversation", "missing_conversation", "trashed_document",
    "restricted_document", "quarantined_document", "revoked_document_access", "changed_path", "raw_upload",
    "changed_bytes", "symlink",
])
async def test_ordinary_chat_document_capability_rechecks_scope_acl_and_bytes(
    db_session, client, tmp_path, monkeypatch, restriction,
):
    from packages.core.config import get_settings
    from packages.core.models.base import generate_ulid
    from packages.core.services.workspace_document_media import (
        WorkspaceDocumentMediaError, inspect_knowledge_document_media,
    )

    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    user, conversation, document, source, upload, context = await _ordinary_chat_document(db_session, tmp_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    token = create_file_access_token(
        **context, rel_path=document.fs_path, source_sha256=digest,
        source_kind="knowledge_document", document_id=document.id,
    )
    if restriction == "inactive_user":
        user.status = "inactive"
    elif restriction == "inactive_conversation":
        conversation.status = "archived"
    elif restriction == "other_user":
        conversation.user_id = generate_ulid()
    elif restriction == "other_entity":
        document.entity_id = generate_ulid()
    elif restriction == "workspace_conversation":
        conversation.scope = "workspace_main"
    elif restriction == "missing_conversation":
        await db_session.delete(conversation)
    elif restriction == "trashed_document":
        document.is_trashed = True
    elif restriction == "restricted_document":
        document.classification = "restricted"
    elif restriction == "quarantined_document":
        document.quarantine_status = "quarantined"
    elif restriction == "revoked_document_access":
        document.owner_id = generate_ulid()
    elif restriction == "changed_path":
        document.fs_path = "moved-media.jpg"
    elif restriction == "raw_upload":
        document.fs_path = str(upload.relative_to(tmp_path / user.entity_id))
    elif restriction == "changed_bytes":
        source.write_bytes(b"unapproved replacement media")
    elif restriction == "symlink":
        renamed = source.with_name("other.jpg")
        source.rename(renamed)
        source.symlink_to(renamed)
    await db_session.commit()
    with pytest.raises(WorkspaceDocumentMediaError):
        await inspect_knowledge_document_media(
            {"source_kind": "knowledge_document", "document_id": document.id},
            **context, workspace_id=None, tool_name="media_transport", allowed_suffixes={".jpg"},
            max_bytes=1024, expected_source_sha256=digest,
        )
    for method in (client.head, client.get):
        response = await method(f"/api/v1/fs/public/{token}/prepared-media.jpg")
        assert response.status_code in {403, 404}
        assert b"ordinary chat media" not in response.content


def test_knowledge_document_tokens_are_complete_bounded_and_cannot_change_scope(monkeypatch):
    from packages.core.services import file_access_tokens

    monkeypatch.setattr(file_access_tokens.time, "time", lambda: 1000)
    claims = dict(
        entity_id="entity", user_id="actor", conversation_id="chat", document_id="document",
        rel_path="poster.jpg", source_sha256="a" * 64, source_kind="knowledge_document",
    )
    token = create_file_access_token(**claims, expires_in_seconds=99999)
    monkeypatch.setattr(file_access_tokens.time, "time", lambda: 1901)
    assert verify_file_access_token(token) is None
    for key in ("user_id", "conversation_id", "document_id", "source_sha256"):
        with pytest.raises(ValueError):
            create_file_access_token(**{**claims, key: None})
    for changes in (
        {"workspace_id": "workspace"}, {"source_kind": "unrecognized_document"},
        {"rel_path": "uploads/chat/1234567890abcdef.jpg"},
        {"source_kind": "workspace_document"},
    ):
        with pytest.raises(ValueError):
            create_file_access_token(**{**claims, **changes})
    payload = {**claims, "path": claims["rel_path"], "exp": 3000}
    payload.pop("rel_path")
    for key in ("user_id", "conversation_id", "document_id", "source_sha256", "source_kind"):
        malformed = {name: value for name, value in payload.items() if name != key}
        encoded = file_access_tokens._b64_encode(json.dumps(malformed).encode())
        assert verify_file_access_token(f"{encoded}.{file_access_tokens._sign(encoded)}") is None
