"""Lark wire contracts, credential isolation and error boundaries."""
from __future__ import annotations

import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from packages.core.ai.mcp import lark
from packages.core.services.integration_health import run_test

_CREDS = {"app_id": "cli_test", "app_secret": "test-secret", "platform": "lark"}
_HTTP_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def clear_token_cache():
    lark._TOKEN_CACHE.clear()
    yield
    lark._TOKEN_CACHE.clear()


def _transport(monkeypatch, handler):
    monkeypatch.setattr(lark.httpx, "AsyncClient", lambda **kwargs: _HTTP_CLIENT(
        **kwargs, transport=httpx.MockTransport(handler),
    ))


def _text(result):
    return result["content"][0]["text"]


@pytest.mark.parametrize(("name", "args", "method", "path", "params", "body"), [
    ("list_chats", {"page_token": "next"}, "GET", "/im/v1/chats",
     {"page_size": "20", "page_token": "next"}, None),
    ("get_chat", {"chat_id": "oc_chat"}, "GET", "/im/v1/chats/oc_chat", {}, None),
    ("list_messages", {"chat_id": "oc_chat", "start_time": "12", "sort_type": "ByCreateTimeDesc"},
     "GET", "/im/v1/messages", {"page_size": "20", "container_id_type": "chat",
     "container_id": "oc_chat", "start_time": "12", "sort_type": "ByCreateTimeDesc"}, None),
    ("list_messages", {"thread_id": "omt_thread"}, "GET", "/im/v1/messages",
     {"page_size": "20", "container_id_type": "thread", "container_id": "omt_thread"}, None),
    ("send_message", {"chat_id": "oc_chat", "text": "你好\nLark", "uuid": "stable"},
     "POST", "/im/v1/messages", {"receive_id_type": "chat_id"},
     {"receive_id": "oc_chat", "msg_type": "text", "content": '{"text": "你好\\nLark"}', "uuid": "stable"}),
    ("reply_message", {"message_id": "om_message", "text": "reply", "reply_in_thread": True},
     "POST", "/im/v1/messages/om_message/reply", {},
     {"msg_type": "text", "content": '{"text": "reply"}', "reply_in_thread": True}),
    ("get_document", {"document_id": "doc_token"}, "GET", "/docx/v1/documents/doc_token", {}, None),
    ("get_document_content", {"document_id": "doc_token"}, "GET",
     "/docx/v1/documents/doc_token/raw_content", {}, None),
    ("create_document", {"title": "Test", "folder_token": "folder"}, "POST", "/docx/v1/documents",
     {}, {"title": "Test", "folder_token": "folder"}),
    ("append_document_text", {"document_id": "doc_token", "paragraphs": ["Hello", "世界"],
     "client_token": "stable"}, "POST", "/docx/v1/documents/doc_token/blocks/doc_token/children",
     {"document_revision_id": "-1", "client_token": "stable"}, {"index": -1, "children": [
         {"block_type": 2, "text": {"elements": [{"text_run": {"content": "Hello"}}]}},
         {"block_type": 2, "text": {"elements": [{"text_run": {"content": "世界"}}]}},
     ]}),
    ("list_bitable_tables", {"app_token": "base_token"}, "GET",
     "/bitable/v1/apps/base_token/tables", {"page_size": "20"}, None),
    ("list_bitable_fields", {"app_token": "base_token", "table_id": "tbl_table"}, "GET",
     "/bitable/v1/apps/base_token/tables/tbl_table/fields", {"page_size": "20"}, None),
    ("list_bitable_records", {"app_token": "base_token", "table_id": "tbl_table", "page_size": 2,
     "field_names": ["Name"], "filter": {"conjunction": "and", "conditions": [
         {"field_name": "Name", "operator": "is", "value": ["Alice"]}]}},
     "POST", "/bitable/v1/apps/base_token/tables/tbl_table/records/search", {"page_size": "2"},
     {"field_names": ["Name"], "filter": {"conjunction": "and", "conditions": [
         {"field_name": "Name", "operator": "is", "value": ["Alice"]}]}}),
    ("create_bitable_record", {"app_token": "base_token", "table_id": "tbl_table", "fields": {"Name": "Alice"}},
     "POST", "/bitable/v1/apps/base_token/tables/tbl_table/records", {}, {"fields": {"Name": "Alice"}}),
    ("update_bitable_record", {"app_token": "base_token", "table_id": "tbl_table", "record_id": "rec_record",
     "fields": {"Status": "Done"}}, "PUT", "/bitable/v1/apps/base_token/tables/tbl_table/records/rec_record",
     {}, {"fields": {"Status": "Done"}}),
])
@pytest.mark.parametrize("identity", ["app", "user"])
async def test_tool_uses_official_wire_contract(monkeypatch, name, args, method, path, params, body, identity):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            assert json.loads(request.content) == {"app_id": "cli_test", "app_secret": "test-secret"}
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-test"})
        assert request.url.host == "open.larksuite.com"
        assert request.method == method
        assert request.url.path == "/open-apis" + path
        assert dict(request.url.params) == params
        assert (json.loads(request.content) if request.content else None) == body
        assert request.headers["Authorization"] == ("Bearer u-test" if identity == "user" else "Bearer t-test")
        return httpx.Response(200, json={"code": 0, "data": {"content": "document text", "page_token": "cursor"}})

    _transport(monkeypatch, handler)
    creds = _CREDS if identity == "app" else {
        "access_token": "u-test", "token_type": "user_access_token", "platform": "lark",
    }
    result = await lark.call_tool(name, args, json.dumps(creds))
    assert result["isError"] is False, _text(result)
    assert len(calls) == (1 if identity == "user" else 2)
    assert "test-secret" not in _text(result)
    assert "t-test" not in _text(result)
    assert "u-test" not in _text(result)


