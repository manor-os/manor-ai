"""Phase 1: lease_needs_human 接入统一 HitlRequest 记录层。

路径 C（执行中暂停）此前直接写 chat Message.pending_action，不留记录 —
无法 join 回 task/step，也无法复用去重。这些测试钉住接线后的行为。
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.ai.runtime.control import RuntimeAgentCheckpoint
from packages.core.ai.runtime.workspace_delegation_actions import _tool_arguments_hash
from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.execution import (
    COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY,
    WorkLeaseStatus,
)
from packages.core.constants.pending_actions import PendingActionKind
from packages.core.dispatcher.service import Dispatcher
from packages.core.governance.approvals import (
    ApprovalOrigin,
    ApprovalSubject,
    PermissionDecision,
    count_open_requests,
    dedup_key_for,
    grant_approval,
    resolve_approval as _resolve_approval,
)
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Conversation, Message, Task
from packages.core.models.worker import Worker, WorkLease
from packages.core.models.workspace import Workspace
from packages.core.services.runtime_authorization import AuthorizationRule


_POLICY_TEST_PERMISSION = PermissionDecision.allow(AuthorizationRule.SYSTEM_WORKER)


async def resolve_approval(*args, **kwargs):
    return await _resolve_approval(
        *args,
        permission_decision=_POLICY_TEST_PERMISSION,
        **kwargs,
    )


def _subject(entity_id):
    return ApprovalSubject(
        entity_id=entity_id,
        action_key="chrome.fill_or_select",
        capability_id="browser.control",
        risk_level="medium",
        kind="subagent",
    )


def test_lease_origin_dedup_key_includes_lease_and_kind():
    entity_id = generate_ulid()
    lease_id = generate_ulid()
    origin = ApprovalOrigin(
        kind="lease",
        lease_id=lease_id,
        step_id=generate_ulid(),
        context={"pending_kind": "needs_confirmation"},
    )
    assert dedup_key_for(_subject(entity_id), origin) == (f"lease:{lease_id}:needs_confirmation")


def test_lease_origin_same_lease_different_kind_differs():
    """同一租约因不同原因暂停 → 各自独立请求。"""
    entity_id = generate_ulid()
    lease_id = generate_ulid()
    step_id = generate_ulid()
    confirm = ApprovalOrigin(
        kind="lease",
        lease_id=lease_id,
        step_id=step_id,
        context={"pending_kind": "needs_confirmation"},
    )
    login = ApprovalOrigin(
        kind="lease",
        lease_id=lease_id,
        step_id=step_id,
        context={"pending_kind": "needs_login"},
    )
    assert dedup_key_for(_subject(entity_id), confirm) != dedup_key_for(_subject(entity_id), login)


def test_lease_origin_same_lease_same_kind_dedups():
    """同一租约同一原因重复触发 → 同一 key，复用卡片。"""
    entity_id = generate_ulid()
    lease_id = generate_ulid()
    step_id = generate_ulid()

    def _origin():
        return ApprovalOrigin(
            kind="lease",
            lease_id=lease_id,
            step_id=step_id,
            context={"pending_kind": "needs_confirmation"},
        )

    assert dedup_key_for(_subject(entity_id), _origin()) == dedup_key_for(_subject(entity_id), _origin())


def test_lease_origin_without_pending_kind_falls_back():
    """pending_kind 缺失时仍须产出稳定的 lease-scoped key，不得落回 tool: 分支。"""
    entity_id = generate_ulid()
    lease_id = generate_ulid()
    origin = ApprovalOrigin(kind="lease", lease_id=lease_id, step_id=generate_ulid())
    assert dedup_key_for(_subject(entity_id), origin) == f"lease:{lease_id}:human_input"


def test_tool_call_dedup_key_includes_resource_scope():
    entity_id = generate_ulid()
    conversation_id = generate_ulid()
    origin = ApprovalOrigin(
        kind="tool_call",
        conversation_id=conversation_id,
        context={"arguments": {"path": "docs/status.xlsx"}},
    )
    exact = dedup_key_for(
        ApprovalSubject(
            entity_id=entity_id,
            action_key="workspace.file.modify",
            resource_id="docs/status.xlsx",
            capability_id="file.write",
            risk_level="medium",
            kind="action",
        ),
        origin,
    )
    other = dedup_key_for(
        ApprovalSubject(
            entity_id=entity_id,
            action_key="workspace.file.modify",
            resource_id="docs/other.xlsx",
            capability_id="file.write",
            risk_level="medium",
            kind="action",
        ),
        origin,
    )

    assert exact != other
    assert exact.startswith(f"tool:{conversation_id}:workspace.file.modify#resource:docs/status.xlsx:")


def test_tool_call_dedup_key_bounds_long_workspace_file_paths():
    entity_id = generate_ulid()
    conversation_id = generate_ulid()
    resource_id = (
        "Workspaces/_by_id/01M1Y03Z6ZYDV7FN6TXA7G1QC8/runs/"
        "01M280X5QM3KXZPYQXH4H76SG4/technical/stickman-workspace-assets.md"
    )
    origin = ApprovalOrigin(
        kind="tool_call",
        conversation_id=conversation_id,
        args_hash="a" * 64,
    )

    key = dedup_key_for(
        ApprovalSubject(
            entity_id=entity_id,
            action_key="workspace.file.modify",
            resource_id=resource_id,
            capability_id="file.write",
            risk_level="medium",
            kind="action",
        ),
        origin,
    )
    same_key = dedup_key_for(
        ApprovalSubject(
            entity_id=entity_id,
            action_key="workspace.file.modify",
            resource_id=resource_id,
            capability_id="file.write",
            risk_level="medium",
            kind="action",
        ),
        origin,
    )
    other_key = dedup_key_for(
        ApprovalSubject(
            entity_id=entity_id,
            action_key="workspace.file.modify",
            resource_id=f"{resource_id}.other",
            capability_id="file.write",
            risk_level="medium",
            kind="action",
        ),
        origin,
    )

    assert len(key) <= 200
    assert key == same_key
    assert key != other_key
    assert key.startswith(f"tool:{conversation_id}:workspace.file.modify#resource:")


async def _lease_fixture(db, *, entity_id=None, workspace_id=None):
    """A workspace + running plan + leased step, ready to pause.

    Pass ``entity_id`` / ``workspace_id`` to attach the fixture to a workspace
    that already exists (e.g. one created through the API by a registered
    user), so the resolve endpoint's entity scoping matches. When
    ``workspace_id`` is supplied the Workspace row is assumed to exist.
    """
    entity_id = entity_id or generate_ulid()
    if workspace_id is None:
        workspace_id = generate_ulid()
        db.add(
            Workspace(
                id=workspace_id,
                entity_id=entity_id,
                name="Lease WS",
                operating_model={},
            )
        )

    worker_id = generate_ulid()
    db.add(
        Worker(
            id=worker_id,
            entity_id=entity_id,
            kind="internal",
            display_name="W",
            trust_level="trusted",
            status="active",
            capabilities={"supported_kinds": ["subagent"]},
            consecutive_failures=0,
        )
    )

    plan_id = generate_ulid()
    task_id = generate_ulid()
    db.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            plan_dag={},
            status="running",
        )
    )

    step_id = generate_ulid()
    lease_id = generate_ulid()
    db.add(
        ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="publish_via_chrome",
            kind="subagent",
            params={},
            step_status="running",
            attempt_count=1,
            max_attempts=3,
            risk_level="medium",
            capability_id="browser.control",
            # A granted lease always owns its running step; a report through
            # a lease the step no longer points at is refused as superseded.
            current_lease_id=lease_id,
        )
    )

    db.add(
        WorkLease(
            id=lease_id,
            step_id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            worker_id=worker_id,
            status=WorkLeaseStatus.ACTIVE.value,
            # lease_until is NOT NULL on the model — omitting it fails the insert.
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db.flush()
    return {
        "entity_id": entity_id,
        "workspace_id": workspace_id,
        "plan_id": plan_id,
        "task_id": task_id,
        "step_id": step_id,
        "lease_id": lease_id,
    }


@pytest.mark.asyncio
async def test_lease_needs_human_mints_request(db_session):
    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Confirm the LinkedIn post before publishing",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.flush()

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()
    assert req.status == ApprovalStatus.PENDING.value
    assert req.origin_kind == "lease"
    assert req.dedup_key == f"lease:{fx['lease_id']}:needs_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending_kind,expected_hitl_type",
    [
        ("needs_confirmation", "authorize"),
        ("needs_login", "input"),
        ("needs_input", "input"),
        ("human_input", "input"),
    ],
)
async def test_lease_pause_records_what_it_actually_asks_for(
    db_session,
    pending_kind,
    expected_hitl_type,
):
    """路径 C 的暂停必须写下自己是「要信息」还是「要许可」。

    记录层的 ``hitl_type`` 默认是 ``authorize``，而 lease_needs_human 自己的
    注释写着 "the human is being asked for information, not permission" ——
    默认值把这句话反过来存进了库。四个读取面都以 ``hitl_type`` 为判据，所以
    这一条错了，CAPTCHA 墙就会继续以「治理审批」的身份排队变老。
    """
    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Please help",
        pending_action={"kind": pending_kind},
    )
    await db_session.flush()
    db_session.expire_all()

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()
    assert req.hitl_type == expected_hitl_type
    assert req.is_governance() is (expected_hitl_type == "authorize")


@pytest.mark.asyncio
async def test_lease_request_joins_back_to_task_and_plan(db_session):
    """§3.3 验收：每一次暂停都能 join 回具体 task/step。"""
    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Need input",
        pending_action={"kind": "needs_input"},
    )
    await db_session.flush()
    # Expire first: without it the select returns the identity-mapped instance
    # and the asserts would only restate in-memory state, not persisted columns.
    db_session.expire_all()

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()
    assert req.origin_task_id == fx["task_id"]
    assert req.origin_plan_id == fx["plan_id"]
    assert req.workspace_id == fx["workspace_id"]


@pytest.mark.asyncio
async def test_lease_needs_human_twice_same_reason_reuses_request(db_session):
    """同一租约同一原因重复暂停 → 只有一条记录（不重复弹卡）。"""
    fx = await _lease_fixture(db_session)
    svc = Dispatcher()
    for _ in range(2):
        # lease_needs_human parks the lease in needs_human; a second pause on
        # the same lease only happens after it is re-leased, so re-activate it.
        lease = (await db_session.execute(select(WorkLease).where(WorkLease.id == fx["lease_id"]))).scalar_one()
        lease.status = WorkLeaseStatus.ACTIVE.value
        await db_session.flush()

        await svc.lease_needs_human(
            db_session,
            fx["lease_id"],
            prompt="Confirm",
            pending_action={"kind": "needs_confirmation"},
        )
        await db_session.flush()

    rows = (
        (await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])))
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_granted_lease_request_stays_findable_until_consumed(db_session):
    """granted 不是终态 —— 这正是端点必须紧接着 consume 的原因。

    `_find_open_request` 把 granted-unconsumed 视为存活（它的 docstring 说明
    了为什么：授权必须保持可见，否则下一次 resolve 看不见它）。所以「回答
    之后请求就不再悬着」只有在 consumed 之后才成立。本用例钉住这条语义：
    grant 之后仍然找得到，consume 之后才真正离场。

    这里直接调 governance 函数、不经过卡片；端点侧行为由
    test_lease_card_roundtrip_* 覆盖。
    """
    from packages.core.governance.approvals import (
        _find_open_request,
        consume_approval,
        grant_approval,
    )

    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Confirm",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.flush()

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()
    await grant_approval(
        db_session,
        req,
        by_user_id=generate_ulid(),
        via="chat_card",
        authority_prechecked=True,
    )
    await db_session.flush()

    # 只 grant：仍然存活 —— 单靠 grant 关不掉这一行。
    still_open = await _find_open_request(db_session, fx["entity_id"], req.dedup_key)
    assert still_open is not None
    assert still_open.status == ApprovalStatus.GRANTED.value
    assert still_open.decided_at is not None

    # consume 之后：真正离场。
    await consume_approval(db_session, req)
    await db_session.flush()
    assert await _find_open_request(db_session, fx["entity_id"], req.dedup_key) is None


@pytest.mark.asyncio
async def test_plan_terminal_expires_lease_request(db_session):
    """计划到达终态时租约请求随之过期（复用既有 origin 清理，无需新逻辑）。

    用 `plan_id=` 调用：生产中 resolve_origin_requests 只有一个调用点 ——
    executor.py 的计划终态路径 —— 传的正是 plan_id。用 `step_id=` 也能过，
    但那锁的是 helper 的参数分支，不是 docstring 声称的生命周期保证。

    这条保证在 path C 上够不着：步骤停在 waiting_human 时计划永不 finalize，
    所以它只在别的东西先让步骤离开 waiting_human 之后才起作用 —— 也正是
    lease_needs_human 只为「端点关得掉的 kind」铸行的原因。
    """
    from packages.core.governance.approvals import resolve_origin_requests

    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Confirm",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.flush()

    closed = await resolve_origin_requests(db_session, plan_id=fx["plan_id"])
    await db_session.flush()
    assert closed == 1

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()
    assert req.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_fallback_card_without_pending_action_carries_request_id(
    db_session,
    monkeypatch,
):
    """apps/api/routers/workers.py 调用时不带 pending_action —— 走通知器自建的
    fallback 卡片分支。那张卡也必须带 approval_request_id，否则回答时无从关闭，
    请求永远悬着。

    断言点选在 ``notifiers._safe_post``（真正落库的卡片载荷）而不是
    ``notify_step_needs_human`` 的入参：后者只能证明 dispatcher 传了值，
    证明不了 fallback 分支把它合进了卡片 —— 而那正是本用例要钉的那条缝。
    """
    from packages.core.workspace_chat import notifiers as chat_notify

    posted: list[dict] = []

    async def _capture(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(chat_notify, "_safe_post", _capture)

    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Solve the CAPTCHA",
        # 无 pending_action —— 正是 workers.py 路由的调用形态。
    )
    await db_session.flush()

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()

    assert len(posted) == 1, "needs_human 应当恰好投递一张卡片"
    card = posted[0]["pending_action"]
    # fallback 分支的两个指纹：自建的 human_input kind + input_schema。
    # 若它们变了，说明有人用「伪造 pending_action」的方式补这条缝，
    # 那会改掉消息体与表单结构（越界的 UX 变更）。
    assert card["kind"] == "human_input"
    assert "input_schema" in card
    assert posted[0]["body"].startswith("⚠ Need your input on step")
    # 本用例的核心断言。
    assert card["approval_request_id"] == req.id


@pytest.mark.asyncio
async def test_expired_lease_with_unsettled_effect_persists_reconciliation_request_and_card(
    db_session,
    monkeypatch,
):
    from packages.core.workspace_chat import notifiers as chat_notify

    posted: list[dict] = []

    async def _capture(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(chat_notify, "_safe_post", _capture)
    fx = await _lease_fixture(db_session)
    lease = await db_session.get(WorkLease, fx["lease_id"])
    step = await db_session.get(ExecutionStep, fx["step_id"])
    lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    step.evidence_refs = [{
        "kind": "tool_effect",
        "effect": "pending",
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "call-1",
            "arguments_hash": "a" * 64,
            "intent_state": "started",
        },
    }]
    await db_session.commit()

    assert await Dispatcher().expire_leases(db_session) >= 1
    await db_session.commit()
    db_session.expire_all()

    lease = await db_session.get(WorkLease, fx["lease_id"])
    step = await db_session.get(ExecutionStep, fx["step_id"])
    request = (
        await db_session.execute(
            select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
        )
    ).scalar_one()
    assert lease.status == WorkLeaseStatus.EXPIRED.value
    assert step.step_status == "waiting_human"
    assert request.status == ApprovalStatus.PENDING.value
    assert request.origin_kind == "lease"
    assert request.dedup_key == f"lease:{fx['lease_id']}:effect_reconciliation"
    assert request.hitl_type == "error"
    assert "external action" in request.reason.lower()
    assert "verify it with the provider" in request.reason.lower()

    target_posts = [
        item
        for item in posted
        if (item.get("pending_action") or {}).get("approval_request_id") == request.id
    ]
    assert len(target_posts) == 1
    card = target_posts[0]["pending_action"]
    assert card["kind"] == "effect_reconciliation"
    assert card["approval_request_id"] == request.id
    assert "confirm_effect_not_applied" in card["options"]
    assert card["payload"]["reconciliation"]["not_applied_allowed"] is True
    assert "external action" in target_posts[0]["body"].lower()


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


async def _seed_direct_effect_reconciliation_card(
    client,
    db_session,
    username: str,
) -> tuple[dict, dict, str]:
    headers = await _register(client, username)
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Concurrent effect reconciliation WS"},
    )
    assert workspace_response.status_code in (200, 201), workspace_response.text
    workspace = workspace_response.json()
    fixture = await _lease_fixture(
        db_session,
        entity_id=workspace["entity_id"],
        workspace_id=workspace["id"],
    )
    db_session.add(Task(
        id=fixture["task_id"],
        entity_id=fixture["entity_id"],
        workspace_id=fixture["workspace_id"],
        title="Publish exactly once",
        status="in_progress",
        priority=3,
        task_type="general",
        details={},
    ))
    await db_session.flush()

    tool_name = "mcp__example__publish"
    tool_call_id = "call-concurrent-publish"
    arguments = {"title": "Launch", "body": "Ready"}
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "Publish it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": '{"title":"Launch","body":"Ready"}',
                    },
                }],
            },
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={
            "id": tool_call_id,
            "name": tool_name,
            "arguments": arguments,
        },
    ).to_dict()
    effect = {
        "kind": "tool_effect",
        "effect": "pending",
        "fields": {
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "arguments_hash": _tool_arguments_hash(arguments),
            "effect_generation": 0,
            "owner_step_id": fixture["step_id"],
            "owner_plan_id": fixture["plan_id"],
            "intent_state": "started",
        },
    }
    await Dispatcher().lease_needs_human(
        db_session,
        fixture["lease_id"],
        prompt="Verify whether the publish happened",
        evidence_refs=[effect],
        runtime_checkpoint=checkpoint,
    )
    await db_session.commit()

    request = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.origin_step_id == fixture["step_id"],
            HitlRequest.context["pending_kind"].as_string()
            == PendingActionKind.EFFECT_RECONCILIATION.value,
        )
    )).scalar_one()
    card = (await db_session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == fixture["entity_id"],
            Conversation.workspace_id == fixture["workspace_id"],
            Message.pending_action["approval_request_id"].as_string()
            == request.id,
        )
    )).scalar_one()
    return headers, fixture, card.id


async def _seed_lease_card(client, db_session, username: str, *, kind: str):
    """A workspace + waiting_human step + PENDING request + the chat card
    that points at it. Returns everything the resolve endpoint needs."""
    headers = await _register(client, username)
    ws = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Lease Card WS"},
    )
    ws_body = ws.json()
    entity_id, workspace_id = ws_body["entity_id"], ws_body["id"]

    plan_id, task_id, step_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            task_id=task_id,
            plan_dag={},
            status="running",
        )
    )
    db_session.add(
        ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            step_key="publish_via_chrome",
            kind="subagent",
            params={},
            step_status="waiting_human",
            attempt_count=1,
            max_attempts=3,
            risk_level="medium",
            capability_id="browser.control",
        )
    )

    request_id = generate_ulid()
    db_session.add(
        HitlRequest(
            id=request_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            capability_id="browser.control",
            risk_level="medium",
            origin_kind="lease",
            origin_step_id=step_id,
            origin_plan_id=plan_id,
            origin_task_id=task_id,
            status=ApprovalStatus.PENDING.value,
            dedup_key=f"lease:{generate_ulid()}:{kind}",
            reason="Confirm before publishing",
        )
    )

    conv_id, message_id = generate_ulid(), generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=entity_id,
            workspace_id=workspace_id,
            title="Lease HITL",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Confirm before publishing",
            author_kind="agent",
            message_kind="hitl_request",
            pending_action={
                "kind": kind,
                "step_id": step_id,
                "plan_id": plan_id,
                "approval_request_id": request_id,
            },
        )
    )
    await db_session.commit()
    return headers, workspace_id, message_id, request_id


async def _resolve_card(client, headers, workspace_id, message_id, choice: str, *, note: str | None = None):
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={"choice": choice, **({"note": note} if note is not None else {})},
    )
    assert resp.status_code == 200, resp.text
    return resp


@pytest.mark.asyncio
async def test_cancelling_direct_effect_reconciliation_terminalizes_without_dispatch(
    client,
    db_session,
    monkeypatch,
):
    """The service-owned CANCEL terminal state must not trip the route's wakeup guard."""

    headers = await _register(client, "direct_effect_cancel")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Direct effect cancel WS"},
    )
    assert workspace_response.status_code in (200, 201), workspace_response.text
    workspace = workspace_response.json()
    fixture = await _lease_fixture(
        db_session,
        entity_id=workspace["entity_id"],
        workspace_id=workspace["id"],
    )
    db_session.add(Task(
        id=fixture["task_id"],
        entity_id=fixture["entity_id"],
        workspace_id=fixture["workspace_id"],
        title="Publish launch update",
        status="in_progress",
        priority=3,
        task_type="general",
        details={},
    ))
    await db_session.flush()

    tool_name = "mcp__example__publish"
    tool_call_id = "call-publish-1"
    arguments = {"title": "Launch", "body": "Ready"}
    checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "Publish it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": '{"title":"Launch","body":"Ready"}',
                    },
                }],
            },
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={
            "id": tool_call_id,
            "name": tool_name,
            "arguments": arguments,
        },
    ).to_dict()
    effect = {
        "kind": "tool_effect",
        "effect": "pending",
        "fields": {
            "tool": tool_name,
            "tool_call_id": tool_call_id,
            "arguments_hash": _tool_arguments_hash(arguments),
            "effect_generation": 0,
            "owner_step_id": fixture["step_id"],
            "owner_plan_id": fixture["plan_id"],
            "intent_state": "started",
        },
    }
    await Dispatcher().lease_needs_human(
        db_session,
        fixture["lease_id"],
        prompt="Verify whether the publish happened",
        evidence_refs=[effect],
        runtime_checkpoint=checkpoint,
    )
    await db_session.commit()

    request = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.origin_step_id == fixture["step_id"],
            HitlRequest.context["pending_kind"].as_string()
            == PendingActionKind.EFFECT_RECONCILIATION.value,
        )
    )).scalar_one()
    messages = list((await db_session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == fixture["entity_id"],
            Conversation.workspace_id == fixture["workspace_id"],
        )
    )).scalars().all())
    card = next(
        message
        for message in messages
        if isinstance(message.pending_action, dict)
        and message.pending_action.get("approval_request_id") == request.id
    )
    request_id = request.id
    card_id = card.id

    from packages.core.models.runtime_run import RuntimeOutboxEvent
    from packages.core.plans.wakeup import (
        PLAN_CYCLE_OUTBOX_EVENT,
        plan_cycle_outbox_dedupe_key,
    )
    from tests.test_plan_retries import _capture_plan_cycle_hints

    plan_hints = _capture_plan_cycle_hints(monkeypatch)
    response = await client.post(
        f"/api/v1/workspaces/{fixture['workspace_id']}/chat/messages/{card_id}/resolve",
        headers=headers,
        json={"choice": "cancel"},
    )

    assert response.status_code == 200, response.text
    assert plan_hints == []
    db_session.expire_all()
    plan = await db_session.get(ExecutionPlan, fixture["plan_id"])
    task = await db_session.get(Task, fixture["task_id"])
    step = await db_session.get(ExecutionStep, fixture["step_id"])
    request = await db_session.get(HitlRequest, request_id)
    card = await db_session.get(Message, card_id)
    pending_wake = (await db_session.execute(
        select(RuntimeOutboxEvent.id).where(
            RuntimeOutboxEvent.event_type == PLAN_CYCLE_OUTBOX_EVENT,
            RuntimeOutboxEvent.aggregate_id == fixture["plan_id"],
            RuntimeOutboxEvent.dedupe_key
            == plan_cycle_outbox_dedupe_key(fixture["plan_id"]),
            RuntimeOutboxEvent.delivered_at.is_(None),
        )
    )).scalar_one_or_none()
    assert pending_wake is None
    assert plan.status == "cancelled"
    assert task.status == "cancelled"
    assert step.step_status == "failed"
    assert request.status == ApprovalStatus.DENIED.value
    assert card.resolved_at is not None


