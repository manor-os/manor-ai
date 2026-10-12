"""PMS provider contract: authenticated requests, scoped inputs and failures."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from packages.core.ai.mcp import manor_pms
from packages.core.services import dashboard_http


@pytest.fixture(autouse=True)
def actor_context(monkeypatch):
    monkeypatch.delenv("PMS_API_URL", raising=False)
    manor_pms.set_call_context({"user_id": "user-1", "entity_id": "entity-1"})
    yield
    manor_pms.clear_call_context()


@pytest.fixture
def mock_http(monkeypatch):
    requests = []
    targets = []
    dns_hosts = []
    state = {"status": 200, "body": {"success": True, "data": {"id": 42}}}

    async def handler(request):
        requests.append(request)
        if state.get("exception"):
            raise state["exception"]
        await asyncio.sleep(0)
        if "text" in state:
            return httpx.Response(state["status"], text=state["text"])
        return httpx.Response(state["status"], json=state["body"], headers=state.get("headers"))

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client_factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        kwargs["transport"] = kwargs.get("transport") or transport
        return original_client(**kwargs)

    def pinned_transport(target):
        targets.append(target)
        return transport

    async def resolve_host(hostname, port=443):
        dns_hosts.append((hostname, port))
        return ("93.184.216.34",)

    monkeypatch.setattr(manor_pms.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(manor_pms, "create_public_https_transport", pinned_transport)
    monkeypatch.setattr(dashboard_http, "_resolve_public_host", resolve_host)
    return state, requests, targets, dns_hosts


def error(result):
    assert result["isError"] is True
    return json.loads(result["content"][0]["text"])


def test_catalog_matches_shipped_ltr_str_capabilities_and_effects():
    tools = {tool["name"]: tool for tool in manor_pms.list_tools()}
    assert len(tools) == 227
    writes = {
        "create_property", "create_tenant", "create_owner", "create_vendor",
        "create_lease", "create_maintenance_request", "create_payment",
        "reservation_create", "reservation_transition", "guest_create",
        "pricing_set_override", "ancillary_add_item", "cleaning_transition_task",
        "damage_file_claim", "compliance_record_consent", "channel_import_ical",
        "review_reply",
    }
    actual_writes = {name for name, tool in tools.items() if not tool["annotations"]["readOnlyHint"]}
    assert writes <= actual_writes
    assert len(actual_writes) == 127
    methods = json.loads(Path(manor_pms.__file__).with_name("manor_pms_tools.json").read_text())["operation_methods"]
    assert set(methods) == set(tools)
    for name, method in methods.items():
        assert tools[name]["annotations"]["readOnlyHint"] is (method == "GET" or name == "promo_validate"), name
        if method == "DELETE":
            assert tools[name]["annotations"]["destructiveHint"] is True, name
    assert tools["promo_validate"]["annotations"]["readOnlyHint"] is True
    assert tools["reservation_transition"]["annotations"]["destructiveHint"] is True
    assert not any(name.startswith("pms_") for name in tools)
    cleaning = tools["cleaning_transition_task"]["inputSchema"]
    assert cleaning["required"] == ["cleaning_task_id", "action"]
    assert "task_id" not in cleaning["properties"]
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools.values())
    tools["get_property"]["inputSchema"]["required"].clear()
    assert next(t for t in manor_pms.list_tools() if t["name"] == "get_property")["inputSchema"]["required"] == ["property_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("list_units", {"available_only": True, "property_id": 7}),
    ("list_available_units", {"property_id": 7}),
    ("create_lead", {"property_id": 7, "first_name": "Test", "last_name": "Applicant", "email": "test@example.invalid", "phone": "+12025550111"}),
    ("create_showing", {"property_id": 7, "lead_id": 9, "showing_date": "2026-10-01", "start_time": "10:00:00"}),
    ("update_showing", {"showing_id": 11, "status": "cancelled"}),
    ("get_maintenance_request", {"request_id": 12}),
    ("update_maintenance_request", {"request_id": 12, "status": "in_progress", "assigned_to": 3}),
    ("delete_showing", {"showing_id": 11}),
    ("policy_cancellation_quote", {"reservation_id": 14}),
    ("policy_modification_quote", {"reservation_id": 14}),
])
async def test_leasing_and_maintenance_operations_forward_validated_business_arguments(name, arguments, mock_http):
    _, requests, _, _ = mock_http
    result = await manor_pms.call_tool(name, arguments, "management-jwt")
    assert result["isError"] is False
    assert json.loads(requests[0].content) == {"name": name, "arguments": arguments}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("cleaning_get_task", {}),
    ("cleaning_update_task", {"priority": "high"}),
    ("cleaning_assign_task", {"assigned_to_user_id": 91}),
    ("cleaning_list_task_items", {}),
    ("cleaning_verify_task", {"score": 95, "passed": True}),
])
async def test_cleaning_record_and_assignee_aliases_do_not_replace_runtime_identity(name, arguments, mock_http):
    _, requests, _, _ = mock_http
    manor_pms.set_call_context({"user_id": "manor-user", "entity_id": "manor-entity", "task_id": "manor-task"})
    result = await manor_pms.call_tool(name, {"cleaning_task_id": 42, **arguments}, "management-jwt")
    assert result["isError"] is False
    sent = json.loads(requests[0].content)["arguments"]
    assert sent["task_id"] == 42
    assert "cleaning_task_id" not in sent
    if name == "cleaning_assign_task":
        assert sent["user_id"] == 91
        assert "assigned_to_user_id" not in sent
    else:
        assert "user_id" not in sent
    assert manor_pms._call_context.get()["user_id"] == "manor-user"
    assert manor_pms._call_context.get()["task_id"] == "manor-task"


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("list_units", {"available_only": True, "entity_id": 99}),
    ("create_showing", {"property_id": 7, "lead_id": 9, "showing_date": "2026-02-30", "start_time": "10:00:00"}),
    ("cleaning_assign_task", {"cleaning_task_id": 42, "user_id": 91}),
    ("get_maintenance_request", {"request_id": 0}),
    ("create_lead", {"property_id": 7, "first_name": "Test", "last_name": "Applicant", "email": "test@example.invalid", "phone": "+12025550111", "created_by": 99}),
])
async def test_new_operations_reject_invalid_or_identity_override_arguments_before_http(name, arguments, mock_http):
    _, requests, _, _ = mock_http
    assert error(await manor_pms.call_tool(name, arguments, "management-jwt"))["code"] == "invalid_arguments"
    assert requests == []


@pytest.mark.asyncio
async def test_plain_management_token_is_forwarded_without_actor_overrides(mock_http):
    _, requests, targets, _ = mock_http
    result = await manor_pms.call_tool("list_properties", {"limit": 5}, "management-jwt")
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == {"id": 42}
    request = requests[0]
    assert str(request.url) == "https://portfolio.manorai.xyz/api/mcp/call"
    assert request.headers["authorization"] == "Bearer management-jwt"
    assert "x-entity-id" not in request.headers
    assert "x-api-key" not in request.headers
    assert json.loads(request.content) == {"name": "list_properties", "arguments": {"limit": 5}}
    assert not targets


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["https://pms.example", "https://pms.example/api", "https://pms.example/api/mcp/"])
async def test_connection_url_is_normalized_and_dns_pinned(base_url, mock_http):
    _, requests, targets, dns_hosts = mock_http
    result = await manor_pms.call_tool("get_property", {"property_id": 7}, json.dumps({"access_token": "actor-token", "base_url": base_url}))
    assert result["isError"] is False
    assert str(requests[0].url) == "https://pms.example/api/mcp/call"
    assert requests[0].headers["authorization"] == "Bearer actor-token"
    assert targets[0].addresses == ("93.184.216.34",)
    assert dns_hosts == [("pms.example", 443)]


@pytest.mark.asyncio
async def test_trusted_deployment_endpoint_can_address_self_hosted_pms(monkeypatch, mock_http):
    _, requests, targets, _ = mock_http
    monkeypatch.setenv("PMS_API_URL", "http://pms-backend:8001/api")
    result = await manor_pms.call_tool("list_tenants", {}, "management-jwt")
    assert result["isError"] is False
    assert str(requests[0].url) == "http://pms-backend:8001/api/mcp/call"
    assert targets == []


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", [
    "http://pms.example", "https://127.0.0.1", "https://169.254.169.254",
    "https://localhost", "https://pms.internal", "https://user:secret@pms.example",
    "https://pms.example?token=secret", "https://pms.example#fragment",
    "file:///etc/passwd", "https://pms.example:999999",
])
async def test_unsafe_connection_endpoints_fail_before_http(base_url, mock_http):
    _, requests, _, _ = mock_http
    result = await manor_pms.call_tool("list_properties", {}, json.dumps({"access_token": "secret", "base_url": base_url}))
    assert error(result)["code"] == "invalid_connection"
    assert requests == []


@pytest.mark.asyncio
async def test_private_dns_answer_is_rejected(monkeypatch, mock_http):
    _, requests, _, _ = mock_http

    async def reject_private(*args, **kwargs):
        raise dashboard_http.DashboardHttpPolicyError("private network")

    monkeypatch.setattr(dashboard_http, "_resolve_public_host", reject_private)
    result = await manor_pms.call_tool("list_properties", {}, json.dumps({"access_token": "secret", "base_url": "https://pms.example"}))
    assert error(result)["code"] == "invalid_connection"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "arguments"), [
    ("pms_list_properties", {}), ("get_property", {}),
    ("get_property", {"property_id": "7"}),
    ("list_properties", {"entity_id": "other"}),
    ("list_properties", {"user_id": "other"}),
    ("list_properties", {"access_token": "secret"}),
    ("list_properties", {"bearer_token": "secret"}),
    ("list_properties", {"base_url": "https://evil.example"}),
    ("list_properties", {"_entity_id_from_context": "other"}),
    ("cleaning_transition_task", {"task_id": 7, "action": "complete"}),
    ("cleaning_transition_task", {"cleaning_task_id": 7, "task_id": 8, "action": "complete"}),
    ("reservation_transition", {"reservation_id": 1, "action": "delete"}),
    ("pricing_quote", {"unit_id": 1, "check_in": "bad-date", "check_out": "2026-10-03"}),
    ("list_properties", []),
])
async def test_unknown_invalid_and_scope_override_arguments_make_no_request(name, arguments, mock_http):
    _, requests, _, _ = mock_http
    result = await manor_pms.call_tool(name, arguments, "secret")
    assert error(result)["code"] in {"invalid_arguments", "unknown_tool"}
    assert "secret" not in json.dumps(result)
    assert requests == []


@pytest.mark.asyncio
async def test_cleaning_task_alias_preserves_business_id_and_input(mock_http):
    _, requests, _, _ = mock_http
    manor_pms.set_call_context({"user_id": "user-1", "entity_id": "entity-1", "task_id": "manor-task"})
    arguments = {"cleaning_task_id": 77, "action": "complete"}
    result = await manor_pms.call_tool("cleaning_transition_task", arguments, "management-jwt")
    assert result["isError"] is False
    assert json.loads(requests[0].content)["arguments"] == {"task_id": 77, "action": "complete"}
    assert arguments == {"cleaning_task_id": 77, "action": "complete"}


@pytest.mark.asyncio
@pytest.mark.parametrize("context", [{}, {"user_id": "user"}, {"entity_id": "entity"}])
async def test_missing_actor_fails_closed(context, mock_http):
    _, requests, _, _ = mock_http
    manor_pms.set_call_context(context)
    assert error(await manor_pms.call_tool("list_properties", {}, "token"))["code"] == "missing_actor"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials", ["", "{}", '{"api_key":"shared-service-key"}', "{broken", "[]", '{"access_token":12}', "bad\r\nheader"])
async def test_credentials_require_management_token(credentials, mock_http, monkeypatch):
    _, requests, _, _ = mock_http
    monkeypatch.setenv("PMS_API_KEY", "shared-service-key")
    result = await manor_pms.call_tool("list_properties", {}, credentials)
    assert error(result)["code"] in {"missing_credentials", "invalid_credentials", "invalid_connection"}
    assert "shared-service-key" not in json.dumps(result)
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [(401, "unauthorized"), (403, "forbidden"), (404, "http_error"), (429, "http_error"), (500, "http_error"), (302, "redirect")])
async def test_http_failure_and_redirect_are_not_retried_or_exposed(status, code, mock_http):
    state, requests, _, _ = mock_http
    state.update(status=status, body={"detail": "Bearer secret-token"}, headers={"Location": "https://other.example/steal"})
    result = await manor_pms.call_tool("guest_create", {"first_name": "Test"}, "secret-token")
    assert error(result) == {"message": error(result)["message"], "code": code, "status_code": status}
    assert "secret-token" not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"success": False, "error": "Bearer secret-token", "code": "upstream_error", "status_code": 422},
    {"success": True, "error": "failed"},
    {"success": True, "status_code": 500},
    {"success": True, "status": "error"},
    {"error": {"message": "Bearer secret-token"}},
    {}, [],
])
async def test_provider_failure_envelopes_are_errors_even_over_http_200(body, mock_http):
    state, requests, _, _ = mock_http
    state["body"] = body
    result = await manor_pms.call_tool("list_properties", {}, "secret-token")
    assert error(result)["code"] in {"upstream_error", "provider_error", "invalid_response"}
    assert "secret-token" not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_invalid_json_and_success_token_echo_do_not_leak(mock_http):
    state, _, _, _ = mock_http
    state["text"] = "not JSON: secret-token"
    assert error(await manor_pms.call_tool("list_properties", {}, "secret-token"))["code"] == "invalid_response"
    state.pop("text")
    state["body"] = {"success": True, "data": {"debug": "secret-token"}}
    result = await manor_pms.call_tool("list_properties", {}, "secret-token")
    assert result["isError"] is False
    assert "secret-token" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [httpx.ReadTimeout("secret-token"), httpx.ConnectError("secret-token")])
async def test_uncertain_write_failure_is_not_retried(exception, mock_http):
    state, requests, _, _ = mock_http
    state["exception"] = exception
    result = await manor_pms.call_tool("guest_create", {"first_name": "Test"}, "secret-token")
    assert "unknown" in error(result)["message"]
    assert "secret-token" not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_concurrent_account_calls_keep_endpoint_and_token_paired(mock_http):
    _, requests, _, _ = mock_http

    async def call(account):
        manor_pms.set_call_context({"user_id": account, "entity_id": account, "base_url": f"https://{account}.example"})
        try:
            return await manor_pms.call_tool("list_properties", {}, f"token-{account}")
        finally:
            manor_pms.clear_call_context()

    results = await asyncio.gather(call("first"), call("second"))
    assert all(result["isError"] is False for result in results)
    assert {(request.url.host, request.headers["authorization"]) for request in requests} == {
        ("first.example", "Bearer token-first"), ("second.example", "Bearer token-second"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_id", ["01MANORENTITY", 123, -123])
async def test_connection_probe_requires_no_actor_and_reads_only_auth_verify(entity_id, mock_http):
    state, requests, targets, _ = mock_http
    manor_pms.clear_call_context()
    state["body"] = {"valid": True, "type": "management", "entity_id": entity_id, "user": {"email": "private@example.com"}}
    result = await manor_pms.check_connection({"access_token": "secret-token", "base_url": "https://pms.example/api/mcp"})
    assert result == {"ok": True, "detail": "PMS management connection verified."}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://pms.example/api/auth/verify"
    assert requests[0].headers["authorization"] == "Bearer secret-token"
    assert targets[0].hostname == "pms.example"
    assert "private@example.com" not in json.dumps(result)
    assert "secret-token" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(("body", "reason_code"), [
    ({"valid": False}, "credentials_rejected"),
    ({"valid": True, "type": "tenant", "entity_id": 1}, "management_required"),
    ({"valid": True, "type": "owner", "entity_id": 1}, "management_required"),
    ({"valid": True, "entity_id": 1}, "management_required"),
    ({"valid": True, "type": "management"}, "missing_entity_scope"),
    ({"valid": True, "type": "management", "entity_id": " "}, "missing_entity_scope"),
    ({"valid": True, "type": "management", "entity_id": 0}, "missing_entity_scope"),
    ({"valid": True, "type": "management", "entity_id": True}, "missing_entity_scope"),
    ([], "credentials_rejected"),
])
async def test_connection_probe_rejects_invalid_or_nonmanagement_identity(body, reason_code, mock_http):
    state, requests, _, _ = mock_http
    state["body"] = body
    result = await manor_pms.check_connection({"access_token": "secret-token"})
    assert result["ok"] is False
    assert result["reason_code"] == reason_code
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_connection_probe_rejected_credentials_have_no_fallback(status, mock_http):
    state, requests, _, _ = mock_http
    state.update(status=status, text="Bearer secret-token")
    result = await manor_pms.check_connection({"access_token": "secret-token"})
    assert result["reason_code"] == "credentials_rejected"
    assert len(requests) == 1
    assert "secret-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_connection_probe_reuses_custom_endpoint_restrictions(mock_http):
    _, requests, _, _ = mock_http
    result = await manor_pms.check_connection({"access_token": "secret-token", "base_url": "http://127.0.0.1:8000"})
    assert result["reason_code"] == "invalid_connection"
    assert requests == []