@pytest.mark.parametrize("overrides", [
    {"access_token": "", "token_type": "user_access_token"},
    {"access_token": "user-token", "token_type": "Bearer"},
    {"access_token": "invalid\r\ntoken", "token_type": "user_access_token"},
    {"access_token": "user-token", "token_type": "user_access_token", "platform": "feishu"},
])
async def test_invalid_user_bundle_never_falls_back_to_app_identity(monkeypatch, overrides):
    def reject_io(_request):
        raise AssertionError("Invalid OAuth bundles must not call an app token endpoint")

    _transport(monkeypatch, reject_io)
    result = await lark.call_tool("list_chats", {}, json.dumps({**_CREDS, **overrides}))
    assert result["isError"] is True


@pytest.mark.parametrize("identity", ["ou_expected", "ou_other", None])
async def test_user_health_checks_identity_without_tenant_token_or_writes(monkeypatch, identity):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://open.larksuite.com/open-apis/authen/v1/user_info"
        assert request.headers["Authorization"] == "Bearer user-token"
        return httpx.Response(200, json={"code": 0, "data": {"open_id": identity}})

    _transport(monkeypatch, respond)
    result = await run_test("lark", {
        "access_token": "user-token", "token_type": "user_access_token", "platform": "lark",
        "open_id": "ou_expected",
    })
    assert result["ok"] is (identity == "ou_expected")
    assert len(calls) == 1
    assert "user-token" not in result["detail"]


