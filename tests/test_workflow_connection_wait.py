"""A Workflow run that reaches an unconnected integration waits for it.

The step pauses instead of failing, the Workspace chat gets one connect card,
and once the account is connected the run continues from that same step.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from packages.core.models.base import generate_ulid
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.task import Message
from packages.core.models.user import OAuthAccount, User
from packages.core.models.workflow import WorkflowRun
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from test_workflows import _auth


async def _unconnected_send_run(client, db_session, *, username: str, label: str):
    """A started run whose send step's Agent needs ``label``, which nobody connected."""
    headers = await _auth(client, username)
    workspace_id = (await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Replies"},
    )).json()["id"]
    workspace = await db_session.get(Workspace, workspace_id)
    user = (await db_session.execute(select(User).where(User.email == f"{username}@test.com"))).scalar_one()
    server = MCPServer(
        server_key=f"wait_{generate_ulid().lower()}", name=label,
        transport="builtin", auth_type="oauth2", status="active",
    )
    agent = Agent(entity_id=workspace.entity_id, name=f"{label} agent")
    db_session.add_all([server, agent])
    await db_session.flush()
    db_session.add_all([
        AgentSubscription(entity_id=workspace.entity_id, workspace_id=workspace_id,
                          agent_id=agent.id, service_key="replies"),
        AgentMCPBinding(agent_id=agent.id, mcp_server_id=server.id),
    ])
    await db_session.commit()

    steps = [
        {"id": "trigger", "type": "trigger", "config": {}, "next": ["draft"]},
        {"id": "draft", "type": "transform", "config": {"set": {"draft": "ok"}}, "next": ["send"]},
        {"id": "send", "type": "agent", "name": "Send reply",
         "config": {"agent_id": agent.id, "prompt": "Send the approved reply"}, "next": ["end"]},
        {"id": "end", "type": "end", "config": {}, "next": []},
    ]
    workflow = (await client.post(
        "/api/v1/workflows", headers=headers, json={"name": "Reply approval", "steps": steps},
    )).json()
    binding = (await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"], "workspace_id": workspace_id,
        "trigger_type": "manual", "name": "Reply approval",
    })).json()
    started = await client.post(
        f"/api/v1/workflows/bindings/{binding['id']}/run", headers=headers, json={"execute": False},
    )
    assert started.status_code == 201, started.text
    return headers, workspace_id, user, server, steps, started.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Gmail", "WordPress"])
