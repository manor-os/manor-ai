"""Unit tests for the OpenAI-compatible LLM gateway (``/api/v1/llm``).

The router is mounted on a bare FastAPI app with its DB and auth
dependencies overridden, so these run without Postgres or a provider key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.routers import llm_gateway
from packages.core.ai.llm_client import CreditCheckUnavailableError, CreditExhaustedError
from packages.core.models.user import Entity, User, UserMembership

pytestmark = pytest.mark.unit

ENTITY_ID = "01JZENTITY0000000000000000"
OTHER_ENTITY_ID = "01JZOTHERENTITY00000000000"
USER_ID = "01JZUSER000000000000000000"
CLIENT_ID = "minutes-cloud"
CLIENT_SECRET = "minutes-secret"


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Just enough AsyncSession for the entity lookup in the client path."""

    def __init__(self, entities: dict[str, object]):
        self._entities = entities

    async def execute(self, stmt):
        # The router only issues ``select(Entity).where(Entity.id == ...)``;
        # pull the bound id out of the compiled WHERE clause.
        params = stmt.compile().params
        wanted = next(iter(params.values()), None)
        return _FakeResult(self._entities.get(wanted))


def _entity(entity_id: str, deleted: bool = False):
    return SimpleNamespace(id=entity_id, deleted_at="2026-01-01" if deleted else None)


def _user(user_id: str, entity_id: str, status: str = "active"):
    return SimpleNamespace(id=user_id, entity_id=entity_id, status=status)


def _oauth_client(restricted_entity_id: str | None = None):
    return SimpleNamespace(client_id=CLIENT_ID, restricted_entity_id=restricted_entity_id, active=True)


def _gateway_route(*, model: str = "openai/gpt-4o-mini", byok: bool = True):
    return SimpleNamespace(
        model=model,
        metadata={"llm_api_key": "sk-tenant-key"} if byok else None,
        byok=byok,
    )


@pytest.fixture
def billing_calls(monkeypatch):
    """Capture billing context + credit-gate use around the fake provider call."""
    calls: dict[str, object] = {"contexts": [], "preflight": [], "completions": []}

    class _Ctx:
        def __init__(self, entity_id, **kwargs):
            calls["contexts"].append({"entity_id": entity_id, **kwargs})

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    async def fake_assert_credit(entity_id, **kwargs):
        calls["preflight"].append({"entity_id": entity_id, **kwargs})
        gate = calls.get("gate")
        if gate is not None:
            raise gate

    monkeypatch.setattr(llm_gateway, "llm_billing_context", _Ctx)
    monkeypatch.setattr(llm_gateway, "assert_credit_available", fake_assert_credit)
    return calls


def _install_fake_completion(monkeypatch, billing_calls, *, content="Hello there", stream_pieces=None,
                             usage=None, raise_exc=None):
    usage = usage or {"prompt": 12, "completion": 5, "total": 17, "model": "openai/gpt-4o-mini",
                      "finish_reason": "stop"}

    async def fake_chat_completion(messages, *, stream_handler=None, **kwargs):
        billing_calls["completions"].append({"messages": messages, **kwargs})
        if raise_exc is not None:
            raise raise_exc
        if stream_handler is not None:
            for piece in (stream_pieces if stream_pieces is not None else [content]):
                await stream_handler("text_delta", {"content": piece})
        return content, dict(usage)

    monkeypatch.setattr(llm_gateway, "chat_completion", fake_chat_completion)