@pytest.mark.asyncio
async def test_concurrent_identical_effect_reconciliation_projects_and_dispatches_once(
    client,
    db_session,
    monkeypatch,
):
    """Two request sessions may replay the decision; one Message CAS owns output."""

    headers, fixture, card_id = await _seed_direct_effect_reconciliation_card(
        client,
        db_session,
        "concurrent_effect_reconciliation",
    )
    evidence_calls: list[str] = []
    activity_calls: list[str] = []
    from tests.test_plan_retries import (
        _assert_exact_pending_plan_cycle_wake,
        _capture_plan_cycle_hints,
    )

    plan_hints = _capture_plan_cycle_hints(monkeypatch)

    async def _capture_evidence(*_args, **kwargs):
        evidence_calls.append(str(kwargs["message_id"]))
        return []

    async def _capture_activity(*_args, **kwargs):
        activity_calls.append(str(kwargs["message_id"]))

    monkeypatch.setattr(
        "apps.api.routers.workspace_chat._record_pending_action_resolution_evidence",
        _capture_evidence,
    )
    monkeypatch.setattr(
        "apps.api.routers.workspace_chat._record_pending_action_activity",
        _capture_activity,
    )

    path = (
        f"/api/v1/workspaces/{fixture['workspace_id']}"
        f"/chat/messages/{card_id}/resolve"
    )
    first, second = await asyncio.gather(
        client.post(
            path,
            headers=headers,
            json={"choice": "confirm_effect_not_applied"},
        ),
        client.post(
            path,
            headers=headers,
            json={"choice": "confirm_effect_not_applied"},
        ),
    )

    assert [first.status_code, second.status_code] == [200, 200], [
        first.text, second.text,
    ]
    assert [hint["plan_id"] for hint in plan_hints] == [fixture["plan_id"]]
    await _assert_exact_pending_plan_cycle_wake(
        db_session,
        fixture["plan_id"],
        plan_hints[0],
    )
    assert evidence_calls == [card_id]
    assert activity_calls == [card_id]

    db_session.expire_all()
    card = await db_session.get(Message, card_id)
    messages = list((await db_session.execute(
        select(Message).where(Message.conversation_id == card.conversation_id)
    )).scalars().all())
    followups = [
        message
        for message in messages
        if any(
            isinstance(ref, dict)
            and ref.get("type") == "message"
            and ref.get("id") == card_id
            for ref in (message.refs or [])
        )
    ]
    assert card.resolved_at is not None
    assert card.resolution["choice"] == "confirm_effect_not_applied"
    assert len(followups) == 1


