"""类型化 HITL 记录必须一路走到用户眼前的那张卡片上。

事故复盘的最后一段:``hitl_type`` / ``payload`` 已经正确写进
``approval_requests``(Phase 2 前几个 commit),但 ``post_hitl_card``
没把它们放进 ``Message.pending_action``,前端因此永远只看得到
"这一步需要你批准" —— 行是对的,屏幕是错的。

这个文件盯住那段管子:
  1. post_hitl_card 必须把 hitl_type / payload / task_id 放上卡片;
  2. error 卡片给的是 retry/cancel,绝不是 approve/always/reject;
  3. 走真实门禁(不打桩)时,本地 worker 离线这句真话必须出现在卡片上。
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.constants.approvals import HitlType
from packages.core.governance.service import post_hitl_card
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Message
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import Agent, AgentSubscription, Workspace



async def _workspace(db):
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db.add(Workspace(
        id=workspace_id, entity_id=entity_id, name="WS", status="active",
    ))
    await db.flush()
    return entity_id, workspace_id


async def _card(db, entity_id):
    return (await db.execute(
        select(Message).where(
            Message.pending_action.isnot(None),
            Message.pending_action["kind"].as_string() == "governance_approval",
        ).order_by(Message.created_at.desc(), Message.id.desc())
    )).scalars().first()


@pytest.mark.asyncio
async def test_authorize_card_carries_its_type(db_session):
    """默认的 authorize 卡片也要带上 hitl_type —— 前端据此分流。"""
    entity_id, workspace_id = await _workspace(db_session)
    plan_id, step_id, task_id = generate_ulid(), generate_ulid(), generate_ulid()

    await post_hitl_card(
        entity_id=entity_id, workspace_id=workspace_id,
        plan_id=plan_id, step_id=step_id, step_key="create_automation",
        kind="subagent", action_key="workspace.automation.create",
        matched_rule="step.high_risk", reason="High-risk step needs approval.",
        approval_request_id=generate_ulid(), task_id=task_id,
        db=db_session,
    )
    card = await _card(db_session, entity_id)
    assert card is not None
    pa = card.pending_action
    assert pa["hitl_type"] == HitlType.AUTHORIZE.value
    assert pa["payload"] == {}
    assert pa["task_id"] == task_id, "每张卡片都要能跳回它的任务"
    assert pa["options"] == ["approve", "always_approve", "reject"], (
        "authorize 卡片的按钮不能被 error 分支改掉"
    )


@pytest.mark.asyncio
async def test_publish_card_offers_always_too(db_session):
    """发布类能力同样提供「始终批准」—— 用户说 always 就是 always。

    曾经有过一档「可批准一次、不可永久」的能力分级（publish/email/message
    不给 Always 按钮）。该产品决策已被否决：门禁的作用是让用户知情并做主，
    不是替用户判断哪一类授权他「其实不该想要」。唯一的硬阻断是
    ``never_allow``，而那一条用户自己也能改。
    """
    entity_id, workspace_id = await _workspace(db_session)

    await post_hitl_card(
        entity_id=entity_id, workspace_id=workspace_id,
        plan_id=generate_ulid(), step_id=generate_ulid(),
        step_key="publish_post", kind="subagent",
        action_key="social_post.publish",
        matched_rule="step.high_risk", reason="High-risk step needs approval.",
        approval_request_id=generate_ulid(), task_id=generate_ulid(),
        db=db_session,
    )
    card = await _card(db_session, entity_id)
    assert card is not None
    pa = card.pending_action
    assert pa["hitl_type"] == HitlType.AUTHORIZE.value
    assert pa["options"] == ["approve", "always_approve", "reject"]
    # 卡片上不再挂能力分级标记 —— 没有这一档了。
    assert "standing_grant_eligible" not in pa


@pytest.mark.asyncio
async def test_error_card_carries_the_real_failure_and_offers_no_approve(db_session):
    """error 卡片:说清楚坏在哪、怎么修,并且不提供"批准"。

    "批准"在这里是谎话 —— 这一步已经跑过了,没有任何东西可以授权。
    操作者连点 15 次批准,正是因为界面给了他一个批准按钮。
    """
    entity_id, workspace_id = await _workspace(db_session)
    payload = {
        "what_happened": "Manor could not reach the browser on your computer.",
        "why": "the Chrome MCP reported no paired local worker online.",
        "action_to_take": (
            "Start the local worker on the computer where you're signed in, "
            "then retry."
        ),
        "action_link": "/integrations",
        "is_transient": True,
    }

    await post_hitl_card(
        entity_id=entity_id, workspace_id=workspace_id,
        plan_id=generate_ulid(), step_id=generate_ulid(),
        step_key="publish_via_chrome", kind="subagent",
        action_key="external.social", matched_rule="step.high_risk",
        reason="High-risk step needs one-time operator approval.",
        approval_request_id=generate_ulid(),
        hitl_type=HitlType.ERROR.value, payload=payload,
        task_id=generate_ulid(), db=db_session,
    )
    card = await _card(db_session, entity_id)
    assert card is not None
    pa = card.pending_action

    assert pa["hitl_type"] == HitlType.ERROR.value
    assert pa["payload"]["what_happened"] == payload["what_happened"]
    assert pa["payload"]["action_to_take"] == payload["action_to_take"]
    assert pa["payload"]["action_link"] == "/integrations"

    assert pa["options"] == ["retry", "cancel"]
    assert "approve" not in pa["options"]
    assert "always_approve" not in pa["options"]

    # 卡片正文也不能再说"需要批准"。
    assert "Approval needed" not in card.content
    assert "could not reach the browser" in card.content
    # prompt 是给不认识 payload 的老客户端兜底的,也必须是真话。
    assert pa["prompt"] == payload["what_happened"]


async def _gated_scenario(db):
    """一个需要审批、能被真 worker 领走的 workspace step。

    与 tests/test_error_surfacing.py 的同名工具一致 —— 只有完整的
    workspace/agent/subscription/worker 链条,被批准的步骤才真的会被
    派发出去(否则测不到 fail_lease 之后那次重新过闸)。
    """
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
    task_id = generate_ulid()
    agent_id = generate_ulid()
    subscription_id = generate_ulid()
    worker = Worker(
        id=generate_ulid(), entity_id=entity_id, kind="internal",
        display_name="Internal worker",
        capabilities={"supported_kinds": ["subagent"], "max_risk_level": "high"},
        monthly_spent_usd=Decimal("0"), auto_pause_on_budget=True, status="active",
    )
    db.add_all([
        Workspace(id=workspace_id, entity_id=entity_id,
                  name="Gated workspace", status="active"),
        worker,
        Agent(id=agent_id, entity_id=entity_id, name="Content Agent",
              slug=f"content-{agent_id[:8].lower()}", status="active"),
        AgentSubscription(
            id=subscription_id, entity_id=entity_id, agent_id=agent_id,
            workspace_id=workspace_id, name="Content",
            service_key="content", status="active",
        ),
        SubscriptionWorker(
            subscription_id=subscription_id, worker_id=worker.id,
            priority=100, is_preferred=True,
        ),
        ExecutionPlan(
            id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
            status="running", execution_mode="live", task_id=task_id,
            approval_required=False, plan_dag={"steps": []},
        ),
        ExecutionStep(
            id=step_id, plan_id=plan_id, entity_id=entity_id,
            workspace_id=workspace_id, step_key="publish_via_chrome",
            kind="subagent", service_key="content", capability_id="external.social",
            params={"prompt": "Publish the approved post."},
            depends_on=[], step_status="pending", risk_level="high",
            requires_approval=True, attempt_count=0, max_attempts=3,
        ),
    ])
    await db.flush()
    return {
        "entity_id": entity_id, "step_id": step_id,
        "task_id": task_id, "worker": worker,
    }




# ── error 卡片的两个按钮真的要能用 ───────────────────────────────────


async def _register(client, username: str) -> dict:
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": f"{username}@test.com",
            "password": "pass123",
            "entity_name": f"{username} Corp",
        },
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed_error_card(client, db, username: str):
    """一个卡在 waiting_human 的失败步骤 + 它的 error 请求 + 那张卡片。"""
    from packages.core.constants.approvals import ApprovalStatus
    from packages.core.models.task import Conversation

    headers = await _register(client, username)
    ws = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Error Card WS"},
    )
    body = ws.json()
    entity_id, workspace_id = body["entity_id"], body["id"]

    plan_id, task_id, step_id = generate_ulid(), generate_ulid(), generate_ulid()
    request_id, conv_id, message_id = (
        generate_ulid(), generate_ulid(), generate_ulid(),
    )
    db.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        task_id=task_id, plan_dag={}, status="running",
    ))
    db.add(ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id,
        workspace_id=workspace_id, step_key="publish_via_chrome",
        kind="subagent", params={}, step_status="waiting_human",
        attempt_count=1, max_attempts=3, risk_level="high",
        capability_id="browser.control",
    ))
    db.add(HitlRequest(
        id=request_id, entity_id=entity_id, workspace_id=workspace_id,
        capability_id="browser.control", risk_level="high",
        origin_kind="step", origin_step_id=step_id, origin_plan_id=plan_id,
        origin_task_id=task_id, status=ApprovalStatus.PENDING.value,
        dedup_key=f"step:{step_id}", reason="Manor could not reach the browser.",
        hitl_type=HitlType.ERROR.value,
    ))
    db.add(Conversation(
        id=conv_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Error card", channel="workspace", scope="workspace_main",
    ))
    db.add(Message(
        id=message_id, conversation_id=conv_id, role="system",
        content="⚠️ Action needed — Manor could not reach the browser.",
        author_kind="system", message_kind="hitl_request",
        pending_action={
            "kind": "governance_approval",
            "step_id": step_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "approval_request_id": request_id,
            "hitl_type": HitlType.ERROR.value,
            "options": ["retry", "cancel"],
        },
    ))
    await db.commit()
    return headers, workspace_id, message_id, step_id, request_id


async def _resolve(client, headers, workspace_id, message_id, choice):
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
        headers=headers, json={"choice": choice},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_retry_on_an_error_card_resumes_the_step(client, db_session):
    """「重试」必须真的重试。

    error 卡片不提供 approve,所以路由不能只认 approve —— 否则 retry
    会掉进 else 分支,把用户刚修好的那一步直接取消掉。
    """
    headers, ws_id, msg_id, step_id, req_id = await _seed_error_card(
        client, db_session, "error_card_retry",
    )
    await _resolve(client, headers, ws_id, msg_id, "retry")

    db_session.expire_all()
    step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one()
    request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.id == req_id)
    )).scalar_one()

    assert step.step_status == "pending", "重试后步骤必须回到队列"
    assert step.error is None
    assert request.status == "granted"


@pytest.mark.asyncio
async def test_cancel_on_an_error_card_gives_up_on_the_step(client, db_session):
    """「取消」仍然是放弃这一步 —— error 卡片的另一半也得管用。"""
    headers, ws_id, msg_id, step_id, req_id = await _seed_error_card(
        client, db_session, "error_card_cancel",
    )
    await _resolve(client, headers, ws_id, msg_id, "cancel")

    db_session.expire_all()
    step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one()
    request = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.id == req_id)
    )).scalar_one()

    assert step.step_status == "failed"
    assert request.status == "denied"