@pytest.fixture
def gateway_app(monkeypatch):
    """Bare app with the gateway router, DB + user auth overridden."""
    monkeypatch.setenv("DEPLOYMENT_MODE", "cloud")
    app = FastAPI()
    app.include_router(llm_gateway.router)

    entities = {ENTITY_ID: _entity(ENTITY_ID), OTHER_ENTITY_ID: _entity(OTHER_ENTITY_ID)}
    users = {USER_ID: _user(USER_ID, ENTITY_ID)}
    state = SimpleNamespace(entities=entities, users=users, oauth_client=_oauth_client())

    async def override_db():
        yield _FakeSession(state.entities)

    async def override_user(request, credentials, db):
        token = credentials.credentials if credentials else ""
        if token != "user-token":
            from fastapi import HTTPException
            raise HTTPException(401, "Invalid or expired token")
        return users[USER_ID]

    async def fake_verify_client(db, client_id, client_secret):
        if client_id == CLIENT_ID and client_secret == CLIENT_SECRET:
            return state.oauth_client
        return None

    async def fake_get_user(db, user_id):
        return state.users.get(user_id)

    app.dependency_overrides[llm_gateway.get_db] = override_db
    monkeypatch.setattr(llm_gateway, "get_current_user", override_user)
    monkeypatch.setattr(llm_gateway, "verify_client_credentials", fake_verify_client)
    monkeypatch.setattr(llm_gateway, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(
        llm_gateway,
        "_client_user_has_active_entity_membership",
        AsyncMock(side_effect=lambda _db, user, entity_id: str(user.entity_id) == entity_id),
    )
    monkeypatch.setattr(
        llm_gateway,
        "_resolve_gateway_model_route",
        AsyncMock(return_value=_gateway_route()),
        raising=False,
    )
    app.state.gateway_test = state
    return app


@pytest.fixture
async def gw(gateway_app):
    async with AsyncClient(transport=ASGITransport(app=gateway_app), base_url="http://test") as c:
        yield c


USER_HEADERS = {"Authorization": "Bearer user-token"}
CLIENT_HEADERS = {
    "X-Manor-Client-Id": CLIENT_ID,
    "X-Manor-Client-Secret": CLIENT_SECRET,
    "X-Manor-Entity-Id": ENTITY_ID,
    "X-Manor-User-Id": USER_ID,
    "X-Manor-Business-Type": "meeting_note",
}
BODY = {"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}


def _sse_events(text: str) -> list:
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        data = block[len("data: "):]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


# ── user bearer path ──


async def test_user_token_non_stream_returns_openai_shape_and_bills_user_entity(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)

    resp = await gw.post("/api/v1/llm/chat/completions", headers=USER_HEADERS, json=BODY)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "openai/gpt-4o-mini"
    assert data["choices"][0]["message"] == {"role": "assistant", "content": "Hello there"}
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}

    assert billing_calls["preflight"][0]["entity_id"] == ENTITY_ID
    ctx = billing_calls["contexts"][0]
    assert ctx == {"entity_id": ENTITY_ID, "user_id": USER_ID, "source": "llm_gateway"}
    sent = billing_calls["completions"][0]
    assert sent["model"] == "openai/gpt-4o-mini" and sent["max_tokens"] == 50


async def test_user_token_business_type_labels_usage(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    headers = {**USER_HEADERS, "X-Manor-Business-Type": "meeting_chat"}

    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)

    assert resp.status_code == 200
    assert billing_calls["contexts"][0]["source"] == "meeting_chat"


async def test_missing_auth_is_401(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    resp = await gw.post("/api/v1/llm/chat/completions", json=BODY)
    assert resp.status_code == 401
    assert billing_calls["completions"] == []


# ── oauth client credential path ──


async def test_client_credentials_bill_named_entity_and_user(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)

    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)

    assert resp.status_code == 200, resp.text
    assert billing_calls["contexts"][0] == {
        "entity_id": ENTITY_ID, "user_id": USER_ID, "source": "meeting_note",
    }


async def test_client_without_business_type_is_labelled_by_client_id(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    headers = {k: v for k, v in CLIENT_HEADERS.items() if k != "X-Manor-Business-Type"}

    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)

    assert resp.status_code == 200
    assert billing_calls["contexts"][0]["source"] == CLIENT_ID


async def test_bad_client_secret_is_401(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    headers = {**CLIENT_HEADERS, "X-Manor-Client-Secret": "wrong"}
    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)
    assert resp.status_code == 401
    assert billing_calls["completions"] == []


async def test_client_missing_entity_is_400(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    headers = {k: v for k, v in CLIENT_HEADERS.items() if k != "X-Manor-Entity-Id"}
    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)
    assert resp.status_code == 400


async def test_client_unknown_entity_is_404(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    headers = {**CLIENT_HEADERS, "X-Manor-Entity-Id": "01JZNOPE000000000000000000", "X-Manor-User-Id": ""}
    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)
    assert resp.status_code == 404


async def test_client_user_from_other_entity_is_403(gw, gateway_app, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    gateway_app.state.gateway_test.users["other-user"] = _user("other-user", OTHER_ENTITY_ID)
    headers = {**CLIENT_HEADERS, "X-Manor-User-Id": "other-user"}

    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)

    assert resp.status_code == 403
    assert billing_calls["completions"] == []


async def test_entity_restricted_client_cannot_bill_other_entity(gw, gateway_app, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    gateway_app.state.gateway_test.oauth_client = _oauth_client(restricted_entity_id=OTHER_ENTITY_ID)

    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)

    assert resp.status_code == 403
    assert billing_calls["completions"] == []


async def test_oss_client_requires_explicit_target_restriction(gw, gateway_app, monkeypatch, billing_calls):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    _install_fake_completion(monkeypatch, billing_calls)

    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "entity_restriction_required_for_client"
    assert billing_calls["completions"] == []


async def test_invalid_business_type_is_400(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    headers = {**CLIENT_HEADERS, "X-Manor-Business-Type": "bad type!"}
    resp = await gw.post("/api/v1/llm/chat/completions", headers=headers, json=BODY)
    assert resp.status_code == 400


# ── credit gate ──


async def test_exhausted_credit_is_402_before_provider_call(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    billing_calls["gate"] = CreditExhaustedError("Credit balance exhausted")

    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)

    assert resp.status_code == 402
    assert "exhausted" in resp.json()["detail"]
    assert billing_calls["completions"] == []


async def test_credit_check_outage_is_503(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    billing_calls["gate"] = CreditCheckUnavailableError("credit_check_unavailable")
    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)
    assert resp.status_code == 503


async def test_credit_preflight_endpoint(gw, monkeypatch, billing_calls):
    ok = await gw.get("/api/v1/llm/credit", headers=CLIENT_HEADERS)
    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "entity_id": ENTITY_ID, "user_id": USER_ID}

    billing_calls["gate"] = CreditExhaustedError("out of credit")
    blocked = await gw.get("/api/v1/llm/credit", headers=CLIENT_HEADERS)
    assert blocked.status_code == 402


async def test_credit_exhausted_inside_completion_is_402(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls, raise_exc=CreditExhaustedError("gone"))
    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)
    assert resp.status_code == 402