@pytest.mark.asyncio
async def test_expired_claim_rejects_not_applied_until_claim_is_released(
    client,
    db_session,
    monkeypatch,
):
    """An expired lease with a live claim cannot honestly authorize a retry."""

    headers = await _register(client, "expired_effect_claim")
    workspace_response = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Expired effect claim WS"},
    )
    assert workspace_response.status_code in (200, 201), workspace_response.text
    workspace = workspace_response.json()
    fixture = await _lease_fixture(
        db_session,
        entity_id=workspace["entity_id"],
        workspace_id=workspace["id"],
    )
    db_session.add(Task(
        id=fixture["task_id"],
        entity_id=fixture["entity_id"],
        workspace_id=fixture["workspace_id"],
        title="Publish from an expired worker",
        status="in_progress",
        priority=3,
        task_type="general",
        details={},
    ))
    lease = await db_session.get(WorkLease, fixture["lease_id"])
    step = await db_session.get(ExecutionStep, fixture["step_id"])
    claim_id = generate_ulid()
    lease.execution_claim_id = claim_id
    lease.execution_claimed_at = datetime.now(timezone.utc)
    lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    effect_arguments = {"title": "Launch", "body": "Ready"}
    step.runtime_checkpoint = RuntimeAgentCheckpoint(
        messages=[
            {"role": "user", "content": "Publish it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-expired-1",
                    "type": "function",
                    "function": {
                        "name": "mcp__example__publish",
                        "arguments": '{"title":"Launch","body":"Ready"}',
                    },
                }],
            },
        ],
        usage={},
        rounds=1,
        tool_calls_made=[],
        pending_tool_call={
            "id": "call-expired-1",
            "name": "mcp__example__publish",
            "arguments": effect_arguments,
        },
    ).to_dict()
    step.evidence_refs = [{
        "kind": "tool_effect",
        "effect": "pending",
        "fields": {
            "tool": "mcp__example__publish",
            "tool_call_id": "call-expired-1",
            "arguments_hash": _tool_arguments_hash(effect_arguments),
            "intent_state": "started",
        },
    }]
    await db_session.commit()

    from tests.test_plan_retries import (
        _assert_exact_pending_plan_cycle_wake,
        _capture_plan_cycle_hints,
    )

    plan_hints = _capture_plan_cycle_hints(monkeypatch)
    assert await Dispatcher().expire_leases(db_session) >= 1
    await db_session.commit()
    expiry_wake = await _assert_exact_pending_plan_cycle_wake(
        db_session,
        fixture["plan_id"],
    )
    assert expiry_wake.payload["reason"] == "lease_expired"
    expiry_generation = str(expiry_wake.payload["wake_generation"])
    db_session.expire_all()

    lease = await db_session.get(WorkLease, fixture["lease_id"])
    request = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.origin_step_id == fixture["step_id"],
            HitlRequest.context["pending_kind"].as_string()
            == PendingActionKind.EFFECT_RECONCILIATION.value,
        )
    )).scalar_one()
    card = (await db_session.execute(
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.entity_id == fixture["entity_id"],
            Conversation.workspace_id == fixture["workspace_id"],
            Message.pending_action["approval_request_id"].as_string()
            == request.id,
        )
    )).scalar_one()
    request_id = request.id
    card_id = card.id
    assert lease.status == WorkLeaseStatus.EXPIRED.value
    assert lease.execution_claim_id == claim_id
    assert "confirm_effect_not_applied" in card.pending_action["options"]
    assert (
        card.pending_action["payload"]["reconciliation"]["not_applied_allowed"]
        is False
    )

    rejected = await client.post(
        f"/api/v1/workspaces/{fixture['workspace_id']}/chat/messages/{card_id}/resolve",
        headers=headers,
        json={"choice": "confirm_effect_not_applied"},
    )
    assert rejected.status_code == 409, rejected.text
    assert "not known to be quiescent" in rejected.json()["detail"]
    db_session.expire_all()
    lease = await db_session.get(WorkLease, fixture["lease_id"])
    request = await db_session.get(HitlRequest, request_id)
    card = await db_session.get(Message, card_id)
    assert request.status == ApprovalStatus.PENDING.value
    assert card.resolved_at is None
    assert lease.execution_claim_id == claim_id

    from packages.core.workers.execution_claim import release_execution_claim

    assert await release_execution_claim(
        db_session,
        fixture["lease_id"],
        claim_id=claim_id,
    )
    await db_session.commit()

    resumed = await client.post(
        f"/api/v1/workspaces/{fixture['workspace_id']}/chat/messages/{card_id}/resolve",
        headers=headers,
        json={"choice": "confirm_effect_not_applied"},
    )
    assert resumed.status_code == 200, resumed.text
    assert [hint["plan_id"] for hint in plan_hints] == [fixture["plan_id"]]
    resumed_wake = await _assert_exact_pending_plan_cycle_wake(
        db_session,
        fixture["plan_id"],
        plan_hints[0],
    )
    assert resumed_wake.payload["reason"] == "effect_reconciliation_resolved"
    assert resumed_wake.payload["wake_generation"] != expiry_generation
    db_session.expire_all()
    plan = await db_session.get(ExecutionPlan, fixture["plan_id"])
    task = await db_session.get(Task, fixture["task_id"])
    assert plan.status == "running"
    assert task.status == "in_progress"


