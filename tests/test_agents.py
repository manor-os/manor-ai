"""E2E tests: agents CRUD, marketplace, subscriptions, tool bindings."""

import pytest
from httpx import AsyncClient


async def _auth(client: AsyncClient, username: str = "agentuser") -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_create_agent(client: AsyncClient):
    headers = await _auth(client)
    resp = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Sales Bot",
            "description": "Handles sales inquiries",
            "system_prompt": "You are a sales assistant.",
            "category": "sales",
            "tags": ["sales", "crm"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Sales Bot"
    assert data["category"] == "sales"
    assert "sales" in data["tags"]
    assert data["source"] == "custom"


@pytest.mark.asyncio
async def test_list_agents(client: AsyncClient):
    headers = await _auth(client)
    await client.post("/api/v1/agents", headers=headers, json={"name": "Agent A"})
    await client.post("/api/v1/agents", headers=headers, json={"name": "Agent B"})

    resp = await client.get("/api/v1/agents", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_update_agent(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post("/api/v1/agents", headers=headers, json={"name": "Old Bot"})
    agent_id = create.json()["id"]

    resp = await client.put(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={
            "name": "New Bot",
            "system_prompt": "Updated prompt",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Bot"
    assert resp.json()["system_prompt"] == "Updated prompt"


@pytest.mark.asyncio
async def test_delete_agent(client: AsyncClient):
    headers = await _auth(client)
    create = await client.post("/api/v1/agents", headers=headers, json={"name": "ToDelete"})
    agent_id = create.json()["id"]

    resp = await client.delete(f"/api/v1/agents/{agent_id}", headers=headers)
    assert resp.status_code == 204

    resp2 = await client.get(f"/api/v1/agents/{agent_id}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_agent_isolation(client: AsyncClient):
    headers_a = await _auth(client, "agent_a")
    headers_b = await _auth(client, "agent_b")

    create = await client.post("/api/v1/agents", headers=headers_a, json={"name": "A's Bot"})
    agent_id = create.json()["id"]

    resp = await client.get(f"/api/v1/agents/{agent_id}", headers=headers_b)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_subscribe_to_agent(client: AsyncClient):
    headers = await _auth(client)
    # Create a template agent
    create = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Template Bot",
        },
    )
    agent_id = create.json()["id"]

    # Subscribe
    sub_resp = await client.post(
        "/api/v1/agents/subscriptions",
        headers=headers,
        json={
            "agent_id": agent_id,
            "custom_prompt": "Be extra helpful",
        },
    )
    assert sub_resp.status_code == 201
    sub = sub_resp.json()
    assert sub["agent_id"] == agent_id
    assert sub["custom_prompt"] == "Be extra helpful"

    # List subscriptions
    subs = await client.get("/api/v1/agents/subscriptions/mine", headers=headers)
    assert len(subs.json()) == 1

    # Unsubscribe
    unsub = await client.delete(f"/api/v1/agents/subscriptions/{sub['id']}", headers=headers)
    assert unsub.status_code == 204

    subs2 = await client.get("/api/v1/agents/subscriptions/mine", headers=headers)
    assert len(subs2.json()) == 0


@pytest.mark.asyncio
async def test_tool_bindings(client: AsyncClient):
    headers = await _auth(client)

    # Create agent
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "Tool Bot"})
    agent_id = agent.json()["id"]

    # Create tool definitions (normally these are seeded)
    from packages.core.services.agent_service import create_tool_definition
    from packages.core.database import async_session

    async with async_session() as db:
        t1 = await create_tool_definition(db, name="list_tasks", category="tasks")
        t2 = await create_tool_definition(db, name="send_email", category="comms")
        await db.commit()
        tool_ids = [t1.id, t2.id]

    # Bind tools
    bind_resp = await client.post(
        f"/api/v1/agents/{agent_id}/tools",
        headers=headers,
        json={
            "tool_ids": tool_ids,
        },
    )
    assert bind_resp.status_code == 200
    assert bind_resp.json()["bound"] == 2

    # List bound tools
    tools_resp = await client.get(f"/api/v1/agents/{agent_id}/tools", headers=headers)
    assert len(tools_resp.json()) == 2
    names = {t["name"] for t in tools_resp.json()}
    assert names == {"list_tasks", "send_email"}

    # Unbind one
    unbind_resp = await client.request(
        "DELETE",
        f"/api/v1/agents/{agent_id}/tools",
        headers=headers,
        json={
            "tool_ids": [tool_ids[0]],
        },
    )
    assert unbind_resp.json()["unbound"] == 1

    tools2 = await client.get(f"/api/v1/agents/{agent_id}/tools", headers=headers)
    assert len(tools2.json()) == 1
    assert tools2.json()[0]["name"] == "send_email"


@pytest.mark.asyncio
async def test_agent_tool_binding_syncs_mcp_binding_from_settings(client: AsyncClient):
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.base import generate_ulid
    from packages.core.models.mcp import AgentMCPBinding, MCPServer
    from packages.core.services.agent_service import create_tool_definition

    headers = await _auth(client, "agent_mcp_settings")
    agent = await client.post("/api/v1/agents", headers=headers, json={"name": "MCP Tool Bot"})
    agent_id = agent.json()["id"]

    async with async_session() as db:
        server = MCPServer(
            id=generate_ulid(),
            server_key="agent_settings_x",
            name="Agent Settings X",
            transport="builtin",
            auth_type="none",
            tools_cached={},
            default_config={},
            status="active",
        )
        db.add(server)
        tool = await create_tool_definition(
            db,
            name="mcp__agent_settings_x__create_post",
            display_name="Create Post",
            category="mcp",
        )
        await db.commit()
        server_id = server.id
        tool_id = tool.id

    bind_resp = await client.post(
        f"/api/v1/agents/{agent_id}/tools",
        headers=headers,
        json={"tool_ids": [tool_id]},
    )
    assert bind_resp.status_code == 200

    async with async_session() as db:
        binding = (
            await db.execute(
                select(AgentMCPBinding).where(
                    AgentMCPBinding.agent_id == agent_id,
                    AgentMCPBinding.mcp_server_id == server_id,
                )
            )
        ).scalar_one()
        assert binding.status == "active"
        assert binding.allowed_tools == ["create_post"]

    unbind_resp = await client.request(
        "DELETE",
        f"/api/v1/agents/{agent_id}/tools",
        headers=headers,
        json={"tool_ids": [tool_id]},
    )
    assert unbind_resp.status_code == 200

    async with async_session() as db:
        binding = (
            await db.execute(
                select(AgentMCPBinding).where(
                    AgentMCPBinding.agent_id == agent_id,
                    AgentMCPBinding.mcp_server_id == server_id,
                )
            )
        ).scalar_one()
        assert binding.status == "inactive"
        assert binding.allowed_tools == []


@pytest.mark.asyncio
async def test_all_tools_for_create_backfills_runtime_catalog(client: AsyncClient):
    headers = await _auth(client, "agent_tools_catalog")

    resp = await client.get("/api/v1/agents/tools/all", headers=headers)
    assert resp.status_code == 200
    tools = resp.json()
    assert len(tools) >= 10
    names = {tool["name"] for tool in tools}
    assert {"search_tools", "manor"} <= names

    # A second read should be idempotent, not duplicate rows.
    again = await client.get("/api/v1/agents/tools/all", headers=headers)
    assert again.status_code == 200
    assert len(again.json()) == len(tools)


async def _create_mapped_agent_runtime(
    client: AsyncClient, headers: dict, name: str = "Runtime Agent"
) -> tuple[str, str, str]:
    """Create a workspace service and map an agent to it."""
    workspace = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={
            "name": f"{name} Workspace",
        },
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    agent = await client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": name,
            "description": "Runtime managed agent",
            "system_prompt": "Use the assigned workspace context and tools.",
        },
    )
    assert agent.status_code == 201, agent.text
    agent_id = agent.json()["id"]

    service = await client.post(
        f"/api/v1/workspaces/{workspace_id}/services",
        headers=headers,
        json={
            "key": "leasing_consultant",
            "name": "Leasing Consultant",
            "description": "Handle leasing requests.",
        },
    )
    assert service.status_code == 200, service.text

    mapped = await client.post(
        f"/api/v1/workspaces/{workspace_id}/agents",
        headers=headers,
        json={
            "service_key": "leasing_consultant",
            "agent_id": agent_id,
            "custom_prompt": "Follow this workspace's leasing SOP.",
        },
    )
    assert mapped.status_code == 200, mapped.text
    return workspace_id, agent_id, "leasing_consultant"


