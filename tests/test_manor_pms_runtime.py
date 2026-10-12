"""PMS registration and actor-scoped API → vault → runtime → HTTP contract.

The integration test uses the suite's isolated test database. Only the PMS HTTP
transport and background health queue are replaced; account authorization,
credential encryption/lease, builtin dispatch and provider validation are real.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet

from packages.core.ai.mcp import BUILTIN_MCP_MODULES, manor_pms
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.tool_pool import ToolPool
from packages.core.credentials import CredentialService
from packages.core.credentials.dev_provider import DevKeyProvider
from tests.auth_helpers import register_user_and_get_token


@pytest.fixture(scope="module")
def pms_pool():
    pool = ToolPool()
    pool.initialize()
    return pool


def test_pms_catalog_pool_and_public_schema_are_consistent(pms_pool):
    assert BUILTIN_MCP_MODULES["manor_pms"] is manor_pms
    catalog = manor_pms.list_tools()
    assert set(pms_pool.registered_tool_names(prefix="mcp__manor_pms__")) == {
        f"mcp__manor_pms__{tool['name']}" for tool in catalog
    }
    assert len(catalog) == 227
    for tool in catalog:
        schema = pms_pool.get_schema(f"mcp__manor_pms__{tool['name']}")
        parameters = schema["function"]["parameters"]
        properties = parameters["properties"]
        assert "integration_account_id" in properties
        assert ("integration_account_selection" in properties) is tool["annotations"]["readOnlyHint"]
        for key in ("integration_account_id", "integration_account_selection"):
            properties.pop(key, None)
        assert parameters == tool["inputSchema"]
        assert {"entity_id", "user_id", "task_id", "bearer_token", "base_url"}.isdisjoint(properties)


def test_pms_approval_classification_covers_every_actual_operation():
    effects = Counter()
    for tool in manor_pms.list_tools():
        result = classify_runtime_tool(f"mcp__manor_pms__{tool['name']}")
        expected = "read_only" if tool["annotations"]["readOnlyHint"] else "action"
        assert result.effect.value == expected, tool["name"]
        effects[result.effect.value] += 1
        if expected == "action":
            assert result.action is not None
        else:
            assert result.action is None
    assert effects == {"read_only": 100, "action": 127}


@pytest.mark.parametrize("action", ["get_unknown", "list_unknown", "pms_list_properties", "delete_everything"])
def test_unknown_pms_operation_does_not_bypass_approval_by_read_prefix(action, pms_pool):
    name = f"mcp__manor_pms__{action}"
    assert pms_pool.get_schema(name) is None
    result = classify_runtime_tool(name)
    assert result.effect.value == "action"
    assert result.action is not None


def test_pms_skill_tools_match_the_executable_contract():
    skill_dir = Path(manor_pms.__file__).parent.parent / "skills" / "mcp_manor_pms"
    config = json.loads((skill_dir / "config.json").read_text())
    assert len(config["tools"]) == len(set(config["tools"]))
    assert set(config["tools"]) == {
        f"mcp__manor_pms__{tool['name']}" for tool in manor_pms.list_tools()
    }


def test_pms_management_catalog_never_grants_public_webchat_authority():
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.policies import check_runtime_tool_policy
    from packages.core.ai.runtime.principals import RuntimePrincipal, RuntimePrincipalKind
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.surfaces import ChatSurface

    names = tuple(f"mcp__manor_pms__{tool['name']}" for tool in manor_pms.list_tools())
    envelope = RuntimeEnvelope(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        profile=RuntimeProfile.EXTERNAL_CUSTOMER_SAFE,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.EXTERNAL_CONTACT),
        allowed_tool_names=names,
    )
    for name in names:
        decision = check_runtime_tool_policy(envelope=envelope, tool_name=name)
        assert not decision.allowed, name
        assert decision.code == "external_principal_tool_denied", name


class _AuditRecorder:
    def __init__(self):
        self.events = []

    def log(self, event):
        self.events.append(event)


async def _register(client, username):
    response = await register_user_and_get_token(client, json={
        "username": username,
        "email": f"{username}@test.com",
        "password": "TestPassword123!",
        "entity_name": f"{username} Corp",
    })
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    return headers, response.json()["user_id"], me.json()["entity_id"]


@pytest.mark.asyncio
async def test_api_connection_vault_account_authorization_and_real_dispatch(client, monkeypatch, pms_pool):
    import packages.core.credentials as credential_module
    import packages.core.database as db_module
    from packages.core.governance.approvals import grant_approval
    from tests.approval_settings_test_utils import action_rule, set_personal_test_approval
    from packages.core.models.document import Integration
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation, Task
    from packages.core.models.user import User
    from packages.core.services import dashboard_http, integration_service
    from packages.core.tasks.channel_tasks import health_check_task

    audit = _AuditRecorder()
    vault = CredentialService(DevKeyProvider(key=Fernet.generate_key().decode(), audit_sink=audit), audit_sink=audit)
    monkeypatch.setattr(credential_module, "get_credential_service", lambda: vault)
    monkeypatch.setattr(integration_service, "get_credential_service", lambda: vault)
    enqueue = Mock()
    monkeypatch.setattr(health_check_task, "delay", enqueue)
    monkeypatch.delenv("PMS_API_URL", raising=False)
    requests = []
    contexts = []
    targets = []

    async def resolve_host(hostname, port=443):
        assert hostname in {"pms.runtime.example", "pms.selected.example"}
        return ("93.184.216.34",)

    async def pms_http(request):
        requests.append(request)
        assert request.url.host in {"pms.runtime.example", "pms.selected.example"}
        expected_token = "owner-management-token" if request.url.host == "pms.runtime.example" else "selected-management-token"
        assert request.headers["authorization"] == f"Bearer {expected_token}"
        if request.url.path == "/api/auth/verify":
            assert request.method == "GET"
            return httpx.Response(200, json={"valid": True, "type": "management", "entity_id": -12345})
        assert request.method == "POST"
        assert request.url.path == "/api/mcp/call"
        contexts.append(dict(manor_pms._call_context.get()))
        payload = json.loads(request.content)
        assert {"entity_id", "user_id", "access_token", "integration_account_id"}.isdisjoint(payload["arguments"])
        return httpx.Response(200, json={"success": True, "data": {"dispatched": payload, "host": request.url.host}})

    def pinned_transport(target):
        targets.append(target)
        return httpx.MockTransport(pms_http)

    monkeypatch.setattr(dashboard_http, "_resolve_public_host", resolve_host)
    monkeypatch.setattr(manor_pms, "create_public_https_transport", pinned_transport)
    owner_headers, owner_id, entity_id = await _register(client, "pms_runtime_owner")
    accounts = []
    for host, token in [("pms.runtime.example", "owner-management-token"), ("pms.selected.example", "selected-management-token")]:
        response = await client.post("/api/v1/integrations", headers=owner_headers, json={
            "provider": "manor_pms", "config": {},
            "credentials": {"access_token": token, "base_url": f"https://{host}"},
        })
        assert response.status_code == 201, response.text
        assert token not in response.text
        accounts.append(response.json()["id"])
    assert len(enqueue.call_args_list) == 2

    async with db_module.async_session() as db:
        for account_id in accounts:
            row = await db.get(Integration, account_id)
            assert row.owner_user_id == row.created_by_user_id == owner_id
            assert row.entity_id == entity_id
            assert row.credential_scheme == "dev_fernet"
            assert row.credentials == {}
            assert row.credential_ref and "management-token" not in row.credential_ref
        # Even an admin in the same tenant must not silently borrow the owner's
        # unshared PMS account. The real account registry decides this below.
        teammate = User(entity_id=entity_id, email="pms_runtime_admin@test.com", password_hash="unused-test-account", role="admin", status="active")
        db.add(teammate)
        await db.flush()
        teammate_id = teammate.id
        conversation = Conversation(entity_id=entity_id, user_id=owner_id, title="PMS task review")
        db.add(conversation)
        await db.flush()
        conversation_id = conversation.id
        task = Task(entity_id=entity_id, conversation_id=conversation_id, title="PMS cleaning transition", creator_id=owner_id)
        db.add(task)
        await db.flush()
        runtime_task_id = task.id
        await db.commit()

    health = await client.post(f"/api/v1/integrations/health-check/entity-accounts/{accounts[0]}", headers=owner_headers)
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True
    assert health.json()["latency_ms"] >= 0
    assert "management-token" not in health.text and "-12345" not in health.text
    assert len(requests) == 1
    assert contexts == []  # Connection testing reads no business data.

    result = json.loads(await pms_pool.execute(
        "mcp__manor_pms__get_property", {"property_id": 42, "integration_account_id": accounts[0]},
        entity_id=entity_id, user_id=owner_id,
    ))
    assert result["dispatched"] == {"name": "get_property", "arguments": {"property_id": 42}}
    assert result["host"] == "pms.runtime.example"
    assert contexts[-1]["entity_id"] == entity_id
    assert contexts[-1]["user_id"] == owner_id
    assert contexts[-1]["integration_account_id"] == accounts[0]
    assert manor_pms._call_context.get() == {}

    selected = json.loads(await pms_pool.execute(
        "mcp__manor_pms__list_properties", {"integration_account_id": accounts[1]},
        entity_id=entity_id, user_id=owner_id,
    ))
    assert selected["host"] == "pms.selected.example"
    assert contexts[-1]["integration_account_id"] == accounts[1]
    leases = [e for e in audit.events if e.reason == "mcp_builtin._resolve_bearer_token:manor_pms"]
    assert len(leases) == 2
    assert all(e.requester_kind == "agent" and e.requester_id == entity_id for e in leases)
    assert len({e.credential_ref for e in leases}) == 2

    foreign_headers, foreign_id, foreign_entity = await _register(client, "pms_runtime_foreign")
    request_count, lease_count = len(requests), len(audit.events)
    for actor, tenant in [(teammate_id, entity_id), (foreign_id, foreign_entity)]:
        denied = json.loads(await pms_pool.execute(
            "mcp__manor_pms__get_property", {"property_id": 42, "integration_account_id": accounts[0]},
            entity_id=tenant, user_id=actor,
        ))
        assert denied["error"] == "credentials_unavailable"
    foreign_health = await client.post(f"/api/v1/integrations/health-check/entity-accounts/{accounts[0]}", headers=foreign_headers)
    assert foreign_health.status_code == 404
    assert len(requests) == request_count
    assert len(audit.events) == lease_count

    # A real write approval must stop before credential lease and PMS HTTP.
    # Grant that concrete request, then retry through the same runtime path.
    cleaning_arguments = {"cleaning_task_id": 73, "action": "start", "integration_account_id": accounts[0],
        "_runtime_run_id_from_context": "pms-test", "_runtime_tool_call_id_from_context": "clean-chat"}
    blocked = json.loads(await pms_pool.execute(
        "mcp__manor_pms__cleaning_transition_task", cleaning_arguments,
        entity_id=entity_id, user_id=owner_id, conversation_id=conversation_id,
    ))
    assert blocked.get("__hitl__") is True, blocked
    assert blocked["error"] == "approval_required"
    assert len(requests) == request_count
    assert len(audit.events) == lease_count
    async with db_module.async_session() as db:
        approval = await db.get(HitlRequest, blocked["approval_token"])
        assert approval.action_key == "manor_pms.cleaning_transition_task"
        await grant_approval(db, approval, by_user_id=owner_id, via="chat_card")
        await db.commit()
    cleaned = json.loads(await pms_pool.execute(
        "mcp__manor_pms__cleaning_transition_task",
        {**cleaning_arguments, "approval_token": blocked["approval_token"]},
        entity_id=entity_id, user_id=owner_id, conversation_id=conversation_id,
    ))
    assert cleaned.get("dispatched") == {"name": "cleaning_transition_task", "arguments": {"task_id": 73, "action": "start"}}, cleaned
    assert manor_pms._call_context.get() == {}

    # The user's standing approval lets a Task call execute without creating
    # a suspended Task-runner checkpoint. Its runtime Task ID must remain
    # context only, while the business ID is mapped to PMS task_id.
    async with db_module.async_session() as db:
        await set_personal_test_approval(
            db, entity_id=entity_id, actor_id=owner_id,
            user_rules=[action_rule("manor_pms.cleaning_transition_task")],
        )
        await db.commit()
    task_cleaned = json.loads(await pms_pool.execute(
        "mcp__manor_pms__cleaning_transition_task", {**cleaning_arguments,
            "_runtime_tool_call_id_from_context": "clean-task"},
        entity_id=entity_id, user_id=owner_id, task_id=runtime_task_id, conversation_id=conversation_id,
    ))
    assert task_cleaned.get("dispatched") == cleaned["dispatched"], task_cleaned
    assert contexts[-1]["task_id"] == runtime_task_id
    assert manor_pms._call_context.get() == {}
    assert len(targets) == len(requests)

    # The newly added tour operation respects its configured durable approval
    # gate, even after another action received a standing approval.
    async with db_module.async_session() as db:
        await set_personal_test_approval(
            db, entity_id=entity_id, actor_id=owner_id,
            user_rules=[action_rule("manor_pms.cleaning_transition_task"),
                        action_rule("manor_pms.create_showing", effect="ask")],
        )
        await db.commit()
    showing_arguments = {
        "property_id": 42, "lead_id": 19, "showing_date": "2026-10-01",
        "start_time": "10:00:00", "integration_account_id": accounts[0],
        "_runtime_run_id_from_context": "pms-test", "_runtime_tool_call_id_from_context": "showing",
    }
    request_count, lease_count = len(requests), len(audit.events)
    showing_blocked = json.loads(await pms_pool.execute(
        "mcp__manor_pms__create_showing", showing_arguments,
        entity_id=entity_id, user_id=owner_id, conversation_id=conversation_id,
    ))
    assert showing_blocked.get("__hitl__") is True, showing_blocked
    assert showing_blocked["error"] == "approval_required"
    assert len(requests) == request_count
    assert len(audit.events) == lease_count
    async with db_module.async_session() as db:
        approval = await db.get(HitlRequest, showing_blocked["approval_token"])
        assert approval.action_key == "manor_pms.create_showing"
        await grant_approval(db, approval, by_user_id=owner_id, via="chat_card")
        await db.commit()
    showing = json.loads(await pms_pool.execute(
        "mcp__manor_pms__create_showing",
        {**showing_arguments, "approval_token": showing_blocked["approval_token"]},
        entity_id=entity_id, user_id=owner_id, conversation_id=conversation_id,
    ))
    assert showing.get("dispatched") == {
        "name": "create_showing",
        "arguments": {key: value for key, value in showing_arguments.items()
                      if key not in {"integration_account_id", "_runtime_run_id_from_context", "_runtime_tool_call_id_from_context"}},
    }, showing
    assert len(requests) == request_count + 1
    replay = json.loads(await pms_pool.execute(
        "mcp__manor_pms__create_showing",
        {**showing_arguments, "approval_token": showing_blocked["approval_token"]},
        entity_id=entity_id, user_id=owner_id, conversation_id=conversation_id,
    ))
    assert replay.get("error"), replay
    assert len(requests) == request_count + 1