async def _reload_request(db_session, request_id: str) -> HitlRequest:
    db_session.expire_all()
    return (await db_session.execute(select(HitlRequest).where(HitlRequest.id == request_id))).scalar_one()


@pytest.mark.asyncio
async def test_lease_card_rejects_mismatched_request_origin(client, db_session):
    headers, workspace_id, message_id, request_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_mismatched_origin",
        kind="needs_confirmation",
    )
    request = await db_session.get(HitlRequest, request_id)
    assert request is not None and request.origin_step_id is not None
    original_step_id = request.origin_step_id
    request.origin_step_id = generate_ulid()
    await db_session.commit()

    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={"choice": "confirm"},
    )

    assert response.status_code == 409, response.text
    db_session.expire_all()
    request = await db_session.get(HitlRequest, request_id)
    step = await db_session.get(ExecutionStep, original_step_id)
    message = await db_session.get(Message, message_id)
    assert request is not None and request.status == ApprovalStatus.PENDING.value
    assert step is not None and step.step_status == "waiting_human"
    assert message is not None and message.resolved_at is None


@pytest.mark.asyncio
async def test_resolving_lease_card_closes_request_end_to_end(client, db_session):
    """路径 C 卡片被确认 → 请求 granted。

    这条走真实的 /resolve 端点，因为要钉的是路由里那段新代码本身；
    直接调 grant_approval 只能证明 governance 函数好用，证明不了路由会调它。
    """
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_close",
        kind="needs_confirmation",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "confirm")

    req = await _reload_request(db_session, req_id)
    # CONSUMED, not GRANTED: the answer is both the approval and its spend.
    # A granted-unconsumed row is "live" per _find_open_request, so stopping
    # at GRANTED would leave every answered card sitting in that state.
    assert req.status == ApprovalStatus.CONSUMED.value
    assert req.consumed_at is not None
    assert req.decided_via == "chat_card"
    assert req.decided_at is not None


