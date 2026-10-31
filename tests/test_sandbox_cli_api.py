"""The sandbox CLI API answers only live, internal tokens, as the tool call that minted them.

Every operation must apply the same Knowledge ACL as the agent's own file
tools: a member never sees another member's private document through ``ls``,
``stat``, ``cat``, ``grep`` or ``pull``, a user-less run follows the same
rules as ``read_file``, and file changes are only queued for the bash tool.
"""

from __future__ import annotations

import json
import os

import pytest

import packages.core.database as db_module
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document
from packages.core.services.sandbox_cli import tokens as cli_tokens
from packages.core.services.sandbox_cli.tokens import (
    SandboxCliClaims,
    drain_sandbox_cli_operations,
    mint_sandbox_cli_token,
    resolve_sandbox_cli_token,
    revoke_sandbox_cli_token,
)

SECRET = "BOARD-COMP-SECRET quarterly comp"


@pytest.fixture
def fs_root(tmp_path):
    settings = get_settings()
    old = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    try:
        yield tmp_path
    finally:
        settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT = old


async def _make_user(entity_id: str, name: str, role: str) -> str:
    async with db_module.async_session() as db:
        user = User(
            entity_id=entity_id,
            email=f"{name}-{entity_id.lower()}@test.com",
            display_name=name,
            password_hash=hash_password("pass123"),
            role=role,
            status="active",
        )
        db.add(user)
        await db.flush()
        db.add(UserMembership(
            user_id=user.id, entity_id=entity_id, role=role, status="active", is_primary=True,
        ))
        await db.commit()
        return user.id


@pytest.fixture
async def knowledge(client, fs_root):
    entity_id = generate_ulid()
    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Sandbox CLI Test"))
        await db.commit()
    owner_id = await _make_user(entity_id, "cli_owner", "owner")
    member_id = await _make_user(entity_id, "cli_member", "member")
    root = fs_root / entity_id
    (root / "Knowledge").mkdir(parents=True)
    (root / "Knowledge" / "board-comp.md").write_text(SECRET, encoding="utf-8")
    (root / "Knowledge" / "handbook.md").write_text("team handbook SECRET-free body\n", encoding="utf-8")
    (root / ".ai").mkdir()
    (root / ".ai" / "internal.json").write_text("{}", encoding="utf-8")
    from packages.core.services.knowledge_sync import ensure_folder_path

    await ensure_folder_path(entity_id, "Knowledge")  # entity-visible folder
    async with db_module.async_session() as db:
        await create_document(
            db, entity_id, name="board-comp.md", fs_path="Knowledge/board-comp.md",
            file_type="md", source="upload", visibility="private", owner_id=owner_id,
        )
        await create_document(
            db, entity_id, name="handbook.md", fs_path="Knowledge/handbook.md",
            file_type="md", source="upload", visibility="entity", owner_id=owner_id,
        )
        await db.commit()
    return {"entity_id": entity_id, "owner_id": owner_id, "member_id": member_id, "root": root}


async def _token(entity_id: str, user_id: str | None, **extra) -> str:
    token, _claims = await mint_sandbox_cli_token(
        entity_id=entity_id,
        sandbox_id="sbx-test",
        ttl_seconds=120,
        user_id=user_id,
        agent_id="agent-1",
        conversation_id="conv-1",
        **extra,
    )
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── tokens ──


@pytest.mark.asyncio
async def test_token_round_trip_revocation_and_expiry(client, monkeypatch):
    token, claims = await mint_sandbox_cli_token(
        entity_id="ent-1", sandbox_id="sbx-1", ttl_seconds=60, user_id="user-1",
        agent_id="agent-1", conversation_id="conv-1", workspace_id="ws-1", task_id="task-1",
    )
    assert token.startswith(cli_tokens.TOKEN_PREFIX)
    resolved = await resolve_sandbox_cli_token(token)
    assert resolved == claims
    assert resolved.tool_context_kwargs() == {
        "_user_id_from_context": "user-1",
        "_agent_id_from_context": "agent-1",
        "workspace_id": "ws-1",
        "conversation_id": "conv-1",
        "task_id": "task-1",
    }

    from packages.core.cache import cache

    stored = await cache.get(f"sandbox-cli:token:{cli_tokens.token_digest(token)}")
    assert stored["entity_id"] == "ent-1"
    assert token not in json.dumps(stored), "only the digest of a token is stored"

    real_time = cli_tokens.time.time
    monkeypatch.setattr(cli_tokens.time, "time", lambda: real_time() + 61)
    assert await resolve_sandbox_cli_token(token) is None, "expired tokens are refused"
    monkeypatch.setattr(cli_tokens.time, "time", real_time)
    assert await resolve_sandbox_cli_token(token) == claims

    await revoke_sandbox_cli_token(token)
    assert await resolve_sandbox_cli_token(token) is None
    assert await resolve_sandbox_cli_token("mcli_" + "x" * 43) is None
    assert await resolve_sandbox_cli_token("not-a-token") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["public_customer_chat", "external_channel_chat", "unknown_surface"])
