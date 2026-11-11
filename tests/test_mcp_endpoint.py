"""Hosted MCP endpoint: OAuth challenge, audience isolation, protocol, REST dispatch, transfers.

Adapter cases use real token admission against disposable SQLite and in-process
REST stubs that authenticate with ``get_current_user``. The reviewed-draft commit
case uses the full application, isolated PostgreSQL/Redis and real persistence.
"""
from __future__ import annotations

import json
import subprocess
from urllib.parse import quote

import pytest
from fastapi import Depends, FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from sqlalchemy import JSON, MetaData
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.deps import get_current_user
from apps.api.mcp import tools_workspace, transfers
from apps.api.mcp.manor_api import ManorApi
from apps.api.routers import mcp as mcp_routes
from apps.api.routers import oauth_provider as oauth_routes
from packages.core.database import get_db
from packages.core.models.oauth_provider import OAuthClientApp, OAuthRefreshToken, PersonalAccessToken
from packages.core.models.staff import Department, Staff, StaffRole
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services import auth_service
from packages.core.services.oauth_provider_service import MCP_READ_SCOPE, MCP_SCOPE, MCP_TOKEN_AUDIENCE

pytestmark = pytest.mark.integration

ENTITY = "mcp-team"
USER_ID = "mcp-user"
PAYLOAD = {"blueprint_version": "1.1", "manifest": {"slug": "demo", "title": "Demo"},
           "contract": {}, "embedded": {}, "recipe": {"workflows": []}, "policy": {}}
DOCUMENTS = {
    "small-md": ({"id": "small-md", "name": "menu.md", "file_type": "md", "mime_type": "text/markdown",
                  "file_size": 6}, b"# Menu"),
    "large-md": ({"id": "large-md", "name": "notes.md", "file_type": "md", "mime_type": "text/markdown",
                  "file_size": 200}, b"x" * 200),
    "pdf": ({"id": "pdf", "name": "菜单.pdf", "file_type": "pdf", "mime_type": "application/pdf",
             "file_size": 9}, b"%PDF-\x00\xff\x01"),
}


class _FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)


@pytest.fixture
async def mcp_http(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_service.settings, "JWT_SECRET_KEY", "isolated-mcp-endpoint-signing-key-123456")
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "APP_URL", "http://manor.test")
    redis = _FakeRedis()

    async def fake_redis():
        return redis

    monkeypatch.setattr(transfers, "_redis", fake_redis)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp.sqlite'}")
    metadata = MetaData()
    for model in (Entity, User, UserMembership, Department, StaffRole, Staff,
                  OAuthClientApp, OAuthRefreshToken, PersonalAccessToken):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, (JSONB, ARRAY)):
                column.type = JSON()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    async with sessions.begin() as db:
        db.add_all([
            Entity(id=ENTITY, name="Team"),
            User(id=USER_ID, entity_id=ENTITY, email="fde@example.test", password_hash="unused",
                 role="owner", status="active", token_version=1),
            UserMembership(user_id=USER_ID, entity_id=ENTITY, role="owner", status="active"),
        ])

    app = FastAPI()
    app.include_router(mcp_routes.router)
    app.include_router(oauth_routes.router)  # personal access tokens and connections
    exported = []

    @app.get("/api/v1/auth/me")
    async def me(request: Request, user: User = Depends(get_current_user)):
        return {"id": user.id, "entity_id": user.entity_id, "email": user.email, "role": user.role,
                "auth_client": request.state.auth_client}

    @app.get("/api/v1/documents/{doc_id}")
    async def document(doc_id: str, user: User = Depends(get_current_user)):
        return DOCUMENTS[doc_id][0]

    @app.get("/api/v1/documents/{doc_id}/content")
    async def document_content(doc_id: str, user: User = Depends(get_current_user)):
        return {"content": DOCUMENTS[doc_id][1].decode()}

    @app.get("/api/v1/documents/{doc_id}/download")
    async def document_download(doc_id: str, user: User = Depends(get_current_user)):
        return Response(DOCUMENTS[doc_id][1], media_type=DOCUMENTS[doc_id][0]["mime_type"])

    @app.post("/api/v1/workspaces/{workspace_id}/export-blueprint-payload")
    async def export(workspace_id: str, user: User = Depends(get_current_user)):
        exported.append((workspace_id, user.id))
        return PAYLOAD

    async def database():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://manor.test") as http:
            yield http, sessions, exported
    finally:
        await engine.dispose()


def _token(audience=MCP_TOKEN_AUDIENCE, version=1):
    return auth_service.create_access_token(USER_ID, ENTITY, "owner", token_version=version, audience=audience)