async def test_provider_failure_is_502(gw, monkeypatch, billing_calls):
    _install_fake_completion(
        monkeypatch, billing_calls, content="",
        usage={"prompt": 0, "completion": 0, "total": 0, "model": "m", "error": "HTTP 524: upstream timeout"},
    )
    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json=BODY)
    assert resp.status_code == 502
    assert "524" in resp.json()["detail"]


# ── streaming ──


async def test_stream_emits_openai_chunks_with_usage_and_done(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls, content="Hello there", stream_pieces=["Hello", " there"])

    resp = await gw.post(
        "/api/v1/llm/chat/completions",
        headers=CLIENT_HEADERS,
        json={**BODY, "stream": True, "stream_options": {"include_usage": True}},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events if isinstance(e, dict)]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Hello there"
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}
    assert billing_calls["contexts"][0]["entity_id"] == ENTITY_ID


async def test_stream_sends_buffered_remainder_when_provider_did_not_stream(gw, monkeypatch, billing_calls):
    # chat_completion may fall back to a buffered response and return text it
    # never streamed; the gateway must still deliver it.
    _install_fake_completion(monkeypatch, billing_calls, content="Full answer", stream_pieces=[])

    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json={**BODY, "stream": True})

    chunks = [e for e in _sse_events(resp.text) if isinstance(e, dict)]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Full answer"


async def test_stream_credit_exhausted_is_plain_402(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls)
    billing_calls["gate"] = CreditExhaustedError("out of credit")
    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json={**BODY, "stream": True})
    assert resp.status_code == 402


@pytest.mark.parametrize("stream", [False, True])
async def test_oss_rejects_missing_entity_byok_before_credit_or_provider(
    gw, gateway_app, monkeypatch, billing_calls, stream,
):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    gateway_app.state.gateway_test.oauth_client = _oauth_client(restricted_entity_id=ENTITY_ID)
    _install_fake_completion(monkeypatch, billing_calls)
    monkeypatch.setattr(
        llm_gateway,
        "_resolve_gateway_model_route",
        AsyncMock(side_effect=llm_gateway.HTTPException(503, "llm_byok_not_configured")),
        raising=False,
    )

    resp = await gw.post(
        "/api/v1/llm/chat/completions",
        headers=CLIENT_HEADERS,
        json={**BODY, "stream": stream},
    )

    assert resp.status_code == 503
    assert billing_calls["preflight"] == []
    assert billing_calls["completions"] == []


async def test_oss_gateway_uses_entity_scoped_byok_model(gw, gateway_app, monkeypatch, billing_calls):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    gateway_app.state.gateway_test.oauth_client = _oauth_client(restricted_entity_id=ENTITY_ID)
    _install_fake_completion(monkeypatch, billing_calls)
    route = _gateway_route(model="openai/gpt-5.6-terra")
    monkeypatch.setattr(
        llm_gateway, "_resolve_gateway_model_route", AsyncMock(return_value=route), raising=False,
    )

    resp = await gw.post(
        "/api/v1/llm/chat/completions",
        headers=CLIENT_HEADERS,
        json={**BODY, "model": "openai/gpt-5.6-terra"},
    )

    assert resp.status_code == 200
    sent = billing_calls["completions"][0]
    assert sent["model"] == "openai/gpt-5.6-terra"
    assert sent["metadata"] == {"llm_api_key": "sk-tenant-key"}
    assert billing_calls["contexts"][0]["byok"] is True