async def _register_test_worker(client: AsyncClient, headers: dict, name: str = "Hermes Runtime") -> str:
    resp = await client.post(
        "/api/v1/workers/register",
        headers=headers,
        json={
            "kind": "custom_http",
            "display_name": name,
            "description": "External agent worker used by subscription bindings.",
            "version": "test-1",
            "capabilities": {
                "supported_kinds": ["subagent", "llm", "action"],
                "supported_providers": None,
                "max_concurrent_leases": 2,
                "max_risk_level": "medium",
                "uses_manor_credentials": False,
                "deployment": "remote",
                "protocol_version": 1,
            },
            "trust_level": "standard",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["worker_id"]


def test_worker_capabilities_accept_runtime_capability_filter() -> None:
    from apps.api.routers.workers import WorkerCapabilities
    from packages.worker_sdk.types import HeartbeatCapacity, HeartbeatRequest

    capabilities = WorkerCapabilities(
        supported_kinds=["action"],
        supported_capabilities=["external.social"],
    )
    heartbeat = HeartbeatRequest(
        capacity=HeartbeatCapacity(can_accept_leases=0),
        capabilities={"supported_capabilities": ["external.social"]},
    )

    assert capabilities.model_dump()["supported_capabilities"] == ["external.social"]
    assert heartbeat.model_dump()["capabilities"] == {"supported_capabilities": ["external.social"]}


@pytest.mark.asyncio
async def test_python_worker_reports_runtime_capabilities_on_heartbeat() -> None:
    from datetime import datetime, timezone

    from packages.worker_sdk.types import HeartbeatResponse
    from packages.worker_sdk.worker import ManorWorker

    class FakeClient:
        def __init__(self) -> None:
            self.requests = []

        async def heartbeat(self, req):
            self.requests.append(req)
            return HeartbeatResponse(
                server_time=datetime.now(timezone.utc),
                next_heartbeat_in_seconds=1,
                new_leases=[],
                instructions=[],
            )

    client = FakeClient()
    worker = ManorWorker(
        endpoint="http://test.local",
        worker_id="wkr_1",
        secret="wks_1",
        client=client,
        capabilities={"supported_capabilities": ["external.social"]},
    )

    await worker._tick()
    assert client.requests[-1].capabilities == {"supported_capabilities": ["external.social"]}

    worker.update_capabilities({"supported_capabilities": ["workspace.task"]})
    await worker._tick()
    assert client.requests[-1].capabilities == {"supported_capabilities": ["workspace.task"]}


@pytest.mark.asyncio
async def test_agent_deployments_manage_subscription_worker_bindings(client: AsyncClient, db_session):
    from sqlalchemy import select
    from packages.core.models.workspace import AgentSubscription

    headers = await _auth(client, "agent_runtime_bind")
    workspace_id, agent_id, service_key = await _create_mapped_agent_runtime(client, headers)

    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.agent_id == agent_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()

    worker_id = await _register_test_worker(client, headers)
    bind = await client.post(
        f"/api/v1/agents/subscriptions/{sub.id}/workers",
        headers=headers,
        json={"worker_id": worker_id, "priority": 25, "is_preferred": True},
    )
    assert bind.status_code == 201, bind.text
    assert bind.json()["worker_id"] == worker_id
    assert bind.json()["is_preferred"] is True
    assert bind.json()["worker"]["kind"] == "custom_http"

    listed = await client.get(f"/api/v1/agents/subscriptions/{sub.id}/workers", headers=headers)
    assert listed.status_code == 200
    assert [row["worker_id"] for row in listed.json()] == [worker_id]

    deployments = await client.get(f"/api/v1/agents/{agent_id}/deployments", headers=headers)
    assert deployments.status_code == 200
    body = deployments.json()
    assert len(body) == 1
    assert body[0]["id"] == sub.id
    assert body[0]["workspace_id"] == workspace_id
    assert body[0]["service_key"] == service_key
    assert body[0]["workers"][0]["worker_id"] == worker_id

    unbind = await client.delete(
        f"/api/v1/agents/subscriptions/{sub.id}/workers/{worker_id}",
        headers=headers,
    )
    assert unbind.status_code == 204
    listed_after = await client.get(f"/api/v1/agents/subscriptions/{sub.id}/workers", headers=headers)
    assert listed_after.status_code == 200
    assert listed_after.json() == []


@pytest.mark.asyncio
async def test_subscription_worker_bindings_do_not_expose_another_users_cli_worker(
    client: AsyncClient,
    db_session,
):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from packages.core.models.base import generate_ulid
    from packages.core.models.user import User, UserMembership
    from packages.core.models.worker import SubscriptionWorker, Worker
    from packages.core.models.workspace import AgentSubscription
    from packages.core.services.auth_service import create_access_token, hash_password

    owner_headers = await _auth(client, "agent_cli_owner")
    owner_me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    workspace_id, agent_id, service_key = await _create_mapped_agent_runtime(client, owner_headers)
    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.agent_id == agent_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()

    other_user = User(
        id=generate_ulid(),
        entity_id=owner_me["entity_id"],
        email="agent_cli_other@example.com",
        display_name="Agent CLI Other",
        password_hash=hash_password("pass123"),
        role="member",
        status="active",
    )
    other_worker_display_name = "Other User Local Worker"
    other_worker = Worker(
        id="worker_agent_other_cli",
        entity_id=owner_me["entity_id"],
        kind="custom_http",
        display_name=other_worker_display_name,
        version="test",
        status="active",
        created_by_user_id=other_user.id,
        last_heartbeat_at=datetime.now(timezone.utc),
        capabilities={"supported_kinds": ["action"], "supported_providers": ["chrome"]},
    )
    db_session.add_all(
        [
            other_user,
            UserMembership(
                id=generate_ulid(),
                user_id=other_user.id,
                entity_id=owner_me["entity_id"],
                role="member",
                status="active",
                is_primary=True,
            ),
            other_worker,
        ]
    )
    await db_session.commit()

    denied_bind = await client.post(
        f"/api/v1/agents/subscriptions/{sub.id}/workers",
        headers=owner_headers,
        json={"worker_id": other_worker.id, "priority": 25, "is_preferred": True},
    )
    assert denied_bind.status_code == 404

    db_session.add(
        SubscriptionWorker(
            subscription_id=sub.id,
            worker_id=other_worker.id,
            priority=25,
            is_preferred=True,
        )
    )
    await db_session.commit()

    listed = await client.get(f"/api/v1/agents/subscriptions/{sub.id}/workers", headers=owner_headers)
    assert listed.status_code == 200
    assert listed.json() == []

    deployments = await client.get(f"/api/v1/agents/{agent_id}/deployments", headers=owner_headers)
    assert deployments.status_code == 200
    assert deployments.json()[0]["workers"] == []

    denied_unbind = await client.delete(
        f"/api/v1/agents/subscriptions/{sub.id}/workers/{other_worker.id}",
        headers=owner_headers,
    )
    assert denied_unbind.status_code == 404

    other_token = create_access_token(other_user.id, owner_me["entity_id"], "member")
    other_headers = {"Authorization": f"Bearer {other_token}"}
    other_listed = await client.get(f"/api/v1/agents/subscriptions/{sub.id}/workers", headers=other_headers)
    assert other_listed.status_code == 200
    assert [row["worker_id"] for row in other_listed.json()] == [other_worker.id]


@pytest.mark.asyncio
async def test_worker_lease_includes_agent_runtime_context(client: AsyncClient, db_session):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from apps.api.routers.workers import _serialize_lease_for_worker
    from packages.core.models.base import generate_ulid
    from packages.core.models.execution import ExecutionPlan, ExecutionStep
    from packages.core.models.worker import WorkLease
    from packages.core.models.workspace import AgentSubscription, AgentToolBinding
    from packages.core.services.agent_service import create_tool_definition

    headers = await _auth(client, "agent_runtime_lease")
    workspace_id, agent_id, service_key = await _create_mapped_agent_runtime(
        client,
        headers,
        name="Lease Context Agent",
    )
    sub = (
        await db_session.execute(
            select(AgentSubscription).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.agent_id == agent_id,
                AgentSubscription.service_key == service_key,
                AgentSubscription.status == "active",
            )
        )
    ).scalar_one()
    worker_id = await _register_test_worker(client, headers, name="OpenClaw Runtime")

    tool = await create_tool_definition(
        db_session,
        name="runtime_test_workspace_search",
        display_name="Runtime Workspace Search",
        description="Search workspace-scoped context.",
        category="workspace",
    )
    db_session.add(AgentToolBinding(agent_id=agent_id, tool_id=tool.id))

    plan = ExecutionPlan(
        id=generate_ulid(),
        entity_id=sub.entity_id,
        workspace_id=workspace_id,
        agent_subscription_id=sub.id,
        plan_dag={},
        status="running",
        execution_mode="live",
    )
    step = ExecutionStep(
        id=generate_ulid(),
        plan_id=plan.id,
        entity_id=sub.entity_id,
        workspace_id=workspace_id,
        step_key="draft_followup",
        kind="subagent",
        service_key=service_key,
        capability_id="workspace.search",
        resolved_subscription_id=sub.id,
        resolved_agent_id=agent_id,
        params={"lead": "Maya Chen"},
        expected_output_schema={"type": "object"},
        depends_on=[],
        step_status="running",
        risk_level="low",
    )
    lease = WorkLease(
        id=generate_ulid(),
        step_id=step.id,
        plan_id=plan.id,
        entity_id=sub.entity_id,
        workspace_id=workspace_id,
        worker_id=worker_id,
        subscription_id=sub.id,
        status="active",
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add_all([plan, step, lease])
    await db_session.commit()

    dto = await _serialize_lease_for_worker(db_session, lease, step)

    assert dto.subscription_id == sub.id
    assert dto.service_key == service_key
    assert dto.capability_id == "workspace.search"
    assert dto.agent is not None
    assert dto.agent["id"] == agent_id
    assert dto.agent["subscription_id"] == sub.id
    assert dto.agent["service_key"] == service_key
    assert dto.agent["custom_prompt"] == "Follow this workspace's leasing SOP."
    assert any(tool_info["name"] == "runtime_test_workspace_search" for tool_info in dto.bindings["tools"])