async def _rpc(http, method, params=None, *, token=None, message_id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if message_id is not None:
        body["id"] = message_id
    return await http.post("/api/v1/mcp", json=body,
                           headers={"Authorization": f"Bearer {token or _token()}"})


async def _call(http, name, arguments=None, *, token=None):
    response = await _rpc(http, "tools/call", {"name": name, "arguments": arguments or {}}, token=token)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result, json.loads(result["content"][0]["text"]) if not result["isError"] else result["content"][0]["text"]


async def test_unauthenticated_request_gets_the_discovery_challenge(mcp_http):
    http, _, _ = mcp_http
    response = await http.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == (
        'Bearer resource_metadata="http://manor.test/.well-known/oauth-protected-resource/api/v1/mcp"'
        ', scope="mcp"'
    )


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_probe_without_token_gets_the_challenge_and_with_token_405(mcp_http, method):
    # Clients probe the endpoint with a GET before discovery; the challenge must
    # answer it, not a 405 that sends them guessing well-known URLs.
    http, _, _ = mcp_http
    probe = await http.request(method, "/api/v1/mcp")
    assert probe.status_code == 401
    assert "resource_metadata=" in probe.headers["www-authenticate"]
    assert 'scope="mcp"' in probe.headers["www-authenticate"]
    authed = await http.request(method, "/api/v1/mcp", headers={"Authorization": f"Bearer {_token()}"})
    assert authed.status_code == 405 and authed.headers["allow"] == "POST"


@pytest.mark.parametrize("token", [
    pytest.param(lambda: _token(audience=None), id="rest-api-token"),
    pytest.param(lambda: _token(version=0), id="revoked-token-version"),
    pytest.param(lambda: "not-a-jwt", id="garbage"),
])
async def test_only_live_mcp_audience_tokens_are_admitted(mcp_http, token):
    http, _, _ = mcp_http
    response = await _rpc(http, "tools/list", token=token())
    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    assert 'scope="mcp"' in response.headers["www-authenticate"]


async def test_browser_origin_from_another_site_is_refused(mcp_http):
    http, _, _ = mcp_http
    response = await http.post("/api/v1/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                               headers={"Authorization": f"Bearer {_token()}", "Origin": "https://evil.example"})
    assert response.status_code == 403


async def test_protocol_handshake_and_tool_listing(mcp_http):
    http, _, _ = mcp_http
    init = (await _rpc(http, "initialize", {"protocolVersion": "2025-06-18"})).json()["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    notified = await _rpc(http, "notifications/initialized", message_id=None)
    assert notified.status_code == 202
    tools = {tool["name"]: tool for tool in (await _rpc(http, "tools/list")).json()["result"]["tools"]}
    assert {"export_blueprint", "install_blueprint", "stage_workspace_change", "deliver_to_client"} <= set(tools)
    assert tools["list_workspaces"]["annotations"]["readOnlyHint"] is True
    for name in ("apply_workspace_change", "upsert_skill", "discard_workspace_change", "upsert_workflow",
                 "run_workflow", "retry_workflow_run", "toggle_scheduled_job", "run_scheduled_job", "install_blueprint",
                 "configure_knowledge_starter", "send_workspace_chat_message"):
        assert tools[name]["annotations"]["destructiveHint"] is True
    from packages.core.services.workspace_operation_service import OPERATION_PATCH_OP_CATALOG

    assert {entry.canonical for entry in tools_workspace.PUBLISHED_OPERATIONS} == {
        entry.canonical for entry in OPERATION_PATCH_OP_CATALOG
    }
    published = {entry.canonical: entry for entry in tools_workspace.PUBLISHED_OPERATIONS}
    for entry in OPERATION_PATCH_OP_CATALOG:
        if entry.payload_schema is not None:
            assert published[entry.canonical].payload_schema == entry.payload_schema
    for entry in tools_workspace.PUBLISHED_OPERATIONS:
        tool = tools[tools_workspace.operation_tool_name(entry.canonical)]
        Draft202012Validator.check_schema(tool["inputSchema"])
        assert tool["inputSchema"]["properties"]["payload"] == entry.payload_schema
        assert tool["annotations"]["readOnlyHint"] is False  # persists a draft
        assert tool["annotations"]["destructiveHint"] is False  # does not commit it
    Draft202012Validator.check_schema(tools["stage_workspace_change"]["inputSchema"])
    unknown = (await _rpc(http, "resources/list")).json()
    assert unknown["error"]["code"] == -32601


async def test_tools_act_as_the_token_user_through_the_rest_api(mcp_http):
    http, _, _ = mcp_http
    result, me = await _call(http, "whoami")
    assert result["isError"] is False
    assert (me["id"], me["entity_id"]) == (USER_ID, ENTITY)
    result, error = await _call(http, "get_workspace", {})
    assert result["isError"] is True and "workspace_id" in error


async def test_tool_calls_carry_the_acting_client_to_rest_and_log_it_without_arguments(mcp_http, caplog):
    http, _, _ = mcp_http
    client_id = "https://client.example.test/oauth/client.json"
    token = auth_service.create_access_token(USER_ID, ENTITY, "owner", token_version=1,
                                             audience=MCP_TOKEN_AUDIENCE, authorized_party=client_id)
    with caplog.at_level("INFO", logger="apps.api.mcp.protocol"):
        await _rpc(http, "tools/call", {"name": "whoami", "arguments": {}}, token=token)
        await _rpc(http, "tools/call", {"name": "get_workspace", "arguments": {"note": "secret-argument"}},
                   token=token)
    logged = [r.getMessage() for r in caplog.records if r.getMessage().startswith("MCP tool call")]
    assert f"azp={client_id} tool=whoami" in logged[0] and logged[0].endswith("outcome=ok")
    assert "tool=get_workspace" in logged[1] and "secret-argument" not in " ".join(logged)

    # The internal REST token a tool sends names the same client; a web token names none.
    claims = auth_service.decode_token(token, audience=MCP_TOKEN_AUDIENCE)
    app = http._transport.app
    assert (await ManorApi(app, claims=claims).get("/api/v1/auth/me"))["auth_client"] == client_id
    claims.pop("azp")
    assert (await ManorApi(app, claims=claims).get("/api/v1/auth/me"))["auth_client"] is None


def _scoped_token(scope):
    return auth_service.create_access_token(USER_ID, ENTITY, "owner", token_version=1,
                                            audience=MCP_TOKEN_AUDIENCE, scope=scope)


async def _tool_names(http, token):
    return {tool["name"] for tool in (await _rpc(http, "tools/list", token=token)).json()["result"]["tools"]}


async def test_read_only_scope_hides_and_refuses_write_tools_before_anything_runs(mcp_http):
    http, _, exported = mcp_http
    read_only = _scoped_token(MCP_READ_SCOPE)
    visible = await _tool_names(http, read_only)
    assert {"whoami", "export_blueprint", "search_knowledge"} <= visible
    assert not visible & {"apply_workspace_change", "install_blueprint", "deliver_to_client"}
    # The full scope, and a token minted before scopes existed, keep every tool.
    for token in (_scoped_token(MCP_SCOPE), _token(), _scoped_token("")):
        assert "apply_workspace_change" in await _tool_names(http, token)

    refused = await _rpc(http, "tools/call", {"name": "apply_workspace_change", "arguments": {}}, token=read_only)
    assert refused.status_code == 403
    challenge = refused.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge and 'scope="mcp"' in challenge
    assert "resource_metadata=" in challenge

    # A batch with one refused call runs none of it, not even the permitted export.
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "export_blueprint", "arguments": {"workspace_id": "ws-1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "apply_workspace_change", "arguments": {}}},
    ]
    response = await http.post("/api/v1/mcp", json=batch, headers={"Authorization": f"Bearer {read_only}"})
    assert response.status_code == 403 and exported == []

    allowed = await _rpc(http, "tools/call", {"name": "export_blueprint", "arguments": {"workspace_id": "ws-1"}},
                         token=read_only)
    assert allowed.status_code == 200 and allowed.json()["result"]["isError"] is False
    assert exported == [("ws-1", USER_ID)]


async def test_operation_vocabulary_uses_the_mcp_call_shape(mcp_http):
    http, _, _ = mcp_http
    _, described = await _call(http, "describe_workspace_operations")
    listed = (await _rpc(http, "tools/list")).json()["result"]["tools"]
    schemas = {tool["name"]: tool["inputSchema"] for tool in listed}
    assert described["patch_schema"] == schemas["stage_workspace_change"]["properties"]["patches"]["items"]
    assert described["operations"]
    for operation in described["operations"]:
        assert operation["payload_schema"] == schemas[operation["tool"]]["properties"]["payload"]


async def test_operation_adapters_reject_unpublished_or_invalid_input_before_writing():
    from apps.api.mcp.registry import ToolContext, ToolError

    api = _RecordingApi()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    for patches in ([{"op": "hidden.execute", "payload": {}}],
                    [{"op": "heartbeat_policy.update", "payload": {"enabled": "yes"}}],
                    [{"op": "app_view.update", "payload": {"asks": {"action:next": "series_id"}}}],
                    [{"op": "app_view.update", "payload": {"i18n": []}}],
                    [{"op": "app_view.update", "payload": {"app_view": {"i18n": "Chinese"}}}]):
        with pytest.raises(ToolError, match="published operation"):
            await tools_workspace.stage_change(ctx, {"workspace_id": "ws-1", "patches": patches})
    with pytest.raises(ToolError, match="review_fingerprint"):
        await tools_workspace.apply_change(ctx, {"workspace_id": "ws-1", "draft_id": "draft-1"})
    fixed = mcp_routes.dispatcher.tools["stage_workspace_rule_add"]
    with pytest.raises(ToolError, match="operation is fixed"):
        await fixed.handler(ctx, {"workspace_id": "ws-1", "payload": {}, "op": "rule.remove"})
    assert api.calls == []


async def test_stage_returns_one_review_snapshot_and_apply_passes_its_preconditions():
    from apps.api.mcp.registry import ToolContext
    from packages.core.services.workspace_operation_service import operation_draft_review_fingerprint

    api = _RecordingApi()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    staged = await tools_workspace.stage_change(ctx, {"workspace_id": "ws-1", "patches": [
        {"op": "guardrail.add", "payload": {"description": "Review outgoing messages"}},
        {"op": "budget.update", "payload": {"monthly_budget_credits": 20}},
    ]})
    assert len(api.calls) == 1
    body = api.calls[0][2]
    assert [patch["op"] for patch in body["patches"]] == ["rule.add", "budget_policy.update"]
    assert set(body["allowed_patch_operations"]) == set(_OPERATION_SAMPLES)
    assert staged["review_fingerprint"] == operation_draft_review_fingerprint({"id": "draft-1"})
    await tools_workspace.apply_change(ctx, {"workspace_id": "ws-1", **staged})
    assert api.calls[-1][2] == {
        "user_confirmation": True, "expected_review_fingerprint": staged["review_fingerprint"],
        "allowed_patch_operations": body["allowed_patch_operations"],
    }


@pytest.mark.parametrize(
    ("message", "waits"),
    [
        ({"pending_action": {"kind": "workflow_approval"}, "resolved_at": None}, True),
        # Staging, 2026-11-04: a Workflow approval answered in Manor closes the
        # message, while its hitl_requests copy still reads resolved: false.
        ({"pending_action": {"kind": "workflow_approval"}, "resolved_at": "2026-11-02T01:04:43Z",
          "hitl_requests": [{"id": "card-1", "resolved": False}]}, False),
        ({"pending_action": None, "resolved_at": None, "hitl_requests": [{"id": "card-1"}]}, True),
        ({"pending_action": None, "resolved_at": None, "hitl_requests": [{"id": "card-1", "resolved": True}]}, False),
        ({"pending_action": None, "resolved_at": None,
          "hitl_requests": [{"id": "card-1", "resolved": True}, {"id": "card-2"}]}, True),
        ({"body": "A plain reply"}, False),
    ],
)
def test_only_an_unanswered_card_waits_for_a_person(message, waits):
    from apps.api.mcp import tools_chat

    assert tools_chat._waits_for_person(message) is waits


async def test_workspace_chat_waits_a_bounded_time_and_surfaces_cards_without_answering(monkeypatch):
    from apps.api.mcp import tools_chat
    from apps.api.mcp.registry import ToolContext

    sent = {"id": "msg-2", "conversation_id": "conv-1", "author_kind": "user", "body": "Send the digest"}
    card = {"id": "msg-3", "author_kind": "agent", "body": "Approve sending?", "resolved_at": None,
            "pending_action": {"kind": "external_message_approval"}}
    pages = [[{"id": "msg-1", "author_kind": "agent", "body": "Earlier"}, sent],
             [{"id": "msg-1", "author_kind": "agent", "body": "Earlier"}, sent, card]]

    class _ChatApi(_RecordingApi):
        async def request(self, method, path, *, json_body=None, params=None, files=None, headers=None):
            self.calls.append((method, path, json_body, False))
            if method == "POST":
                return sent
            return {"conversation_id": "conv-1", "items": pages.pop(0) if pages else []}

    monkeypatch.setattr(tools_chat, "_POLL_SECONDS", 0)
    api = _ChatApi()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    replied = await tools_chat.send_workspace_chat_message(
        ctx, {"workspace_id": "ws-1", "message": "Send the digest", "wait_seconds": 5})
    assert replied["status"] == "replied" and replied["message_id"] == "msg-2"
    assert [m["id"] for m in replied["messages"]] == ["msg-3"]
    assert replied["needs_person"] == ["msg-3"]
    assert api.calls[0][2] == {"body": "Send the digest", "audience": "ai"}
    assert not any("resolve" in path or "approval" in path for _, path, _, _ in api.calls)

    api.calls.clear()
    working = await tools_chat.send_workspace_chat_message(
        ctx, {"workspace_id": "ws-1", "message": "Send the digest", "wait_seconds": 0})
    assert working["status"] == "working" and working["conversation_id"] == "conv-1"
    assert "after_message_id=msg-2" in working["next"]
    assert [method for method, *_ in api.calls] == ["POST"]


async def test_workspace_draft_turns_outlive_the_wait_and_busy_drafts_refuse_messages():
    """Regression (staging 2026-11-01): a first Architect turn took 186 s, so the
    MCP call timed out while the turn went on; a message sent meanwhile 500'd."""
    import asyncio

    from apps.api.mcp import tools_create
    from apps.api.mcp.manor_api import ManorApiError
    from apps.api.mcp.registry import ToolContext, ToolError

    release = asyncio.Event()
    finished = asyncio.Event()

    class _DraftApi(_RecordingApi):
        async def request(self, method, path, *, json_body=None, params=None, files=None, headers=None):
            self.calls.append((method, path, json_body, False))
            if path.endswith("/messages") and json_body["message"] == "busy":
                raise ManorApiError(409, {"code": "draft_busy", "message": "The Architect is still working"})
            draft_id = json_body.get("draft_id") or path.split("/")[4]
            if path == "/api/v1/workspace-drafts":
                await release.wait()  # the opening turn outlasts the wait
                finished.set()
            ready = path.endswith("/messages")
            return {"reply": "Done.", "draft": {"id": draft_id, "status": "ready" if ready else "active",
                                                "ready": ready, "working": False, "messages": []}}

    api = _DraftApi()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    started = await tools_create.start_workspace_draft(ctx, {"brief": "A bakery", "wait_seconds": 0.05})
    draft_id = started["draft_id"]
    # The caller gets the id it can poll with; the turn was sent with that same id.
    assert started["status"] == "working" and started["working"] is True
    assert api.calls[0][2] == {"initial_brief": "A bakery", "draft_id": draft_id}
    assert "get_workspace_draft" in started["next_step"]
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=5)  # the turn ran on after the call returned

    ready = await tools_create.continue_workspace_draft(ctx, {"draft_id": draft_id, "message": "Ship it"})
    assert ready["ready"] is True and ready["reply"] == "Done."
    assert f"finalize_workspace_draft with draft_id={draft_id}" in ready["next_step"]

    with pytest.raises(ToolError, match="still working.*not sent.*get_workspace_draft"):
        await tools_create.continue_workspace_draft(ctx, {"draft_id": draft_id, "message": "busy"})


async def test_an_app_design_that_outlasts_the_call_is_read_back_not_run_twice(monkeypatch):
    """Staging: the designer took 122 s and the MCP client gave up at ~120 s,
    so an agent saw a failure and designed again while the first one finished."""
    import asyncio

    from apps.api.mcp.manor_api import ManorApiError
    from apps.api.mcp.registry import ToolContext, ToolError

    redis = _FakeRedis()

    async def fake_redis():
        return redis

    monkeypatch.setattr(tools_workspace, "_design_redis", fake_redis)
    release = asyncio.Event()

    class _DesignApi(_RecordingApi):
        async def request(self, method, path, *, json_body=None, params=None, files=None, headers=None):
            self.calls.append((method, path, json_body, False))
            if json_body["brief"] == "refuse":
                raise ManorApiError(422, {"error": "the App designer did not produce a page that passes its checks",
                                          "design_notes": "The page needs a dish source."})
            await release.wait()
            return {"draft": {"id": "draft-9", "validation": {}, "diff": {}}, "candidate": {"title": "Menu"},
                    "design_notes": "Dishes by name."}

    api = _DesignApi()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    started = await tools_workspace.design_app_view(ctx, {"workspace_id": "ws-1", "brief": "Dishes by name",
                                                          "wait_seconds": 0.05})
    design_id = started["design_id"]
    assert started["status"] == "working" and "get_workspace_app_design" in started["next_step"]
    still = await tools_workspace.get_app_design(ctx, {"workspace_id": "ws-1", "design_id": design_id})
    assert still["status"] == "working"

    release.set()
    for _ in range(50):  # the design runs on after the call returned
        done = await tools_workspace.get_app_design(ctx, {"workspace_id": "ws-1", "design_id": design_id})
        if done["status"] != "working":
            break
        await asyncio.sleep(0.01)
    assert done["status"] == "done" and done["draft_id"] == "draft-9" and done["design_notes"] == "Dishes by name."
    assert len(done["review_fingerprint"]) == 64
    assert len(api.calls) == 1

    # Only its starter reads it.
    other = ToolContext(api=api, claims={"sub": "someone-else", "entity_id": ENTITY}, request=None)
    with pytest.raises(ToolError, match="No App design"):
        await tools_workspace.get_app_design(other, {"workspace_id": "ws-1", "design_id": design_id})

    # A refused design still says why, as before.
    with pytest.raises(ToolError, match="did not produce a page.*needs a dish source"):
        await tools_workspace.design_app_view(ctx, {"workspace_id": "ws-1", "brief": "refuse"})

    # One cut off with the process that ran it is not "working" forever.
    key = next(key for key in redis.values if key.endswith(design_id))
    redis.values[key] = json.dumps({**json.loads(redis.values[key]), "status": "working", "started_at": 0})
    stopped = await tools_workspace.get_app_design(ctx, {"workspace_id": "ws-1", "design_id": design_id})
    assert stopped["status"] == "stopped" and "design_workspace_app_view again" in stopped["next_step"]


async def test_a_draft_turn_that_ended_without_a_reply_does_not_read_as_answered():
    """A poller waits for working=false and reads reply: the Architect's earlier
    reply and question must not pass for the answer to the latest message."""
    from apps.api.mcp import tools_create
    from apps.api.mcp.registry import ToolContext

    class _DraftApi(_RecordingApi):
        async def request(self, method, path, *, json_body=None, params=None, files=None, headers=None):
            return {"id": "draft-1", "status": "ready", "ready": True, "working": False, "messages": [
                {"role": "assistant", "content": "Which day?", "question": {"text": "Which day?"}},
                {"role": "user", "content": "Add a weekly report"},
            ]}

    ctx = ToolContext(api=_DraftApi(), claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    draft = await tools_create.get_workspace_draft(ctx, {"draft_id": "draft-1"})
    assert draft["reply"] is None and draft["question"] is None
    assert "continue_workspace_draft" in draft["next_step"]
    assert "finalize_workspace_draft" not in draft["next_step"]


async def test_operation_schemas_preserve_existing_blueprint_configuration_shapes():
    from apps.api.mcp.registry import ToolContext
    from packages.core.services.workspace_app_view import normalize_app_view_config
    from packages.core.services.workspace_operation_service import _apply_single_patch

    api = _RecordingApi()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    # Shipped Blueprints contain text Strategist settings and nullable goal
    # values. Normalized App snapshots include version and the asks map.
    for operation, payload in (
        ("strategist.update", {"business_model": "Content services", "proposal_shape": "One task",
                               "voice": "Concise", "priors": {"quality": "evidence"}}),
        ("goal.upsert", {"goal_key": "quality", "deadline": None, "baseline_value": None,
                         "measurement_source": None, "measurement_cadence": None, "description": None}),
        ("heartbeat_policy.update", {"enabled": False, "cadence": None}),
        ("operating_model.update", {"app_view": normalize_app_view_config(None)}),
        ("operating_model.update", {"app_view": {**normalize_app_view_config(None), "i18n": None}}),
        ("operating_model.replace", {"app_view": normalize_app_view_config({
            "title": "Research", "i18n": {"zh-CN": {"title": "研究"}},
        })}),
    ):
        await mcp_routes.dispatcher.tools[tools_workspace.operation_tool_name(operation)].handler(
            ctx, {"workspace_id": "ws-1", "payload": payload},
        )
    assert len(api.calls) == 6
    state = {"operating_model": {"app_view": normalize_app_view_config(None)}}
    for asks, expected in (({"action:next": ["series_id"]}, {"action:next": ["series_id"]}),
                           ({"action:next": None}, {})):
        await tools_workspace.stage_change(ctx, {"workspace_id": "ws-1", "patches": [
            {"op": "app_view.update", "payload": {"asks": asks}},
        ]})
        state = _apply_single_patch(state, api.calls[-1][2]["patches"][0])
        assert state["operating_model"]["app_view"]["asks"] == expected


async def test_mcp_draft_commit_preserves_review_and_workspace_authorization(client, db_session, isolated_test_redis):
    """Typed configuration survives one real, authorized and reviewed commit."""
    from sqlalchemy import select
    from packages.core.models.base import generate_ulid
    from packages.core.models.mcp import MCPServer
    from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition
    from packages.core.services.agent_provisioning_service import spec_from_create_agent_draft

    # A test-owned account-free MCP resource exercises real readiness without
    # credentials or outbound calls; the endpoint is deliberately non-routable.
    db_session.add(MCPServer(id=generate_ulid(), server_key="mcp_contract_fixture",
                             name="Contract fixture", transport="http", auth_type="none",
                             endpoint="https://mcp-contract.invalid/mcp", status="active"))

    design = {"agent_name": "Evidence Reader", "agent_description": "Read cited source material",
              "system_prompt": "Read source files and report only supported evidence.",
              "tool_bindings": ["read_file"]}
    tool_names = spec_from_create_agent_draft(design, service_key="research").tool_bindings
    existing = set((await db_session.execute(select(ToolDefinition.name).where(
        ToolDefinition.name.in_(tool_names),
    ))).scalars().all())
    db_session.add_all([ToolDefinition(id=generate_ulid(), name=name, status="active")
                        for name in tool_names if name not in existing])
    await db_session.commit()
    registered = await client.post("/api/v1/auth/register", json={
        "username": "mcp_review_owner", "email": "mcp_review_owner@example.test",
        "password": "mcp-review-password-123", "entity_name": "MCP Review Team",
    })
    assert registered.status_code == 200, registered.text
    access = registered.json()["access_token"]
    claims = auth_service.decode_token(access)
    token = auth_service.create_access_token(
        claims["sub"], claims["entity_id"], claims["role"],
        token_version=claims.get("token_version", 0), audience=MCP_TOKEN_AUDIENCE,
    )
    headers = {"Authorization": f"Bearer {access}"}
    created = await client.post("/api/v1/workspaces", headers=headers, json={"name": "Review boundary"})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    result, first = await _call(client, "stage_workspace_rule_add", {
        "workspace_id": workspace_id, "payload": {"description": "Use approved sources"},
    }, token=token)
    assert result["isError"] is False, first
    result, second = await _call(client, "stage_workspace_knowledge_policy_update", {
        "workspace_id": workspace_id, "draft_id": first["draft_id"], "payload": {"auto_search": False},
    }, token=token)
    assert result["isError"] is False, second
    assert second["review_fingerprint"] != first["review_fingerprint"]
    # These exact advertised fields must survive the real provisioning and
    # capability normalizers, not just a mocked REST dispatch.
    for tool, payload in (
        ("stage_workspace_service_role_upsert", {"key": "research", "name": "Research"}),
        ("stage_workspace_agent_binding_upsert", {
            "service_key": "research", "strategy": "create_custom", "create_agent_draft": design,
            "custom_prompt": "Use this Workspace's approved evidence only.",
        }),
        ("stage_workspace_capability_binding_upsert", {
            "binding_key": "research-public-mcp", "capability_type": "mcp", "owner_scope": "service",
            "owner_service_key": "research", "integration_key": "mcp_contract_fixture", "allowed_tools": ["search_code"],
            "enabled": False,
        }),
        ("stage_workspace_app_view_update", {
            "title": "Research", "i18n": {"zh-CN": {"title": "研究", "targets": {"action:run": "other"}}},
        }),
    ):
        result, second = await _call(client, tool, {
            "workspace_id": workspace_id, "draft_id": first["draft_id"], "payload": payload,
        }, token=token)
        assert result["isError"] is False, second

    assert second["validation"]["valid"], second["validation"]
    result, stale = await _call(client, "apply_workspace_change", {
        "workspace_id": workspace_id, "draft_id": first["draft_id"],
        "review_fingerprint": first["review_fingerprint"],
    }, token=token)
    assert result["isError"] is True and json.loads(stale)["status"] == 409, stale
    denied = await client.post(
        f"/api/v1/workspaces/{workspace_id}/operation/drafts/{first['draft_id']}/apply",
        headers=headers, json={"user_confirmation": True, "allowed_patch_operations": ["rule.add"],
                               "expected_review_fingerprint": second["review_fingerprint"]},
    )
    assert denied.status_code == 409 and "not exposed" in denied.text
    current = await client.get(f"/api/v1/workspaces/{workspace_id}/operation/current", headers=headers)
    assert current.json()["state"]["rules"] == []

    outsider = await client.post("/api/v1/auth/register", json={
        "username": "mcp_review_other", "email": "mcp_review_other@example.test",
        "password": "mcp-review-password-456", "entity_name": "Other MCP Team",
    })
    assert outsider.status_code == 200, outsider.text
    other = auth_service.decode_token(outsider.json()["access_token"])
    foreign_token = auth_service.create_access_token(
        other["sub"], other["entity_id"], other["role"],
        token_version=other.get("token_version", 0), audience=MCP_TOKEN_AUDIENCE,
    )
    result, forbidden = await _call(client, "apply_workspace_change", {
        "workspace_id": workspace_id, "draft_id": second["draft_id"],
        "review_fingerprint": second["review_fingerprint"],
    }, token=foreign_token)
    assert result["isError"] is True and json.loads(forbidden)["status"] in (403, 404)

    result, applied = await _call(client, "apply_workspace_change", {
        "workspace_id": workspace_id, "draft_id": second["draft_id"],
        "review_fingerprint": second["review_fingerprint"],
    }, token=token)
    assert result["isError"] is False, applied
    assert applied["draft"]["status"] == "applied"
    current = await client.get(f"/api/v1/workspaces/{workspace_id}/operation/current", headers=headers)
    state = current.json()["state"]
    assert state["rules"][0]["description"] == "Use approved sources"
    assert state["knowledge_policy"]["auto_search"] is False
    app_view = state["operating_model"]["app_view"]
    assert app_view["title"] == "Research" and app_view["i18n"] == {"zh": {"title": "研究"}}
    assert app_view["targets"] == {}
    binding = next(row for row in state["capability_bindings"] if row["binding_key"] == "research-public-mcp")
    assert {key: binding[key] for key in ("capability_type", "owner_scope", "owner_service_key",
                                         "integration_key", "allowed_tools", "enabled")} == {
        "capability_type": "mcp", "owner_scope": "service", "owner_service_key": "research",
        "integration_key": "mcp_contract_fixture", "allowed_tools": ["search_code"], "enabled": False,
    }
    db_session.expire_all()
    subscription = (await db_session.execute(select(AgentSubscription).where(
        AgentSubscription.workspace_id == workspace_id, AgentSubscription.service_key == "research",
        AgentSubscription.status == "active",
    ))).scalar_one()
    assert subscription.custom_prompt == "Use this Workspace's approved evidence only."
    agent = await db_session.get(Agent, subscription.agent_id)
    assert (agent.name, agent.description, agent.system_prompt) == (
        design["agent_name"], design["agent_description"], design["system_prompt"],
    )
    persisted_tools = set((await db_session.execute(select(ToolDefinition.name).join(
        AgentToolBinding, AgentToolBinding.tool_id == ToolDefinition.id,
    ).where(AgentToolBinding.agent_id == agent.id))).scalars().all())
    assert "read_file" in persisted_tools

    # The wrapped form can remove one stored translation through a new,
    # independently reviewed draft without changing the authored base text.
    result, cleared = await _call(client, "stage_workspace_app_view_update", {
        "workspace_id": workspace_id, "payload": {"app_view": {"i18n": {"zh": None}}},
    }, token=token)
    assert result["isError"] is False and cleared["validation"]["valid"], cleared
    result, applied = await _call(client, "apply_workspace_change", {
        "workspace_id": workspace_id, "draft_id": cleared["draft_id"],
        "review_fingerprint": cleared["review_fingerprint"],
    }, token=token)
    assert result["isError"] is False, applied
    current = await client.get(f"/api/v1/workspaces/{workspace_id}/operation/current", headers=headers)
    app_view = current.json()["state"]["operating_model"]["app_view"]
    assert app_view["title"] == "Research" and "i18n" not in app_view


@pytest.mark.parametrize("initial_state", ["empty", "unregistered_default", "legacy_folder"])
async def test_mcp_workspace_reads_do_not_initialize_state(
    client, db_session, isolated_test_redis, initial_state,
):
    from copy import deepcopy

    from sqlalchemy import event, select

    import packages.core.database as db_module
    from auth_helpers import register_user_and_get_token
    from packages.core.models.document import DocumentFolder, DocumentGroup
    from packages.core.models.workspace import Workspace

    registered = await register_user_and_get_token(client, {
        "username": "mcp_knowledge_reader", "email": "mcp_knowledge_reader@example.test",
        "password": "mcp-knowledge-password-123", "entity_name": "MCP Knowledge Team",
    })
    access = registered.json()["access_token"]
    claims = auth_service.decode_token(access)
    token = auth_service.create_access_token(
        claims["sub"], claims["entity_id"], claims["role"],
        token_version=claims.get("token_version", 0), audience=MCP_TOKEN_AUDIENCE,
    )
    created = await client.post(
        "/api/v1/workspaces", headers={"Authorization": f"Bearer {access}"},
        json={"name": "Read-only Knowledge"},
    )
    assert created.status_code == 201, created.text
    workspace_id = created.json()["id"]
    result, draft = await _call(client, "stage_workspace_rule_add", {
        "workspace_id": workspace_id, "payload": {"description": "Use approved sources"},
    }, token=token)
    assert result["isError"] is False, draft

    workspace = await db_session.get(Workspace, workspace_id)
    group = None
    if initial_state == "unregistered_default":
        group = DocumentGroup(
            entity_id=claims["entity_id"], workspace_id=workspace_id,
            name="Existing default", settings={"default_collection": True},
        )
        db_session.add(group)
    elif initial_state == "legacy_folder":
        workspace.artifact_folder_id = None
    await db_session.commit()
    await db_session.refresh(workspace)
    before = (deepcopy(workspace.operating_model), workspace.operation_revision,
              workspace.artifact_folder_id, workspace.updated_at)
    folder_ids = set((await db_session.execute(select(DocumentFolder.id).where(
        DocumentFolder.entity_id == claims["entity_id"],
    ))).scalars())
    statements = []

    def record_query(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(db_module.engine.sync_engine, "before_cursor_execute", record_query)
    try:
        for _ in range(2):
            result, workspaces = await _call(client, "list_workspaces", token=token)
            assert result["isError"] is False, workspaces
            assert [row["id"] for row in workspaces] == [workspace_id]
            result, details = await _call(client, "get_workspace", {
                "workspace_id": workspace_id, "include": ["app_view", "agents", "operation_state"],
            }, token=token)
            assert result["isError"] is False, details
            assert set(details) == {"workspace", "app_view", "agents", "operation_state"}
            assert details["workspace"]["id"] == workspace_id
            result, listed = await _call(client, "list_knowledge", {"workspace_id": workspace_id}, token=token)
            assert result["isError"] is False, listed
            assert [row["id"] for row in listed["collections"]] == ([group.id] if group else [])
    finally:
        event.remove(db_module.engine.sync_engine, "before_cursor_execute", record_query)
    assert not any(sql.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for sql in statements)
    await db_session.refresh(workspace)
    assert (workspace.operating_model, workspace.operation_revision,
            workspace.artifact_folder_id, workspace.updated_at) == before
    assert set((await db_session.execute(select(DocumentFolder.id).where(
        DocumentFolder.entity_id == claims["entity_id"],
    ))).scalars()) == folder_ids
    assert list((await db_session.execute(select(DocumentGroup.id).where(
        DocumentGroup.workspace_id == workspace_id,
    ))).scalars()) == ([group.id] if group else [])
    result, applied = await _call(client, "apply_workspace_change", {
        "workspace_id": workspace_id, "draft_id": draft["draft_id"],
        "review_fingerprint": draft["review_fingerprint"],
    }, token=token)
    assert result["isError"] is False, applied
    assert applied["draft"]["status"] == "applied"

    initialized = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert initialized.status_code == 200, initialized.text
    assert any(row["is_default_collection"] for row in initialized.json())
    await db_session.refresh(workspace)
    assert workspace.artifact_folder_id is not None



async def test_export_download_upload_and_compare_round_trip(mcp_http, tmp_path):
    http, _, exported = mcp_http
    filename = "blueprint's $(printf unsafe); review.json"
    _, export = await _call(http, "export_blueprint", {"workspace_id": "ws-1", "filename": filename})
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\"\n")
    curl.chmod(0o700)
    command = subprocess.run(
        ["/bin/sh", "-c", export["command"]], env={"PATH": str(tmp_path)},
        capture_output=True, text=True, check=True, timeout=5,
    )
    assert command.stdout.split("\0")[:-1] == ["-sS", "--fail", "-o", filename, export["download_url"]]
    assert exported == [("ws-1", USER_ID)]
    path = export["download_url"].removeprefix("http://manor.test")
    downloaded = await http.get(path)
    assert downloaded.status_code == 200 and json.loads(downloaded.content) == PAYLOAD

    _, upload = await _call(http, "create_upload", {"filename": "blueprint.json"})
    upload_path = upload["upload_url"].removeprefix("http://manor.test")
    assert (await http.get(upload_path)).status_code == 404  # an upload slot is not downloadable
    sent = await http.put(upload_path, content=downloaded.content,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert sent.status_code == 200, sent.text

    _, comparison = await _call(http, "compare_blueprints", {
        "left": {"upload_id": upload["upload_id"]}, "right": {"workspace_id": "ws-1"}})
    assert comparison == {"equivalent": True, "differences": []}


@pytest.mark.parametrize("doc_id, inline", [
    pytest.param("small-md", True, id="small-text-inline"),
    pytest.param("large-md", False, id="text-over-inline-limit"),
    pytest.param("pdf", False, id="binary-non-ascii-name"),
])
async def test_read_knowledge_document_inlines_small_text_and_downloads_the_rest(mcp_http, doc_id, inline):
    http, _, _ = mcp_http
    metadata, data = DOCUMENTS[doc_id]
    result, read = await _call(http, "read_knowledge_document", {"document_id": doc_id, "max_inline_bytes": 100})
    assert result["isError"] is False, read
    assert read["document"]["name"] == metadata["name"]
    if inline:
        assert read["content"] == data.decode() and "download_url" not in read
        return
    assert "content" not in read and read["bytes"] == len(data)
    downloaded = await http.get(read["download_url"].removeprefix("http://manor.test"))
    assert downloaded.status_code == 200 and downloaded.content == data
    assert downloaded.headers["content-disposition"].endswith(
        "filename*=UTF-8''" + quote(metadata["name"], safe=""))


async def test_an_upload_is_only_usable_by_the_user_and_team_that_created_it(mcp_http):
    http, _, _ = mcp_http
    _, upload = await _call(http, "create_upload", {"filename": "blueprint.json"})
    await http.put(upload["upload_url"].removeprefix("http://manor.test"), content=b"{}")
    with pytest.raises(ValueError):
        await transfers.load_upload_for(upload["upload_id"], user_id="someone-else", entity_id=ENTITY)
    with pytest.raises(ValueError):
        await transfers.load_upload_for(upload["upload_id"], user_id=USER_ID, entity_id="other-team")
    assert (await transfers.load_upload_for(upload["upload_id"], user_id=USER_ID, entity_id=ENTITY)).data == b"{}"


# ── Contract guard: every MCP tool still matches a live REST route and its body ──
#
# The tools are adapters over Manor's REST routes (AGENTS.md: "Manor MCP and
# plugin stay in sync with Workspace tools"). Run each tool against a recording
# API, then resolve every recorded request against the real application's route
# table and validate its JSON body with that route's own body model. Renaming,
# removing or re-shaping a route without updating the tool fails here.

_CANNED = (
    ("GET", "/api/v1/workspaces/ws-1", {"artifact_folder_id": "folder-1"}),
    ("GET", "/api/v1/workspaces/ws-1/operation/current", {"state": {}}),
    ("POST", "/api/v1/workspaces/ws-1/operation/drafts", {"id": "draft-1"}),
    ("PATCH", "/api/v1/workspaces/ws-1/operation/drafts/draft-1", {"id": "draft-1"}),
    ("POST", "/api/v1/workspaces/ws-1/operation/drafts/draft-1/discard", {"id": "draft-1", "status": "discarded"}),
    ("POST", "/api/v1/workspaces/ws-1/app-view/design", {"draft": {"id": "draft-2"}, "candidate": {}}),
    ("POST", "/api/v1/workspaces/ws-1/export-blueprint-payload", PAYLOAD),
    ("GET", "/api/v1/workspaces", []),
    ("GET", "/api/v1/workspaces/ws-1/promote/preflight", []),
    ("GET", "/api/v1/clients", {"items": [{"id": "client-1"}], "total": 1}),
    ("GET", "/api/v1/clients/client-1/contacts", []),
    ("GET", "/api/v1/clients/client-1/deliveries", []),
    ("GET", "/api/v1/documents/doc-1", {"id": "doc-1", "name": "menu.md", "file_type": "md",
                                        "mime_type": "text/markdown", "file_size": 6}),
    ("GET", "/api/v1/documents/doc-1/content", {"content": "# Menu"}),
    ("GET", "/api/v1/documents/doc-2", {"id": "doc-2", "name": "menu.pdf", "file_type": "pdf",
                                        "mime_type": "application/pdf", "file_size": 4}),
    ("GET", "/api/v1/tasks/task-1/logs", []),
    ("GET", "/api/v1/jobs/job-1/runs", []),
    ("GET", "/api/v1/workspace-drafts", []),
    ("POST", "/api/v1/workspaces/ws-1/chat/messages", {"id": "msg-1", "conversation_id": "conv-1"}),
)

_VALID_STEPS = [{"id": "start", "type": "trigger", "config": {}, "next": ["n"]},
                {"id": "n", "type": "llm", "config": {"prompt": "hi"}}]

_OPERATION_SAMPLES = {
    "operating_model.replace": {"services": [{"key": "support", "name": "Support"}]},
    "operating_model.update": {"strategist": {"priors": ["Use approved Knowledge"]}},
    "app_view.update": {"title": "Customer support", "visual": "plain"},
    "workflow_binding.attach": {"workflow_id": "wf-1", "primary": True, "slug": "guest-metrics"},
    "evaluation.update": {"scorecard": [{"metric_key": "quality", "weight": 1}], "target_score": 90},
    "budget_policy.update": {"monthly_budget_credits": 20},
    "heartbeat_policy.update": {"enabled": False},
    "governance_policy.update": {"max_risk_level": "low"},
    "rule.add": {"description": "Cite evidence"},
    "rule.remove": {"rule_key": "cite"},
    "rule.update": {"rule_key": "cite", "rule": {"description": "Cite current evidence"}},
    "rules.replace": {"rules": []}, "goals.replace": {"goals": []},
    "goal.upsert": {"goal_key": "quality", "title": "Improve quality", "target_value": 10},
    "goal.measurement_sources.update": {"updates": [{"goal_key": "quality", "measurement_source": {"provider": "manual"}}]},
    "service_role.upsert": {"key": "support", "name": "Support"},
    "service_role.remove": {"key": "support"},
    "knowledge_policy.update": {"auto_search": True},
    "capability_binding.upsert": {"binding_key": "read", "capability_type": "tool", "owner_scope": "workspace_agent", "tool_name": "read_file"},
    "capability_binding.remove": {"binding_key": "read"},
    "skill_binding.upsert": {"binding_key": "review", "skill_id": "sk-1"},
    "integration_scope.update": {"disabled_providers": ["slack"]},
    "integration.remove": {"provider": "slack"}, "integration.enable": {"provider": "slack"},
    "channel.upsert": {"channel_type": "webchat", "linked_service_key": "support"},
    "agent_binding.upsert": {"service_key": "support", "agent_id": "agent-1"},
    "agent_binding.remove": {"service_key": "support"}, "agent_mappings.replace": {"agent_mappings": []},
    "process.upsert": {"process_key": "triage", "description": "Review incoming questions"},
    "strategist.update": {"priors": ["Use approved Knowledge"]},
    "task.prepare": {"title": "Review the Knowledge index", "priority": 3},
}


class _RecordingApi:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, *, json_body=None, params=None, files=None, headers=None):
        self.calls.append((method, path, json_body, files is not None))
        return next((body for m, p, body in _CANNED if m == method and p == path), {})

    async def download(self, path, *, method="GET", json_body=None, **params):
        self.calls.append((method, path, json_body, False))
        if path.endswith("/export-blueprint-package"):
            from packages.core.blueprints.package import pack, pack_source
            return (pack_source if params.get("layout") == "source" else pack)(PAYLOAD, []), "application/gzip"
        return b"%PDF", "application/pdf"

    async def get(self, path, **params):
        return await self.request("GET", path, params=params)

    async def get_with_headers(self, path, **params):
        return await self.request("GET", path, params=params), {}

    async def post(self, path, body=None, **kwargs):
        return await self.request("POST", path, json_body=body, **kwargs)

    async def put(self, path, body=None):
        return await self.request("PUT", path, json_body=body)

    async def patch(self, path, body=None):
        return await self.request("PATCH", path, json_body=body)


@pytest.mark.asyncio
async def test_the_delivery_list_tells_an_agent_when_a_page_is_not_all_of_it():
    # Catches: a page reading as the whole list, or a limit silently doing nothing.
    from apps.api.mcp.registry import ToolContext, ToolError
    from apps.api.mcp.tools_delivery import list_client_deliveries

    class Paged(_RecordingApi):
        has_more = "true"

        async def get_with_headers(self, path, **params):
            self.calls.append(("GET", path, params, False))
            return [{"id": "a"}, {"id": "b"}], {"X-Has-More": self.has_more}

    api = Paged()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=None)
    assert await list_client_deliveries(ctx, {"limit": 2, "offset": 4}) == {
        "deliveries": [{"id": "a"}, {"id": "b"}], "has_more": True, "next_offset": 6,
    }
    assert api.calls[0][2] == {"limit": 2, "offset": 4}
    api.has_more = "false"
    assert (await list_client_deliveries(ctx, {}))["next_offset"] is None
    with pytest.raises(ToolError, match="limit and offset"):
        await list_client_deliveries(ctx, {"client_id": "client-1", "limit": 2})


def _samples(upload_id):
    """One or more representative calls per tool; every MCP tool must be listed."""
    ws = {"workspace_id": "ws-1"}
    return {
        **{tools_workspace.operation_tool_name(operation): [{**ws, "payload": payload}]
           for operation, payload in _OPERATION_SAMPLES.items()},
        "whoami": [{}],
        "list_workspaces": [{}],
        "create_workspace": [{"name": "Bakery", "description": "Orders and catering", "category": "food"}],
        "start_workspace_draft": [{"brief": "Take catering orders and plan the weekly menu.",
                                   "document_ids": ["01HZX3V5Q8N4ZK2M7P9R6T0ABC"], "wait_seconds": 5}],
        "continue_workspace_draft": [{"draft_id": "draft-1", "message": "Orders come in by email.",
                                      "wait_seconds": 5}],
        "get_workspace_draft": [{"draft_id": "draft-1"}, {"status": "ready"}],
        "list_workspace_recommendations": [{}],
        "update_workspace_draft_fields": [{"draft_id": "draft-1", "fields": {"heartbeat_enabled": False}}],
        "apply_blueprint_to_workspace_draft": [{"draft_id": "draft-1", "blueprint_id": "bp-1"}],
        "finalize_workspace_draft": [{"draft_id": "draft-1"}],
        "send_workspace_chat_message": [
            {**ws, "message": "What is waiting on me?", "wait_seconds": 0},
            {**ws, "message": "Retry this step.", "thread_ref_kind": "task", "thread_ref_id": "task-1",
             "wait_seconds": 0},
        ],
        "read_workspace_chat": [{**ws, "after_message_id": "msg-1", "limit": 20}],
        "list_conversations": [{"kind": "room", "limit": 20}, {}],
        "search_conversation_messages": [
            {"query": "renewal", "kind": "room", "conversation_id": "room-1", "sender_user_id": "user-1",
             "after": "2026-10-01T00:00:00Z", "cursor": "2026-11-01T00:00:00+00:00|msg-1"},
        ],
        "read_conversation_messages": [
            {"kind": "personal", "conversation_id": "conv-1", "around_message_id": "msg-1", "limit": 20},
        ],
        "get_workspace": [{**ws, "include": ["app_view", "agents", "operation_state"]}],
        "describe_workspace_operations": [{}],
        "stage_workspace_change": [
            {**ws, "patches": [{"op": "rule.add", "payload": {"description": "Cite evidence"}}]},
            {**ws, "draft_id": "draft-1", "patches": [{"op": "rule.add", "payload": {"description": "Confirm before publishing"}}]},
        ],
        "apply_workspace_change": [{**ws, "draft_id": "draft-1", "review_fingerprint": "a" * 64}],
        "discard_workspace_change": [{**ws, "draft_id": "draft-1"}],
        "get_workspace_app_audience": [ws],
        "design_workspace_app_view": [
            {**ws, "brief": "Plan a video series and make it episode by episode."},
            {**ws, "brief": "Same series studio.", "feedback": "Keep Remake off the tiles.", "draft_id": "draft-2",
             "audience": "The account owner: pending approvals and finished episodes first.", "wait_seconds": 30},
        ],
        "get_workspace_app_design": [{**ws, "design_id": "design-1"}],
        "test_workspace_app_view": [ws, {**ws, "draft_id": "draft-2"}],
        "upsert_workflow": [
            {"name": "Flow", "steps": _VALID_STEPS, "workspace_id": "ws-1", "trigger_type": "manual"},
            {"workflow_id": "wf-1", "name": "Flow 2", "steps": _VALID_STEPS, "expected_version": 3},
            {"name": "Revisable image", "variables": [{"key": "notes", "default": ""},
                {"key": "previous_artifact", "default": {}}], "steps": [
                {"id": "start", "type": "trigger", "next": ["make"], "config": {"production": {
                    "media": "image", "stages": [{"label": "Make", "steps": ["make"]}],
                    "revise": {"from_step": "make", "notes_input": "notes",
                               "context_inputs": {"previous_artifact": "made"}},
                }}},
                {"id": "make", "type": "code", "next": ["end"], "config": {
                    "language": "python", "code": "print('{}')", "output_format": "json", "output_var": "made"}},
                {"id": "end", "type": "end", "next": [], "config": {}},
            ]},
        ],
        "run_workflow": [{"workflow_id": "wf-1", "variables": {"topic": "x"}},
                         {"workflow_id": "wf-1", "wait": False}],
        "get_workflow_run": [{"run_id": "run-1"}],
        "retry_workflow_run": [
            {"run_id": "run-1"},
            {"run_id": "run-1", "supplied_output": {
                "step_id": "produce", "output": {"video_document_id": "doc-1"},
                "document_ids": ["doc-1"], "note": "Made in chat."}},
        ],
        "list_run_input_choices": [{"workflow_id": "wf-1", "input_key": "creators", **ws}],
        "upsert_skill": [
            {"name": "skill", "system_prompt": "Do it.", "workspace_id": "ws-1", "extra_files": {"a.md": "x"}},
            {"skill_id": "sk-1", "description": "Updated", "expected_edit_version": 2},
        ],
        "list_stats": [ws],
        "upsert_stat": [
            {**ws, "key": "weekly_new_guests", "name": "New guests", "collector_type": "agent_review",
             "collector_config": {"workflow_slug": "guest-metrics", "assessment": {
                 "subject": "workspace", "rubric": "Copy the saved count.", "scale": {"minimum": 0},
                 "missing_evidence": "unknown"}}},
            {**ws, "library_key": "workspace.tasks.completed"},
            {**ws, "stat_id": "stat-1", "status": "paused", "unit": "guests"},
        ],
        "add_assessed_metrics": [{**ws, "templates": ["followers"], "cadence": "weekly"}],
        "create_upload": [{"filename": "blueprint.json"}],
        "add_knowledge_document": [{**ws, "filename": "menu.md", "content": "# Menu"}],
        "list_knowledge": [ws],
        "search_knowledge": [{"query": "menu", **ws, "folder_id": "folder-1", "limit": 5},
                             {"query": "fighter", **ws, "kinds": ["audio"], "results_only": True}],
        "search_knowledge_content": [{"query": "annual plan price", **ws, "folder_id": "folder-1",
                                      "limit": 5}],
        "read_knowledge_document": [{"document_id": "doc-1"}, {"document_id": "doc-2"}],
        "list_tasks": [{**ws, "status": "blocked", "attention": True, "limit": 10}],
        "list_attention": [{**ws, "limit": 20}],
        "get_task": [{"task_id": "task-1", "log_limit": 5}],
        "list_scheduled_jobs": [{**ws, "status": "attention", "search": "digest"}],
        "get_scheduled_job": [{"job_id": "job-1"}],
        "upsert_scheduled_job": [
            {"name": "Weekday digest", **ws, "schedule_kind": "cron", "cron_expr": "0 9 * * 1-5",
             "timezone": "Europe/London", "execution_type": "workflow",
             "execution_target": {"workflow_id": "wf-1"}},
            {"job_id": "job-1", "schedule_kind": "every", "every_seconds": 3600},
        ],
        "toggle_scheduled_job": [{"job_id": "job-1", "enabled": False}],
        "run_scheduled_job": [{"job_id": "job-1", "idempotency_key": "retry-1"}],
        "export_blueprint": [ws, {**ws, "format": "package"}, {**ws, "format": "source", "filename": "studio"}],
        "configure_knowledge_starter": [
            {**ws, "document_id": "doc-1", "body_md": "ticket_id,status\n"},
            {**ws, "document_id": "doc-1", "body_md": None},
        ],
        "validate_blueprint": [{"upload_id": upload_id}],
        "install_blueprint": [
            {"upload_id": upload_id, "mode": "simulate", "workspace_name": "Copy"},
            {"source_workspace_id": "ws-1", "export_options": {"title": "Demo"}},
            {"upload_id": upload_id, "preflight_only": True},
        ],
        "compare_blueprints": [{"left": {"upload_id": upload_id}, "right": ws}],
        "promote_workspace": [{**ws, "preflight_only": True}, {**ws, "force": False}],
        "preview_workspace_upgrade": [ws, {**ws, "target_version": "1.2.0", "include_content": True}],
        "apply_workspace_upgrade": [{**ws, "target_version": "1.2.0", "expected_blueprint_fingerprint": "bp",
                                     "expected_plan_fingerprint": "plan",
                                     "configuration_resolutions": [{"key": "workflow:intake",
                                                                    "resolution": "use_blueprint"}]},
                                    {**ws, "recommended": True, "target_version": "1.2.0"}],
        "revert_workspace_upgrade": [ws],
        "publish_workspace_release": [
            {**ws, "upload_id": upload_id, "dry_run": True},
            {**ws, "upload_id": upload_id, "adopt": True, "release_notes": "Merged from staging"},
        ],
        "list_clients": [{"search": "bakery"}],
        "deliver_to_client": [{"client_id": "client-1", "contact_id": "contact-1", **ws,
                               "knowledge_document_ids": ["doc-1"]}],
        "list_client_deliveries": [{"client_id": "client-1"}, {}, {"limit": 50, "offset": 50}],
        "publish_delivery_update": [
            {"client_id": "client-1", "delivery_id": "delivery-1"},
            {"client_id": "client-1", "delivery_id": "delivery-1",
             "knowledge_document_ids": ["doc-1"], "release_notes": "New intake step"},
            {"client_id": "client-1", "delivery_id": "delivery-1", "dry_run": True},
            {"client_id": "client-1", "delivery_id": "delivery-1", "upload_id": upload_id,
             "dry_run": True, "release_notes": "Merged from staging"},
        ],
    }


async def test_every_mcp_tool_matches_a_live_route_and_its_body_model(monkeypatch):
    from fastapi.dependencies.utils import request_body_to_args
    from fastapi.routing import APIRoute
    from starlette.routing import Match

    from apps.api.main import app
    from apps.api.mcp.registry import ToolContext

    redis = _FakeRedis()

    async def fake_redis():
        return redis

    monkeypatch.setattr(transfers, "_redis", fake_redis)
    monkeypatch.setattr(tools_workspace, "_design_redis", fake_redis)
    await redis.set(tools_workspace._DESIGN_KEY + "design-1", json.dumps(
        {"user_id": USER_ID, "entity_id": ENTITY, "workspace_id": "ws-1", "status": "done", "draft_id": "draft-2"}))
    upload = await transfers.create(kind=transfers.UPLOAD, user_id=USER_ID, entity_id=ENTITY,
                                    filename="blueprint.json", content_type="application/json")
    await transfers.store_upload(upload, json.dumps(PAYLOAD).encode(), "application/json")

    samples = _samples(upload.id)
    assert set(samples) == set(mcp_routes.dispatcher.tools), "add a sample call for every MCP tool"

    api = _RecordingApi()
    request = type("Req", (), {"base_url": "http://manor.test/"})()
    ctx = ToolContext(api=api, claims={"sub": USER_ID, "entity_id": ENTITY}, request=request)
    for name, calls in samples.items():
        for arguments in calls:
            await mcp_routes.dispatcher.tools[name].handler(ctx, arguments)

    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    problems = []
    for method, path, body, multipart in api.calls:
        scope = {"type": "http", "method": method, "path": path, "root_path": ""}
        route = next((r for r in routes if r.matches(scope)[0] == Match.FULL), None)
        if route is None:
            problems.append(f"{method} {path}: no route")
            continue
        if multipart or not route.dependant.body_params:
            continue
        _, errors = await request_body_to_args(
            route.dependant.body_params, body if body is not None else {}, route._embed_body_fields,
        )
        if errors:
            problems.append(f"{method} {path}: {[(e.get('loc'), e.get('type')) for e in errors]}")
    assert not problems, "\n".join(problems)
    assert {path for _, path, _, _ in api.calls} >= {
        "/api/v1/blueprints/install-payload", "/api/v1/workspaces/ws-1/operation/drafts/draft-1/apply",
        "/api/v1/workspaces/ws-1/export-blueprint-package", "/api/v1/workspaces/ws-1/blueprint/upgrade/v3/preview",
        "/api/v1/clients/client-1/deliveries", "/api/v1/documents/upload",
        "/api/v1/documents/doc-1/content", "/api/v1/documents/doc-2/download",
        "/api/v1/workspaces/ws-1/documents", "/api/v1/jobs/job-1/run_now",
        "/api/v1/workspaces/ws-1/app-view/design", "/api/v1/workspaces",
        "/api/v1/workspace-drafts/draft-1/messages", "/api/v1/workspace-drafts/draft-1/finalize",
        "/api/v1/workspaces/ws-1/chat/messages", "/api/v1/workspaces/ws-1/chat/messages/page",
    }