async def test_revoked_user_token_requires_reconnect_without_app_fallback(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path == "/open-apis/im/v1/chats"
        return httpx.Response(401, json={"code": 99991668, "msg": "user-token"})

    _transport(monkeypatch, respond)
    result = await lark.call_tool("list_chats", {}, json.dumps({
        "access_token": "user-token", "token_type": "user_access_token", "platform": "lark",
    }))
    assert result["isError"] is True
    assert "reconnect" in _text(result)
    assert "user-token" not in _text(result)
    assert len(calls) == 1


@pytest.mark.parametrize(("name", "args"), [
    ("send_message", {"chat_id": "oc_chat"}),
    ("send_message", {"chat_id": "../escape", "text": "secret-message"}),
    ("list_chats", {"page_size": 0}),
    ("list_messages", {"chat_id": "oc_chat", "page_size": 51}),
    ("list_messages", {}),
    ("list_messages", {"chat_id": "oc_chat", "thread_id": "omt_thread"}),
    ("list_chats", {"page_size": True}),
    ("list_chats", []),
    ("append_document_text", {"document_id": "doc", "paragraphs": ["too many"] * 51}),
    ("get_document_content", {"document_id": "doc", "content_offset": 20}),
    ("list_chats", {"result_offset": 20}),
    ("update_bitable_record", {"app_token": "base", "table_id": "table", "record_id": "rec", "fields": {}}),
])
async def test_invalid_arguments_fail_before_network(monkeypatch, name, args):
    def handler(request):
        pytest.fail("invalid arguments must not authenticate or execute")

    _transport(monkeypatch, handler)
    result = await lark.call_tool(name, args, json.dumps(_CREDS))
    assert result["isError"] is True
    assert "secret-message" not in _text(result)


@pytest.mark.parametrize("credentials", ["broken-json", "null", "[]", '{}',
    json.dumps({**_CREDS, "platform": "https://untrusted.test"})])
async def test_invalid_credentials_cannot_select_arbitrary_host(monkeypatch, credentials):
    _transport(monkeypatch, lambda request: pytest.fail("invalid credentials must not reach the network"))
    result = await lark.call_tool("list_chats", {}, credentials)
    assert result["isError"] is True


async def test_cache_is_scoped_to_platform_app_and_secret_and_health_is_fresh(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    _transport(monkeypatch, handler)
    for creds in (_CREDS, _CREDS, {**_CREDS, "app_secret": "rotated"}, {**_CREDS, "platform": "feishu"}):
        result = await lark.call_tool("list_chats", {}, json.dumps(creds))
        assert not result["isError"]
    assert sum(r.url.path.endswith("/internal") for r in requests) == 3
    assert requests[-1].url.host == "open.feishu.cn"
    health = await run_test("lark", _CREDS)
    assert health["ok"] is True
    assert "resource permissions" in health["detail"]
    assert sum(r.url.path.endswith("/internal") for r in requests) == 4
    assert "t-token" not in json.dumps(health)


@pytest.mark.parametrize(("code", "status", "reason"), [
    (10014, 200, "app_unavailable"),
    (10015, 200, "credentials_rejected"),
    (10003, 200, "provider_unavailable"),
    (99991543, 400, "credentials_rejected"),
    (99991672, 200, "permission_denied"),
    (99991400, 429, "rate_limited"),
    (99991663, 200, "token_rejected"),
    (99991665, 200, "token_rejected"),
    (42, 502, "provider_unavailable"),
])
async def test_provider_errors_are_actionable_without_echoing_secrets(monkeypatch, code, status, reason):
    _transport(monkeypatch, lambda request: httpx.Response(status, json={
        "code": code, "msg": "echo test-secret t-token", "tenant_access_token": "t-token", "expire": 7200,
    }))
    health = await run_test("lark", _CREDS)
    assert health["ok"] is False
    assert health["reason_code"] == reason
    assert "test-secret" not in json.dumps(health)
    assert "t-token" not in json.dumps(health)


async def test_write_timeout_does_not_replay_and_expired_token_is_invalidated(monkeypatch):
    writes = []

    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        writes.append(request)
        raise httpx.ReadTimeout("potentially committed write", request=request)

    _transport(monkeypatch, handler)
    result = await lark.call_tool("send_message", {"chat_id": "oc_chat", "text": "hello"}, json.dumps(_CREDS))
    assert result["isError"] is True
    assert len(writes) == 1
    assert "check the destination" in _text(result)
    _transport(monkeypatch, lambda request: httpx.Response(200, json={"code": 99991663}))
    result = await lark.call_tool("list_chats", {}, json.dumps(_CREDS))
    assert result["isError"] is True
    assert not lark._TOKEN_CACHE


@pytest.mark.parametrize("character", ["中", "\n", '"', "\x00"])
async def test_long_document_is_readable_in_chunks(monkeypatch, character):
    content = character * 9000

    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        return httpx.Response(200, json={"code": 0, "data": {"content": content}})

    _transport(monkeypatch, handler)
    parts = []
    offset, digest = 0, None
    while True:
        args = {"document_id": "doc", "content_offset": offset}
        if digest:
            args["content_digest"] = digest
        result = await lark.call_tool("get_document_content", args, json.dumps(_CREDS))
        assert len(_text(result)) <= lark._RESULT_MAX_CHARS
        chunk = json.loads(_text(result))
        digest = chunk["content_digest"]
        parts.append(chunk["content"])
        if not chunk["has_more"]:
            break
        assert chunk["next_offset"] > offset
        offset = chunk["next_offset"]
    assert len(parts) > 1
    assert "".join(parts) == content


def test_all_tool_schemas_are_valid():
    tools = lark.list_tools()
    assert len({tool["name"] for tool in tools}) == len(tools)
    for tool in tools:
        Draft202012Validator.check_schema(tool["inputSchema"])


@pytest.mark.parametrize("name,args", [
    ("list_messages", {"chat_id": "oc_chat"}),
    ("list_bitable_records", {"app_token": "base", "table_id": "table"}),
])
async def test_large_read_preserves_json_items_and_native_pagination(monkeypatch, name, args):
    data = {"items": [{"content": "quoted \"content\"\n" * 2000}], "page_token": "next-provider-page", "has_more": True}

    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        assert "result_offset" not in request.url.params
        assert "result_offset" not in (json.loads(request.content) if request.content else {})
        return httpx.Response(200, json={"code": 0, "data": data})

    _transport(monkeypatch, handler)
    offset, chunks, digest = 0, [], None
    while True:
        query = {**args, "result_offset": offset}
        if digest:
            query["result_digest"] = digest
        result = await lark.call_tool(name, query, json.dumps(_CREDS))
        assert result["isError"] is False
        assert len(_text(result)) <= lark._RESULT_MAX_CHARS
        chunk = json.loads(_text(result))
        assert chunk["page_token"] == "next-provider-page"
        assert chunk["provider_has_more"] is True
        chunks.append(chunk["result_json"])
        digest = chunk["result_digest"]
        next_offset = chunk["result_pagination"]["next_offset"]
        if next_offset is None:
            break
        assert next_offset > offset
        offset = next_offset
    assert json.loads("".join(chunks)) == data


async def test_live_result_changes_cannot_silently_corrupt_continuation(monkeypatch):
    data = {"items": [{"content": "first response" * 2000}], "page_token": "next", "has_more": True}

    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        return httpx.Response(200, json={"code": 0, "data": data})

    _transport(monkeypatch, handler)
    first = json.loads(_text(await lark.call_tool("list_chats", {}, json.dumps(_CREDS))))
    data["items"].insert(0, {"content": "new item"})
    result = await lark.call_tool("list_chats", {
        "result_offset": first["result_pagination"]["next_offset"], "result_digest": first["result_digest"],
    }, json.dumps(_CREDS))
    assert result["isError"] is True
    assert "changed between chunks" in _text(result)


async def test_document_edits_cannot_silently_corrupt_content_chunks(monkeypatch):
    data = {"content": "old document" * 2000}

    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        return httpx.Response(200, json={"code": 0, "data": data})

    _transport(monkeypatch, handler)
    first = json.loads(_text(await lark.call_tool("get_document_content", {"document_id": "doc"}, json.dumps(_CREDS))))
    data["content"] = "new paragraph" + data["content"]
    result = await lark.call_tool("get_document_content", {
        "document_id": "doc", "content_offset": first["next_offset"], "content_digest": first["content_digest"],
    }, json.dumps(_CREDS))
    assert result["isError"] is True
    assert "changed between chunks" in _text(result)


@pytest.mark.parametrize("name,args,data,key,expected", [
    ("send_message", {"chat_id": "oc_chat", "text": "hello"},
     {"body": {"content": "long response" * 5000}, "message_id": "om_created"}, "message_id", "om_created"),
    ("create_bitable_record", {"app_token": "base", "table_id": "table", "fields": {"Name": "Alice"}},
     {"record": {"fields": {"Long text": "long response" * 5000}, "record_id": "rec_created"}}, "record_id", "rec_created"),
    ("append_document_text", {"document_id": "doc", "paragraphs": ["hello"]},
     {"children": [{"block_id": "block", "text": "long response" * 5000}], "document_revision_id": 2},
     "block_ids", ["block"]),
])
async def test_large_successful_writes_keep_completion_ids_without_replaying(monkeypatch, name, args, data, key, expected):
    writes = []

    def handler(request):
        if request.url.path.endswith("/internal"):
            return httpx.Response(200, json={"code": 0, "expire": 7200, "tenant_access_token": "t-token"})
        writes.append(request)
        return httpx.Response(200, json={"code": 0, "data": data})

    _transport(monkeypatch, handler)
    result = await lark.call_tool(name, args, json.dumps(_CREDS))
    assert result["isError"] is False
    assert len(writes) == 1
    assert len(_text(result)) <= lark._RESULT_MAX_CHARS
    assert json.loads(_text(result))[key] == expected