async def test_external_principals_cannot_mint(client, surface):
    with pytest.raises(cli_tokens.SandboxCliTokenError):
        await mint_sandbox_cli_token(
            entity_id="ent-1", sandbox_id="sbx-1", ttl_seconds=60, surface=surface,
        )


@pytest.mark.asyncio
async def test_api_refuses_missing_unknown_revoked_and_external_tokens(client, knowledge):
    missing = await client.post("/api/v1/sandbox-cli/ls", json={})
    assert missing.status_code == 401
    unknown = await client.post("/api/v1/sandbox-cli/ls", json={}, headers=_auth("mcli_" + "y" * 43))
    assert unknown.status_code == 401

    token = await _token(knowledge["entity_id"], knowledge["member_id"])
    assert (await client.post("/api/v1/sandbox-cli/ls", json={}, headers=_auth(token))).status_code == 200
    await revoke_sandbox_cli_token(token)
    revoked = await client.post("/api/v1/sandbox-cli/ls", json={}, headers=_auth(token))
    assert revoked.status_code == 401
    assert token not in revoked.text

    # A record that claims an external surface (never mintable) is refused.
    from packages.core.cache import cache

    forged = "mcli_" + "z" * 43
    record = SandboxCliClaims(
        entity_id=knowledge["entity_id"], sandbox_id="sbx", exec_id="e" * 32,
        expires_at=cli_tokens.time.time() + 60, user_id=knowledge["owner_id"],
        surface="public_customer_chat",
    ).to_record()
    await cache.set(f"sandbox-cli:token:{cli_tokens.token_digest(forged)}", record, ttl=60)
    external = await client.post("/api/v1/sandbox-cli/ls", json={}, headers=_auth(forged))
    assert external.status_code == 403


# ── reads apply the Knowledge ACL ──


@pytest.mark.asyncio
async def test_member_never_sees_another_members_private_document(client, knowledge):
    token = await _token(knowledge["entity_id"], knowledge["member_id"])
    headers = _auth(token)

    listing = (await client.post("/api/v1/sandbox-cli/ls", json={"path": "Knowledge"}, headers=headers)).json()
    assert "entries" in listing, listing
    names = {entry["path"] for entry in listing["entries"]}
    assert names == {"Knowledge/handbook.md"}

    root_listing = (await client.post("/api/v1/sandbox-cli/ls", json={"recursive": True}, headers=headers)).json()
    assert {entry["path"] for entry in root_listing["entries"]} == {"Knowledge/handbook.md"}

    stat = await client.post("/api/v1/sandbox-cli/stat", json={"path": "Knowledge/board-comp.md"}, headers=headers)
    assert stat.status_code == 404

    read = await client.post("/api/v1/sandbox-cli/read", json={"path": "Knowledge/board-comp.md"}, headers=headers)
    assert read.status_code == 404
    assert SECRET not in read.text

    grep = (await client.post(
        "/api/v1/sandbox-cli/grep", json={"pattern": "BOARD-COMP", "path": "."}, headers=headers,
    )).json()
    assert grep["matches"] == []

    pull = await client.post("/api/v1/sandbox-cli/pull", json={"path": "Knowledge/board-comp.md"}, headers=headers)
    assert pull.status_code == 404
    assert SECRET.encode() not in pull.content

    handbook = await client.post("/api/v1/sandbox-cli/pull", json={"path": "/workspace/Knowledge/handbook.md"}, headers=headers)
    assert handbook.status_code == 200
    assert handbook.content.startswith(b"team handbook")
    assert handbook.headers["x-manor-path"] == "Knowledge/handbook.md"

    hidden = await client.post("/api/v1/sandbox-cli/read", json={"path": ".ai/internal.json"}, headers=headers)
    assert hidden.status_code in {400, 404}


@pytest.mark.asyncio
async def test_owner_reads_their_private_document(client, knowledge):
    headers = _auth(await _token(knowledge["entity_id"], knowledge["owner_id"]))
    read = (await client.post("/api/v1/sandbox-cli/read", json={"path": "Knowledge/board-comp.md"}, headers=headers)).json()
    assert SECRET in read["content"]
    grep = (await client.post("/api/v1/sandbox-cli/grep", json={"pattern": "BOARD-COMP"}, headers=headers)).json()
    assert [match["file"] for match in grep["matches"]] == ["Knowledge/board-comp.md"]