@pytest.mark.asyncio
async def test_resolving_an_already_closed_lease_card_is_idempotent(
    client,
    db_session,
):
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_idempotent",
        kind="needs_confirmation",
    )

    await _resolve_card(client, headers, ws_id, msg_id, "confirm")
    duplicate = await client.post(
        f"/api/v1/workspaces/{ws_id}/chat/messages/{msg_id}/resolve",
        headers=headers,
        json={"choice": "confirm"},
    )

    assert duplicate.status_code == 200, duplicate.text
    assert (await _reload_request(db_session, req_id)).status == (
        ApprovalStatus.CONSUMED.value
    )


@pytest.mark.asyncio
async def test_cancelling_lease_card_denies_request(client, db_session):
    """取消必须 deny，不能 grant。

    关闭逻辑跑在 if/elif 链之前，那里还没解释 choice —— 若无条件 grant，
    用户明确拒绝的操作会留下一条 live 的批准（_find_open_request 视
    granted-unconsumed 为存活），下一次 gate 直接放行。这是 fail-open。
    本用例钉住那份「与下方分支同步的」词表。
    """
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_cancel",
        kind="needs_confirmation",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "cancel")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.DENIED.value
    assert req.decided_via == "chat_card"
    assert req.decided_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected_decision", "expected_step_status"),
    [
        ("confirm", "grant", "pending"),
        ("cancel", "deny", "failed"),
    ],
)
async def test_coordinator_card_decides_exact_approval_before_parent_step_mutation(
    client,
    db_session,
    monkeypatch,
    choice,
    expected_decision,
    expected_step_status,
):
    """The bridge validator needs the parent to still be parked when it mirrors."""

    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        f"coordinator_card_order_{choice}",
        kind="needs_confirmation",
    )
    request = await db_session.get(HitlRequest, req_id)
    request.context = {
        **(request.context or {}),
        COORDINATOR_NESTED_APPROVAL_CONTEXT_KEY: {},
    }
    step_id = request.origin_step_id
    await db_session.commit()

    events = []

    async def fake_grant(db, approval, **_kwargs):
        step = await db.get(ExecutionStep, approval.origin_step_id)
        assert step.step_status == "waiting_human"
        events.append("grant")
        approval.status = ApprovalStatus.GRANTED.value
        return approval

    async def fake_consume(_db, approval, **_kwargs):
        assert events == ["grant"]
        events.append("consume")
        approval.status = ApprovalStatus.CONSUMED.value
        return approval

    async def fake_deny(db, approval, **_kwargs):
        step = await db.get(ExecutionStep, approval.origin_step_id)
        assert step.step_status == "waiting_human"
        events.append("deny")
        approval.status = ApprovalStatus.DENIED.value
        return approval

    monkeypatch.setattr(
        "packages.core.governance.approvals.grant_approval",
        fake_grant,
    )
    monkeypatch.setattr(
        "packages.core.governance.approvals.consume_approval",
        fake_consume,
    )
    monkeypatch.setattr(
        "packages.core.governance.approvals.deny_approval",
        fake_deny,
    )

    await _resolve_card(client, headers, ws_id, msg_id, choice)

    assert events[0] == expected_decision
    step = await db_session.get(ExecutionStep, step_id)
    await db_session.refresh(step)
    assert step.step_status == expected_step_status