async def test_oss_model_route_rejects_platform_or_incompatible_credentials(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    principal = llm_gateway.GatewayPrincipal(entity_id=ENTITY_ID, user_id=USER_ID, source="test")
    import packages.core.services.model_resolver as model_resolver

    monkeypatch.setattr(
        model_resolver, "resolve_model_for_user", AsyncMock(return_value="openai/gpt-4o-mini"),
    )
    monkeypatch.setattr(
        model_resolver, "resolve_llm_metadata_for_user", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        llm_gateway,
        "resolve_llm_routing_for_model",
        AsyncMock(return_value=SimpleNamespace(api_key="platform-key", is_byok=False)),
    )

    with pytest.raises(llm_gateway.HTTPException, match="llm_byok_not_configured"):
        await llm_gateway._resolve_gateway_model_route(principal, "openai/gpt-4o-mini")

    monkeypatch.setattr(
        model_resolver,
        "resolve_llm_metadata_for_user",
        AsyncMock(return_value={"llm_api_key": "sk-tenant-key-1234567890"}),
    )
    monkeypatch.setattr(
        llm_gateway,
        "resolve_llm_routing_for_model",
        AsyncMock(return_value=SimpleNamespace(
            api_key="sk-tenant-key-1234567890",
            base_url="https://api.anthropic.com/v1",
            is_byok=True,
        )),
    )

    with pytest.raises(llm_gateway.HTTPException, match="model.*openai"):
        await llm_gateway._resolve_gateway_model_route(principal, "openai/gpt-4o-mini")


async def test_oss_model_route_accepts_entity_byok(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "oss")
    principal = llm_gateway.GatewayPrincipal(entity_id=ENTITY_ID, user_id=USER_ID, source="test")
    import packages.core.services.model_resolver as model_resolver

    metadata = {"llm_api_key": "sk-tenant-key-1234567890"}
    monkeypatch.setattr(
        model_resolver, "resolve_model_for_user", AsyncMock(return_value="openai/gpt-4o-mini"),
    )
    monkeypatch.setattr(
        model_resolver, "resolve_llm_metadata_for_user", AsyncMock(return_value=metadata),
    )
    monkeypatch.setattr(
        llm_gateway,
        "resolve_llm_routing_for_model",
        AsyncMock(return_value=SimpleNamespace(
            api_key=metadata["llm_api_key"],
            base_url="https://api.openai.com/v1",
            is_byok=True,
        )),
    )

    assert await llm_gateway._resolve_gateway_model_route(
        principal, "openai/gpt-4o-mini",
    ) == llm_gateway.GatewayModelRoute(
        model="openai/gpt-4o-mini", metadata=metadata, byok=True,
    )


async def test_client_attribution_uses_selected_active_membership_not_primary_entity(db_session):
    primary = Entity(id="gateway-primary", name="Primary")
    selected = Entity(id="gateway-selected", name="Selected")
    user = User(
        id="gateway-member", entity_id=primary.id, email="gateway-member@example.test",
        password_hash="unused", role="owner", status="active",
    )
    selected_membership = UserMembership(
        user_id=user.id, entity_id=selected.id, role="member", status="active",
    )
    db_session.add_all([
        primary,
        selected,
        user,
        UserMembership(user_id=user.id, entity_id=primary.id, role="owner", status="active"),
        selected_membership,
    ])
    await db_session.flush()

    assert await llm_gateway._client_user_has_active_entity_membership(db_session, user, selected.id)

    selected_membership.status = "inactive"
    await db_session.flush()
    assert not await llm_gateway._client_user_has_active_entity_membership(db_session, user, selected.id)


async def test_stream_provider_failure_is_error_event(gw, monkeypatch, billing_calls):
    _install_fake_completion(monkeypatch, billing_calls, raise_exc=RuntimeError("boom"))
    resp = await gw.post("/api/v1/llm/chat/completions", headers=CLIENT_HEADERS, json={**BODY, "stream": True})
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    error_events = [e for e in events if isinstance(e, dict) and "error" in e]
    assert error_events and error_events[0]["error"]["code"] == 500
    assert events[-1] == "[DONE]"