async def test_run_waits_for_a_missing_connection_and_continues_from_its_step(
    client, db_session, monkeypatch, label,
):
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session

    headers, workspace_id, user, server, steps, run_id = await _unconnected_send_run(
        client, db_session, username=f"wfconnwait{label.lower()}", label=label,
    )

    agent_calls: list[str] = []

    async def agent_step(self, step, *_args, **_kwargs):
        agent_calls.append(step["id"])
        async with async_session() as db:
            connected = (await db.execute(select(OAuthAccount.id).where(
                OAuthAccount.user_id == user.id, OAuthAccount.provider == server.server_key,
            ))).first()
        if connected:
            return {"status": "completed", "output": {"sent": True}}
        return {"status": "failed", "error": f"{label} is not connected, so nothing was sent."}

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", agent_step)
    resumes: list[tuple] = []
    monkeypatch.setattr(WorkflowRunner, "enqueue_resume", staticmethod(
        lambda run_id, delay, continuation_token=None: resumes.append((run_id, continuation_token)) or True
    ))

    async def current_run() -> WorkflowRun:
        async with async_session() as db:
            return await db.get(WorkflowRun, run_id)

    async def connect_cards() -> list[Message]:
        async with async_session() as db:
            return [row for row in (await db.execute(select(Message))).scalars().all()
                    if (row.pending_action or {}).get("waiting_workflow_run_id") == run_id]

    # A missing connection cannot authorize replay of an attempt that already
    # acted outward, including older results that only recorded the receipt.
    from packages.core.services.workflow_connection_wait import connection_wait_for_failed_step

    untouched = await current_run()
    for safety in ({"do_not_replay": True}, {"effect_state": "confirmed"}, {"effect_state": "unconfirmed"}):
        assert await connection_wait_for_failed_step(
            db_session, run=untouched, step=steps[2],
            result={"status": "failed", "error": f"Connect {label}.", **safety},
        ) is None
    assert untouched.continuation_token is None
    assert await connect_cards() == []

    # 1. It stops at the send step and waits for the connection, not failed.
    await WorkflowRunner(session_factory=async_session).run(run_id)
    run = await current_run()
    assert run.status == "paused" and run.current_step_id == "send", run.error
    wait = run.step_results["send"]
    assert (wait["wait_type"], wait["connection_provider"], wait["resume_strategy"]) == (
        "connection", server.server_key, "reexecute",
    )
    assert run.step_results["draft"]["status"] == "completed"
    assert run.continuation_token and run.continuation_due_at > datetime.now(timezone.utc)
    [card] = await connect_cards()
    assert card.pending_action["kind"] == "needs_login"
    assert card.pending_action["connection_request"] is True
    assert card.pending_action["provider_key"] == server.server_key
    assert card.pending_action["recommendation"]["option"] == "sign_in"

    # The run page answers it by connecting: no raw link, no failure.
    detail = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=headers)).json()
    assert detail["resume_action"]["connection_provider"] == server.server_key
    assert detail["resume_action"]["connection_label"] == wait["connection_label"]
    compact = (await client.get(f"/api/v1/workflows/runs/{run_id}?detail=false", headers=headers)).json()
    assert compact["intervention"]["kind"] == "workflow_resume"
    assert compact["intervention"]["connection_provider"] == server.server_key
    assert "](" not in str(compact["intervention"]["observed_problem"])

    # 2. A recheck while still unconnected keeps waiting without rerunning the Agent or asking again.
    async with async_session() as db:
        await db.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(
            continuation_due_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ))
        await db.commit()
    assert await WorkflowRunner.resume(
        run_id, continuation_token=run.continuation_token, session_factory=async_session,
    ) == "resumed"
    run = await current_run()
    assert run.status == "paused" and run.step_results["send"]["connection_rechecks"] == 1
    assert agent_calls == ["send"]
    assert len(await connect_cards()) == 1

    # 3. Connected, the card's "continue" resumes the run from the send step.
    async with async_session() as db:
        db.add(OAuthAccount(user_id=user.id, provider=server.server_key, provider_user_id="owner@example.test",
                            access_token="fixture-token-not-used-for-provider-calls"))
        await db.commit()
    resolved = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{card.id}/resolve",
        headers=headers, json={"choice": "continue_after_login"},
    )
    assert resolved.status_code == 200, resolved.text
    assert [item[0] for item in resumes] == [run_id]
    # Nothing is handed to the AI to redo: the run itself continues.
    async with async_session() as db:
        assert not [row for row in (await db.execute(select(Message))).scalars().all()
                    if row.author_kind == "user" and wait["connection_label"] in (row.content or "")]
    assert await WorkflowRunner.resume(
        run_id, continuation_token=resumes[0][1], session_factory=async_session,
    ) == "resumed"
    run = await current_run()
    assert run.status == "completed", run.error
    assert run.step_results["send"]["output"] == {"sent": True}
    assert run.step_results["draft"]["status"] == "completed"
    assert agent_calls == ["send", "send"]


@pytest.mark.asyncio
async def test_step_that_stopped_for_a_person_is_not_told_to_connect_an_unused_account(
    client, db_session, monkeypatch,
):
    """Staging 2026-11-08: a step that stopped for real product photos was shown
    "Connect Facebook" because its Agent also had Facebook unconnected."""
    from packages.core.ai.workflow_runner import WorkflowRunner
    from packages.core.database import async_session
    from packages.core.services.workflow_step_needs_person import step_needs_person_result

    headers, _workspace_id, _user, _server, _steps, run_id = await _unconnected_send_run(
        client, db_session, username="wfconnwaitperson", label="Facebook",
    )
    request = {
        "missing": "The product knowledge has no real photos of the candles, so no post can show them.",
        "ask": "Add real product photos to the product knowledge, then retry.",
    }

    async def agent_step(self, step, *_args, **_kwargs):
        return step_needs_person_result(step["id"], request)

    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", agent_step)
    await WorkflowRunner(session_factory=async_session).run(run_id)

    compact = (await client.get(f"/api/v1/workflows/runs/{run_id}?detail=false", headers=headers)).json()
    intervention = compact["intervention"]
    assert (compact["business_outcome"], intervention["kind"], intervention["step_id"]) == (
        "needs_input", "workflow_retry", "send",
    )
    assert intervention["observed_problem"] == request["missing"]
    assert intervention["required_change"] == request["ask"]
    assert not intervention.get("connection_provider")