@pytest.mark.asyncio
async def test_skipping_needs_input_card_denies_request(client, db_session):
    """needs_input 的 skip 走 else 分支取消步骤 —— 请求同样应当 denied。"""
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_skip",
        kind="needs_input",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "skip")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.DENIED.value


@pytest.mark.asyncio
async def test_signin_choice_leaves_login_request_pending(client, db_session):
    """sign_in 是两段式登录的前半段（后端 no-op，步骤仍 waiting_human）。

    此时结案会在用户真正登录之前就关掉请求，因此必须原样保持 PENDING，
    等 continue_after_login（grant）或 skip（deny）。
    """
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_signin",
        kind="needs_login",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "sign_in")

    req = await _reload_request(db_session, req_id)
    message = await db_session.get(Message, msg_id)
    assert req.status == ApprovalStatus.PENDING.value
    assert req.decided_at is None
    assert message is not None
    assert message.resolved_at is None
    assert message.resolution is None


@pytest.mark.asyncio
async def test_continue_after_login_grants_request(client, db_session):
    """两段式登录的后半段：cookies 已捕获、步骤重试 → 此时才结案。"""
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_after_login",
        kind="needs_login",
    )
    # 先走前半段，确认它没有提前结案，再走后半段。
    await _resolve_card(client, headers, ws_id, msg_id, "sign_in")
    assert (await _reload_request(db_session, req_id)).status == ApprovalStatus.PENDING.value

    await _resolve_card(client, headers, ws_id, msg_id, "continue_after_login")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.CONSUMED.value
    assert req.decided_via == "chat_card"