@pytest.mark.asyncio
async def test_identity_comes_from_the_token_never_the_body(client, knowledge):
    headers = _auth(await _token(knowledge["entity_id"], knowledge["member_id"]))
    read = await client.post(
        "/api/v1/sandbox-cli/read",
        json={
            "path": "Knowledge/board-comp.md",
            "user_id": knowledge["owner_id"],
            "_user_id_from_context": knowledge["owner_id"],
            "entity_id": knowledge["entity_id"],
        },
        headers=headers,
    )
    assert read.status_code == 404
    escape = await client.post("/api/v1/sandbox-cli/read", json={"path": "../other/x.md"}, headers=headers)
    assert escape.status_code == 400


@pytest.mark.asyncio
async def test_user_less_runs_follow_the_read_file_rules(client, knowledge):
    from packages.core.ai.tools.file_tools import _read_file

    token = await _token(knowledge["entity_id"], None)
    claims = await resolve_sandbox_cli_token(token)
    direct = json.loads(await _read_file(
        knowledge["entity_id"], path="Knowledge/handbook.md", **claims.tool_context_kwargs(),
    ))
    via_cli = await client.post(
        "/api/v1/sandbox-cli/read", json={"path": "Knowledge/handbook.md"}, headers=_auth(token),
    )
    assert bool(direct.get("error")) == (via_cli.status_code != 200), (direct, via_cli.text)


# ── file changes are queued, never applied by the API ──


@pytest.mark.asyncio
async def test_file_operations_are_validated_and_queued_only(client, knowledge):
    token = await _token(knowledge["entity_id"], knowledge["member_id"])
    claims = await resolve_sandbox_cli_token(token)
    headers = _auth(token)

    queued = await client.post(
        "/api/v1/sandbox-cli/ops",
        json={"op": "mv", "paths": ["Knowledge/handbook.md", "Knowledge/Archive/handbook.md"]},
        headers=headers,
    )
    assert queued.status_code == 200, queued.text
    assert (knowledge["root"] / "Knowledge" / "handbook.md").exists(), "the API never mutates files"

    for body, status in (
        ({"op": "chmod", "paths": ["Knowledge/handbook.md"]}, 400),
        ({"op": "rm", "paths": [".ai/internal.json"]}, 403),
        ({"op": "rm", "paths": ["Knowledge/missing.md"]}, 404),
        ({"op": "mv", "paths": ["Knowledge/handbook.md"]}, 400),
        ({"op": "rm", "paths": ["/etc/passwd"]}, 400),
    ):
        response = await client.post("/api/v1/sandbox-cli/ops", json=body, headers=headers)
        assert response.status_code == status, (body, response.text)

    drained = await drain_sandbox_cli_operations(claims.exec_id)
    assert drained == [{
        "op": "mv",
        "paths": ["Knowledge/handbook.md", "Knowledge/Archive/handbook.md"],
        "recursive": False,
    }]
    assert await drain_sandbox_cli_operations(claims.exec_id) == []


@pytest.mark.asyncio
async def test_whoami_reveals_scope_but_no_secret(client, knowledge):
    token = await _token(knowledge["entity_id"], knowledge["member_id"])
    payload = (await client.post("/api/v1/sandbox-cli/whoami", json={}, headers=_auth(token))).json()
    assert payload["entity_id"] == knowledge["entity_id"]
    assert payload["agent_id"] == "agent-1"
    assert payload["conversation_id"] == "conv-1"
    assert payload["user"] is True
    assert token not in json.dumps(payload)
    assert knowledge["member_id"] not in json.dumps(payload)


def test_entity_root_exists_for_fixture(fs_root):
    assert os.path.isdir(fs_root)


# ── rate limiter exemption ──────────────────────────────────────────────────


async def test_sandbox_cli_is_exempt_from_per_ip_rate_limit(monkeypatch):
    """Every sandbox on a runner shares its egress IP; the CLI authenticates
    (and is bounded) per command token instead, like the external agent
    gateway (#1088)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from apps.api import middleware_core as mw

    app = FastAPI()

    @app.post("/api/v1/sandbox-cli/ls")
    async def cli_route():
        return {"ok": True}

    @app.post("/api/v1/other")
    async def other():
        return {"ok": True}

    app.add_middleware(mw.RateLimitMiddleware)
    monkeypatch.setattr(mw, "_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(mw, "RATE_LIMIT_PER_MINUTE", 2)
    mw._buckets.clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            cli_codes = [(await c.post("/api/v1/sandbox-cli/ls")).status_code for _ in range(6)]
            other_codes = [(await c.post("/api/v1/other")).status_code for _ in range(4)]
    finally:
        mw._buckets.clear()
    assert cli_codes == [200] * 6
    assert 429 in other_codes
    # Only the CLI's own prefix: a lookalike path is still limited.
    assert not mw._is_rate_limit_exempt("/api/v1/sandbox-cli-other/ls")