@pytest.mark.asyncio
async def test_lease_needs_human_survives_step_without_workspace(db_session):
    """无 workspace 的步骤不铸请求，但仍须跑完 lease_needs_human。

    dispatcher 在 `if step.workspace_id:` 之外读 approval_request 来给通知器
    传 id，所以那句预初始化是有载荷的：删掉它，这条路径抛 UnboundLocalError。
    该异常在 _safe_chat_step_needs_human 的 try 之外（实参求值阶段）触发，
    不会被吞掉 —— 但也只有无 workspace 的步骤会走到，其余用例覆盖不到。
    """
    entity_id = generate_ulid()
    worker_id = generate_ulid()
    db_session.add(
        Worker(
            id=worker_id,
            entity_id=entity_id,
            kind="internal",
            display_name="W",
            trust_level="trusted",
            status="active",
            capabilities={"supported_kinds": ["subagent"]},
            consecutive_failures=0,
        )
    )

    plan_id, step_id, lease_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(
        ExecutionPlan(
            id=plan_id,
            entity_id=entity_id,
            workspace_id=None,
            task_id=generate_ulid(),
            plan_dag={},
            status="running",
        )
    )
    db_session.add(
        ExecutionStep(
            id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=None,
            step_key="headless_step",
            kind="subagent",
            params={},
            step_status="running",
            attempt_count=1,
            max_attempts=3,
            risk_level="medium",
            capability_id="browser.control",
            current_lease_id=lease_id,
        )
    )
    db_session.add(
        WorkLease(
            id=lease_id,
            step_id=step_id,
            plan_id=plan_id,
            entity_id=entity_id,
            workspace_id=None,
            worker_id=worker_id,
            status=WorkLeaseStatus.ACTIVE.value,
            lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.flush()

    # 若预初始化被删，这一行抛 UnboundLocalError。
    lease = await Dispatcher().lease_needs_human(
        db_session,
        lease_id,
        prompt="Solve the CAPTCHA",
    )
    await db_session.flush()

    assert lease.status == WorkLeaseStatus.NEEDS_HUMAN.value
    # 无 workspace ⇒ 无卡片可渲染 ⇒ 不铸请求。
    rows = (await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == step_id))).scalars().all()
    assert rows == []


async def _lease_card_via_dispatcher(
    client,
    db_session,
    monkeypatch,
    username: str,
    *,
    pending_action=None,
):
    """跑完整链路：dispatcher → notifier → 卡片 → 落库，返回可被端点消费的消息。

    与 _seed_lease_card 的差别正是本组用例存在的理由：pending_action 不是手写
    的，而是 notifier 实际投递的那一份。手写卡片会跳过 notifier，于是
    「结构化分支有没有把 approval_request_id 合进去」这个问题根本没被问到。
    """
    from packages.core.workspace_chat import notifiers as chat_notify

    headers = await _register(client, username)
    ws = await client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Lease Roundtrip WS"},
    )
    ws_body = ws.json()
    fx = await _lease_fixture(
        db_session,
        entity_id=ws_body["entity_id"],
        workspace_id=ws_body["id"],
    )

    posted: list[dict] = []

    async def _capture(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(chat_notify, "_safe_post", _capture)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Confirm the publish",
        pending_action=pending_action,
    )
    await db_session.flush()
    assert len(posted) == 1, "needs_human 应当恰好投递一张卡片"
    card = posted[0]["pending_action"]

    req = (
        await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"]))
    ).scalar_one()

    conv_id, message_id = generate_ulid(), generate_ulid()
    db_session.add(
        Conversation(
            id=conv_id,
            entity_id=ws_body["entity_id"],
            workspace_id=ws_body["id"],
            title="Lease HITL",
            channel="workspace",
            scope="workspace_main",
        )
    )
    db_session.add(
        Message(
            id=message_id,
            conversation_id=conv_id,
            role="assistant",
            content="Confirm the publish",
            author_kind="agent",
            message_kind="hitl_request",
            # 关键：原样使用 notifier 投递的卡片。
            pending_action=card,
        )
    )
    await db_session.commit()
    return headers, ws_body["id"], message_id, req.id, card


@pytest.mark.asyncio
async def test_lease_card_roundtrip_structured_card_closes_request(
    client,
    db_session,
    monkeypatch,
):
    """结构化卡片（internal.py 那条 exc.pending_action 非 None 的主路径）
    必须带上 request id，并且回答后请求结案。

    Task 2 曾把 id 注入 pending_action，所以结构化分支「构造上必然覆盖」；
    Part A 把唯一写入点移到 notifier 之后，这条分支就只剩下这一个测试在守。
    """
    headers, ws_id, msg_id, req_id, card = await _lease_card_via_dispatcher(
        client,
        db_session,
        monkeypatch,
        "lease_rt_structured",
        pending_action={"kind": "needs_confirmation"},
    )
    # 确认走的确实是结构化分支，而不是 fallback（fallback 才有 input_schema）。
    assert card["kind"] == "needs_confirmation"
    assert "input_schema" not in card
    assert card["approval_request_id"] == req_id

    await _resolve_card(client, headers, ws_id, msg_id, "confirm")
    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_lease_card_roundtrip_human_input_card_closes_request(
    client,
    db_session,
    monkeypatch,
):
    """workers.py 那条不带 pending_action 的路径产出 human_input 卡片。

    这是本任务两半唯一交汇的地方：id 到达 fallback 卡片（Part A），
    且回答该卡片会关闭请求（Part B）。缺了它，human_input 那条
    `_lease_decision = "grant"` 删掉都没人发现。
    """
    headers, ws_id, msg_id, req_id, card = await _lease_card_via_dispatcher(
        client,
        db_session,
        monkeypatch,
        "lease_rt_human_input",
    )
    assert card["kind"] == "human_input"
    assert "input_schema" in card
    assert card["approval_request_id"] == req_id

    await _resolve_card(client, headers, ws_id, msg_id, "ok", note="Ready to continue.")
    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_skipping_needs_login_card_denies_request(client, db_session):
    """needs_login 的 skip 走 else 分支取消步骤 —— 请求应当 denied。"""
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client,
        db_session,
        "lease_card_login_skip",
        kind="needs_login",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "skip")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.DENIED.value


# Both approval planes consume one versioned policy; a mode change never
# resolves an already pending human decision.


async def _approval_policy_scope(db):
    from packages.core.models.user import User, UserMembership
    from tests.approval_settings_test_utils import initialize_test_approval_owners
    entity_id, actor_id = generate_ulid(), generate_ulid()
    workspace = Workspace(id=generate_ulid(), entity_id=entity_id,
        name="Policy parity", operating_model={})
    actor = User(id=actor_id, entity_id=entity_id, email=f"{actor_id}@example.test",
        password_hash="test-only", role="owner", status="active")
    db.add_all([workspace, actor, UserMembership(user_id=actor_id,
        entity_id=entity_id, role="owner", status="active", is_primary=True)])
    await db.flush()
    await initialize_test_approval_owners(db, workspace, user_id=actor_id)
    return workspace, actor


@pytest.mark.asyncio
async def test_saved_rule_is_honored_by_both_step_and_tool_planes(db_session):
    from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval
    workspace, actor = await _approval_policy_scope(db_session)
    subject = ApprovalSubject(entity_id=workspace.entity_id, workspace_id=workspace.id,
        user_id=actor.id, action_key="social_post.publish", capability_id="external.social",
        risk_level="high", kind="action")
    step_origin = ApprovalOrigin(kind="step", step_id=generate_ulid())
    tool_origin = ApprovalOrigin(kind="tool_call", conversation_id=generate_ulid(),
        args_hash="abc123", operation_id="runtime:parity:call:original")
    step_before = await resolve_approval(db_session, subject=subject, origin=step_origin)
    tool_before = await resolve_approval(db_session, subject=subject, origin=tool_origin)
    assert step_before.outcome == tool_before.outcome == "needs_human"
    assert tool_before.request is not None
    with pytest.raises(ValueError, match="versioned approval-settings"):
        await grant_approval(db_session, tool_before.request, by_user_id=actor.id,
            via="always", standing=True)
    assert tool_before.request.status == ApprovalStatus.PENDING.value

    await set_workspace_test_approval(db_session, workspace_id=workspace.id,
        entity_id=workspace.entity_id, actor_id=actor.id, mode="automatic")
    for origin, original in ((step_origin, step_before), (tool_origin, tool_before)):
        pending = await resolve_approval(db_session, subject=subject, origin=origin)
        assert pending.outcome == "needs_human"
        assert pending.request.id == original.request.id

    await set_workspace_test_approval(db_session, workspace_id=workspace.id,
        entity_id=workspace.entity_id, actor_id=actor.id, mode="confirm_changes",
        user_rules=[action_rule("social_post.publish")])
    # New operations see the saved rule, while the old requests remain pending.
    for origin in (ApprovalOrigin(kind="step", step_id=generate_ulid()),
        ApprovalOrigin(kind="tool_call", conversation_id=generate_ulid(),
            args_hash="def456", operation_id="runtime:parity:call:new")):
        result = await resolve_approval(db_session, subject=subject, origin=origin)
        assert result.outcome == "allow" and result.request is None
    assert await count_open_requests(db_session, workspace_id=workspace.id) == 2
    await grant_approval(db_session, tool_before.request, by_user_id=actor.id, via="chat_card")
    assert tool_before.request.status == ApprovalStatus.GRANTED.value
    assert await count_open_requests(db_session, workspace_id=workspace.id) == 1


@pytest.mark.asyncio
async def test_saved_allow_never_overrides_root_deny(db_session):
    from tests.approval_settings_test_utils import action_rule, set_workspace_test_approval
    from packages.core.governance.approval_policy import ApprovalScope, PolicyDraft
    from packages.core.governance.approval_policy_service import read_approval_settings, update_approval_policy
    workspace, actor = await _approval_policy_scope(db_session)
    scope = ApprovalScope(kind="account", id=workspace.entity_id, entity_id=workspace.entity_id)
    before = await read_approval_settings(db_session, scope, actor)
    await update_approval_policy(db_session, scope,
        PolicyDraft(mode="automatic", user_rules=[action_rule("billing.charge", effect="deny")]),
        before["version"], actor)
    await set_workspace_test_approval(db_session, entity_id=workspace.entity_id,
        workspace_id=workspace.id, actor_id=actor.id,
        user_rules=[action_rule("billing.charge")])
    decision = await resolve_approval(db_session, subject=ApprovalSubject(
        entity_id=workspace.entity_id, workspace_id=workspace.id, user_id=actor.id,
        action_key="billing.charge", risk_level="high", kind="action"),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()))
    assert decision.outcome == "deny" and decision.request is None
    assert await count_open_requests(db_session, workspace_id=workspace.id) == 0


@pytest.mark.asyncio
async def test_uncloseable_kind_mints_no_request(db_session, monkeypatch):
    """端点关不掉的 kind 不得铸行，否则那一行永远 PENDING。

    `workspace_operation_review` 由 internal.py 的 _pending_action_from_tool_payload
    产出，经 _NeedsHumanInput 走同一条 lease_needs_human。它在 resolve 端点里
    既不设 _lease_decision，也不 resume/cancel 步骤 —— 于是形成闭环：
    行保持 PENDING → 步骤保持 waiting_human → 计划永不 finalize →
    resolve_origin_requests（生产中唯一调用点是计划终态路径）永不触发。

    卡片照发（暂停本身仍要告知用户），只是不铸记录行。
    """
    from packages.core.workspace_chat import notifiers as chat_notify

    posted: list[dict] = []

    async def _capture(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(chat_notify, "_safe_post", _capture)

    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session,
        fx["lease_id"],
        prompt="Apply this workspace operation draft?",
        pending_action={"kind": "workspace_operation_review", "draft_id": generate_ulid()},
    )
    await db_session.flush()

    rows = (
        (await db_session.execute(select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])))
        .scalars()
        .all()
    )
    assert rows == [], "端点关不掉的 kind 不应留下 HitlRequest 行"

    # 卡片仍然发出，且不带 approval_request_id（没有行可指）。
    assert len(posted) == 1
    assert posted[0]["pending_action"]["kind"] == "workspace_operation_review"
    assert "approval_request_id" not in posted[0]["pending_action"]


def test_mint_and_close_kind_sets_are_the_same_object():
    """两端必须 import 同一个对象，而不是两份内容相同的字面量。

    C1 的根因就是 mint 端按开放集合、resolve 端按封闭集合 —— 只要还有第二
    份字面量，加第五种 kind 时就会重新漂移。
    """
    from apps.api.routers import workspace_chat as chat_router
    from packages.core.ai.pending_action import LEASE_HITL_CLOSEABLE_KINDS
    from packages.core.dispatcher import service as dispatcher_service

    assert dispatcher_service.LEASE_HITL_CLOSEABLE_KINDS is LEASE_HITL_CLOSEABLE_KINDS
    assert chat_router.LEASE_HITL_CLOSEABLE_KINDS is LEASE_HITL_CLOSEABLE_KINDS
    # 集合内容就是所有 path C kind，包括外部副作用核对。
    assert LEASE_HITL_CLOSEABLE_KINDS == frozenset(
        {
            "effect_reconciliation",
            "human_input",
            "needs_input",
            "needs_confirmation",
            "needs_login",
        }
    )
