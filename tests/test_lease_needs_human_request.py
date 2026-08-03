"""Phase 1: lease_needs_human 接入统一 HitlRequest 记录层。

路径 C（执行中暂停）此前直接写 chat Message.pending_action，不留记录 —
无法 join 回 task/step，也无法复用去重。这些测试钉住接线后的行为。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.core.constants.approvals import ApprovalStatus
from packages.core.constants.execution import WorkLeaseStatus
from packages.core.dispatcher.service import Dispatcher
from packages.core.governance import WorkspacePolicy, get_policy, update_policy
from packages.core.governance.approvals import (
    ApprovalOrigin,
    ApprovalSubject,
    count_open_requests,
    dedup_key_for,
    grant_approval,
    resolve_approval,
)
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.task import Conversation, Message
from packages.core.models.worker import Worker, WorkLease
from packages.core.models.workspace import Workspace


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
        kind="lease", lease_id=lease_id, step_id=generate_ulid(),
        context={"pending_kind": "needs_confirmation"},
    )
    assert dedup_key_for(_subject(entity_id), origin) == (
        f"lease:{lease_id}:needs_confirmation"
    )


def test_lease_origin_same_lease_different_kind_differs():
    """同一租约因不同原因暂停 → 各自独立请求。"""
    entity_id = generate_ulid()
    lease_id = generate_ulid()
    step_id = generate_ulid()
    confirm = ApprovalOrigin(
        kind="lease", lease_id=lease_id, step_id=step_id,
        context={"pending_kind": "needs_confirmation"},
    )
    login = ApprovalOrigin(
        kind="lease", lease_id=lease_id, step_id=step_id,
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
            kind="lease", lease_id=lease_id, step_id=step_id,
            context={"pending_kind": "needs_confirmation"},
        )

    assert dedup_key_for(_subject(entity_id), _origin()) == dedup_key_for(_subject(entity_id), _origin())


def test_lease_origin_without_pending_kind_falls_back():
    """pending_kind 缺失时仍须产出稳定的 lease-scoped key，不得落回 tool: 分支。"""
    entity_id = generate_ulid()
    lease_id = generate_ulid()
    origin = ApprovalOrigin(kind="lease", lease_id=lease_id, step_id=generate_ulid())
    assert dedup_key_for(_subject(entity_id), origin) == f"lease:{lease_id}:human_input"


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
        db.add(Workspace(
            id=workspace_id, entity_id=entity_id, name="Lease WS", operating_model={},
        ))

    worker_id = generate_ulid()
    db.add(Worker(
        id=worker_id, entity_id=entity_id, kind="internal",
        display_name="W", trust_level="trusted", status="active",
        capabilities={"supported_kinds": ["subagent"]}, consecutive_failures=0,
    ))

    plan_id = generate_ulid()
    task_id = generate_ulid()
    db.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        task_id=task_id, plan_dag={}, status="running",
    ))

    step_id = generate_ulid()
    db.add(ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        step_key="publish_via_chrome", kind="subagent", params={},
        step_status="running", attempt_count=1, max_attempts=3,
        risk_level="medium", capability_id="browser.control",
    ))

    lease_id = generate_ulid()
    db.add(WorkLease(
        id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
        workspace_id=workspace_id, worker_id=worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        # lease_until is NOT NULL on the model — omitting it fails the insert.
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    ))
    await db.flush()
    return {
        "entity_id": entity_id, "workspace_id": workspace_id,
        "plan_id": plan_id, "task_id": task_id,
        "step_id": step_id, "lease_id": lease_id,
    }


@pytest.mark.asyncio
async def test_lease_needs_human_mints_request(db_session):
    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session, fx["lease_id"],
        prompt="Confirm the LinkedIn post before publishing",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.flush()

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()
    assert req.status == ApprovalStatus.PENDING.value
    assert req.origin_kind == "lease"
    assert req.dedup_key == f"lease:{fx['lease_id']}:needs_confirmation"


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind,expected_hitl_type", [
    ("needs_confirmation", "authorize"),
    ("needs_login", "input"),
    ("needs_input", "input"),
    ("human_input", "input"),
])
async def test_lease_pause_records_what_it_actually_asks_for(
    db_session, pending_kind, expected_hitl_type,
):
    """路径 C 的暂停必须写下自己是「要信息」还是「要许可」。

    记录层的 ``hitl_type`` 默认是 ``authorize``，而 lease_needs_human 自己的
    注释写着 "the human is being asked for information, not permission" ——
    默认值把这句话反过来存进了库。四个读取面都以 ``hitl_type`` 为判据，所以
    这一条错了，CAPTCHA 墙就会继续以「治理审批」的身份排队变老。
    """
    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session, fx["lease_id"], prompt="Please help",
        pending_action={"kind": pending_kind},
    )
    await db_session.flush()
    db_session.expire_all()

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()
    assert req.hitl_type == expected_hitl_type
    assert req.is_governance() is (expected_hitl_type == "authorize")


@pytest.mark.asyncio
async def test_lease_request_joins_back_to_task_and_plan(db_session):
    """§3.3 验收：每一次暂停都能 join 回具体 task/step。"""
    fx = await _lease_fixture(db_session)
    await Dispatcher().lease_needs_human(
        db_session, fx["lease_id"], prompt="Need input",
        pending_action={"kind": "needs_input"},
    )
    await db_session.flush()
    # Expire first: without it the select returns the identity-mapped instance
    # and the asserts would only restate in-memory state, not persisted columns.
    db_session.expire_all()

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()
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
        lease = (await db_session.execute(
            select(WorkLease).where(WorkLease.id == fx["lease_id"])
        )).scalar_one()
        lease.status = WorkLeaseStatus.ACTIVE.value
        await db_session.flush()

        await svc.lease_needs_human(
            db_session, fx["lease_id"], prompt="Confirm",
            pending_action={"kind": "needs_confirmation"},
        )
        await db_session.flush()

    rows = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalars().all()
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
        db_session, fx["lease_id"], prompt="Confirm",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.flush()

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()
    await grant_approval(db_session, req, by_user_id=generate_ulid(), via="chat_card")
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
        db_session, fx["lease_id"], prompt="Confirm",
        pending_action={"kind": "needs_confirmation"},
    )
    await db_session.flush()

    closed = await resolve_origin_requests(db_session, plan_id=fx["plan_id"])
    await db_session.flush()
    assert closed == 1

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()
    assert req.status == ApprovalStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_fallback_card_without_pending_action_carries_request_id(
    db_session, monkeypatch,
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
        db_session, fx["lease_id"], prompt="Solve the CAPTCHA",
        # 无 pending_action —— 正是 workers.py 路由的调用形态。
    )
    await db_session.flush()

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()

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


async def _seed_lease_card(client, db_session, username: str, *, kind: str):
    """A workspace + waiting_human step + PENDING request + the chat card
    that points at it. Returns everything the resolve endpoint needs."""
    headers = await _register(client, username)
    ws = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Lease Card WS"},
    )
    ws_body = ws.json()
    entity_id, workspace_id = ws_body["entity_id"], ws_body["id"]

    plan_id, task_id, step_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        task_id=task_id, plan_dag={}, status="running",
    ))
    db_session.add(ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        step_key="publish_via_chrome", kind="subagent", params={},
        step_status="waiting_human", attempt_count=1, max_attempts=3,
        risk_level="medium", capability_id="browser.control",
    ))

    request_id = generate_ulid()
    db_session.add(HitlRequest(
        id=request_id, entity_id=entity_id, workspace_id=workspace_id,
        capability_id="browser.control", risk_level="medium",
        origin_kind="lease", origin_step_id=step_id, origin_plan_id=plan_id,
        origin_task_id=task_id, status=ApprovalStatus.PENDING.value,
        dedup_key=f"lease:{generate_ulid()}:{kind}",
        reason="Confirm before publishing",
    ))

    conv_id, message_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=conv_id, entity_id=entity_id, workspace_id=workspace_id,
        title="Lease HITL", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Message(
        id=message_id, conversation_id=conv_id, role="assistant",
        content="Confirm before publishing", author_kind="agent",
        message_kind="hitl_request",
        pending_action={
            "kind": kind,
            "step_id": step_id,
            "plan_id": plan_id,
            "approval_request_id": request_id,
        },
    ))
    await db_session.commit()
    return headers, workspace_id, message_id, request_id


async def _resolve_card(client, headers, workspace_id, message_id, choice: str):
    resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/chat/messages/{message_id}/resolve",
        headers=headers,
        json={"choice": choice},
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _reload_request(db_session, request_id: str) -> HitlRequest:
    db_session.expire_all()
    return (await db_session.execute(
        select(HitlRequest).where(HitlRequest.id == request_id)
    )).scalar_one()


@pytest.mark.asyncio
async def test_resolving_lease_card_closes_request_end_to_end(client, db_session):
    """路径 C 卡片被确认 → 请求 granted。

    这条走真实的 /resolve 端点，因为要钉的是路由里那段新代码本身；
    直接调 grant_approval 只能证明 governance 函数好用，证明不了路由会调它。
    """
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client, db_session, "lease_card_close", kind="needs_confirmation",
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
async def test_cancelling_lease_card_denies_request(client, db_session):
    """取消必须 deny，不能 grant。

    关闭逻辑跑在 if/elif 链之前，那里还没解释 choice —— 若无条件 grant，
    用户明确拒绝的操作会留下一条 live 的批准（_find_open_request 视
    granted-unconsumed 为存活），下一次 gate 直接放行。这是 fail-open。
    本用例钉住那份「与下方分支同步的」词表。
    """
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client, db_session, "lease_card_cancel", kind="needs_confirmation",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "cancel")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.DENIED.value
    assert req.decided_via == "chat_card"
    assert req.decided_at is not None


@pytest.mark.asyncio
async def test_skipping_needs_input_card_denies_request(client, db_session):
    """needs_input 的 skip 走 else 分支取消步骤 —— 请求同样应当 denied。"""
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client, db_session, "lease_card_skip", kind="needs_input",
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
        client, db_session, "lease_card_signin", kind="needs_login",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "sign_in")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.PENDING.value
    assert req.decided_at is None


@pytest.mark.asyncio
async def test_continue_after_login_grants_request(client, db_session):
    """两段式登录的后半段：cookies 已捕获、步骤重试 → 此时才结案。"""
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client, db_session, "lease_card_after_login", kind="needs_login",
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
    db_session.add(Worker(
        id=worker_id, entity_id=entity_id, kind="internal",
        display_name="W", trust_level="trusted", status="active",
        capabilities={"supported_kinds": ["subagent"]}, consecutive_failures=0,
    ))

    plan_id, step_id, lease_id = generate_ulid(), generate_ulid(), generate_ulid()
    db_session.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=None,
        task_id=generate_ulid(), plan_dag={}, status="running",
    ))
    db_session.add(ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id, workspace_id=None,
        step_key="headless_step", kind="subagent", params={},
        step_status="running", attempt_count=1, max_attempts=3,
        risk_level="medium", capability_id="browser.control",
    ))
    db_session.add(WorkLease(
        id=lease_id, step_id=step_id, plan_id=plan_id, entity_id=entity_id,
        workspace_id=None, worker_id=worker_id,
        status=WorkLeaseStatus.ACTIVE.value,
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    ))
    await db_session.flush()

    # 若预初始化被删，这一行抛 UnboundLocalError。
    lease = await Dispatcher().lease_needs_human(
        db_session, lease_id, prompt="Solve the CAPTCHA",
    )
    await db_session.flush()

    assert lease.status == WorkLeaseStatus.NEEDS_HUMAN.value
    # 无 workspace ⇒ 无卡片可渲染 ⇒ 不铸请求。
    rows = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == step_id)
    )).scalars().all()
    assert rows == []


async def _lease_card_via_dispatcher(
    client, db_session, monkeypatch, username: str, *, pending_action=None,
):
    """跑完整链路：dispatcher → notifier → 卡片 → 落库，返回可被端点消费的消息。

    与 _seed_lease_card 的差别正是本组用例存在的理由：pending_action 不是手写
    的，而是 notifier 实际投递的那一份。手写卡片会跳过 notifier，于是
    「结构化分支有没有把 approval_request_id 合进去」这个问题根本没被问到。
    """
    from packages.core.workspace_chat import notifiers as chat_notify

    headers = await _register(client, username)
    ws = await client.post(
        "/api/v1/workspaces", headers=headers, json={"name": "Lease Roundtrip WS"},
    )
    ws_body = ws.json()
    fx = await _lease_fixture(
        db_session, entity_id=ws_body["entity_id"], workspace_id=ws_body["id"],
    )

    posted: list[dict] = []

    async def _capture(**kwargs):
        posted.append(kwargs)

    monkeypatch.setattr(chat_notify, "_safe_post", _capture)
    await Dispatcher().lease_needs_human(
        db_session, fx["lease_id"], prompt="Confirm the publish",
        pending_action=pending_action,
    )
    await db_session.flush()
    assert len(posted) == 1, "needs_human 应当恰好投递一张卡片"
    card = posted[0]["pending_action"]

    req = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalar_one()

    conv_id, message_id = generate_ulid(), generate_ulid()
    db_session.add(Conversation(
        id=conv_id, entity_id=ws_body["entity_id"], workspace_id=ws_body["id"],
        title="Lease HITL", channel="workspace", scope="workspace_main",
    ))
    db_session.add(Message(
        id=message_id, conversation_id=conv_id, role="assistant",
        content="Confirm the publish", author_kind="agent",
        message_kind="hitl_request",
        # 关键：原样使用 notifier 投递的卡片。
        pending_action=card,
    ))
    await db_session.commit()
    return headers, ws_body["id"], message_id, req.id, card


@pytest.mark.asyncio
async def test_lease_card_roundtrip_structured_card_closes_request(
    client, db_session, monkeypatch,
):
    """结构化卡片（internal.py 那条 exc.pending_action 非 None 的主路径）
    必须带上 request id，并且回答后请求结案。

    Task 2 曾把 id 注入 pending_action，所以结构化分支「构造上必然覆盖」；
    Part A 把唯一写入点移到 notifier 之后，这条分支就只剩下这一个测试在守。
    """
    headers, ws_id, msg_id, req_id, card = await _lease_card_via_dispatcher(
        client, db_session, monkeypatch, "lease_rt_structured",
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
    client, db_session, monkeypatch,
):
    """workers.py 那条不带 pending_action 的路径产出 human_input 卡片。

    这是本任务两半唯一交汇的地方：id 到达 fallback 卡片（Part A），
    且回答该卡片会关闭请求（Part B）。缺了它，human_input 那条
    `_lease_decision = "grant"` 删掉都没人发现。
    """
    headers, ws_id, msg_id, req_id, card = await _lease_card_via_dispatcher(
        client, db_session, monkeypatch, "lease_rt_human_input",
    )
    assert card["kind"] == "human_input"
    assert "input_schema" in card
    assert card["approval_request_id"] == req_id

    await _resolve_card(client, headers, ws_id, msg_id, "ok")
    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.CONSUMED.value


@pytest.mark.asyncio
async def test_skipping_needs_login_card_denies_request(client, db_session):
    """needs_login 的 skip 走 else 分支取消步骤 —— 请求应当 denied。"""
    headers, ws_id, msg_id, req_id = await _seed_lease_card(
        client, db_session, "lease_card_login_skip", kind="needs_login",
    )
    await _resolve_card(client, headers, ws_id, msg_id, "skip")

    req = await _reload_request(db_session, req_id)
    assert req.status == ApprovalStatus.DENIED.value


# ── "Always" 跨路径守卫 ────────────────────────────────────────────
#
# 步骤门禁 (governance/approvals.resolve_approval) 与聊天工具调用
# (ai/runtime/approval_service.guard_runtime_tool_action) 共用 workspace
# policy 的 auto_approve 集合作为唯一 standing 存储。这只是代码注释里的
# 约定，没有任何结构强制 —— PR #289 正是死在某条分支自己读了另一份偏好
# 存储上，用户于是永远批不完同一个动作。下面两条测试把该性质钉死。


@pytest.mark.asyncio
async def test_always_grant_is_honored_by_both_step_and_tool_planes(db_session):
    """工作区内点 "Always" 后，步骤门禁与聊天工具调用两条路径都不再要求审批。

    这两条路径共用 workspace policy 作为唯一 standing 存储（PR #326/#328）。
    该性质是约定而非结构强制，故用测试钉死 —— PR #289 正是死在某条分支
    不读 Always 上。
    """
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(Workspace(
        id=workspace_id, entity_id=entity_id, name="Always WS", operating_model={},
    ))
    await db_session.flush()

    def _subj():
        return ApprovalSubject(
            entity_id=entity_id, workspace_id=workspace_id,
            action_key="social_post.publish", capability_id="external.social",
            risk_level="high", kind="action",
        )

    # 前置：工作区把该动作设为 HITL。这一步是为了让「工具调用」路径的
    # 授予前断言真的有意义 —— 工作区内的工具调用不吃 high_risk 内在触发
    # （那是 step-origin 专属），只有策略 HITL 规则会拦它。少了这条规则，
    # tool_before 会因为「本来就不需要审批」而假通过。
    await update_policy(
        db_session, entity_id=entity_id, workspace_id=workspace_id,
        policy=WorkspacePolicy(hitl_required_actions=["social_post.publish"]),
        changed_by=generate_ulid(), change_summary="require approval",
    )
    await db_session.flush()

    # 授予前：两条路径都要求人工介入
    step_before = await resolve_approval(
        db_session, subject=_subj(),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
    assert step_before.outcome == "needs_human"

    tool_before = await resolve_approval(
        db_session, subject=_subj(),
        origin=ApprovalOrigin(
            kind="tool_call", conversation_id=generate_ulid(), args_hash="abc123",
        ),
    )
    assert tool_before.outcome == "needs_human"
    assert tool_before.request is not None

    # 用户在聊天卡片上点 "Always" —— standing=True 把同意写进 standing 存储
    await grant_approval(
        db_session, tool_before.request,
        by_user_id=generate_ulid(), via="always", standing=True,
    )
    await db_session.flush()

    # 唯一 standing 存储 = workspace policy 的 auto_approve 集合
    policy = await get_policy(db_session, workspace_id)
    assert "social_post.publish" in policy.auto_approve_actions

    # 授予后：步骤门禁放行。用新的 step_id / 新的 conversation+args，
    # 这样放行只可能来自 standing 存储，不可能是上面那条一次性 grant 记录。
    step_after = await resolve_approval(
        db_session, subject=_subj(),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
    assert step_after.outcome == "allow"

    # 授予后：聊天工具调用路径同样放行（同一 standing 存储）
    tool_after = await resolve_approval(
        db_session, subject=_subj(),
        origin=ApprovalOrigin(
            kind="tool_call", conversation_id=generate_ulid(), args_hash="def456",
        ),
    )
    assert tool_after.outcome == "allow"

    # 放行来自 standing 存储本身，两次 resolve 都没有再挂卡片。
    assert step_after.request is None
    assert tool_after.request is None
    # 仍开着的只有授予前 step 那一张（"Always" 点在聊天卡片上，不会回头
    # 关闭别的 origin 已经挂出的卡；那张由 resolve_origin_requests 在
    # step 终态时清理）。数值锁死在 1 ⇒ 授予后的两次 resolve 一张没新增。
    assert await count_open_requests(db_session, workspace_id=workspace_id) == 1


@pytest.mark.asyncio
async def test_always_grant_never_overrides_hard_block(db_session):
    """never_allow 是硬阻断：任何 standing grant 都不得放行。"""
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(Workspace(
        id=workspace_id, entity_id=entity_id, name="Block WS", operating_model={},
    ))
    await db_session.flush()

    await update_policy(
        db_session, entity_id=entity_id, workspace_id=workspace_id,
        policy=WorkspacePolicy(
            never_allow_actions=["billing.*"],
            auto_approve_actions=["billing.*"],  # 同时 always：硬阻断仍须获胜
        ),
        changed_by=generate_ulid(), change_summary="conflicting rules",
    )
    await db_session.flush()

    decision = await resolve_approval(
        db_session,
        subject=ApprovalSubject(
            entity_id=entity_id, workspace_id=workspace_id,
            action_key="billing.charge", risk_level="high", kind="action",
        ),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
    assert decision.outcome == "deny"
    assert decision.request is None  # 硬阻断不 mint 记录
    assert await count_open_requests(db_session, workspace_id=workspace_id) == 0


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
        db_session, fx["lease_id"], prompt="Apply this workspace operation draft?",
        pending_action={"kind": "workspace_operation_review", "draft_id": generate_ulid()},
    )
    await db_session.flush()

    rows = (await db_session.execute(
        select(HitlRequest).where(HitlRequest.origin_step_id == fx["step_id"])
    )).scalars().all()
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
    # 集合内容就是四种 path C kind。
    assert LEASE_HITL_CLOSEABLE_KINDS == frozenset({
        "human_input", "needs_input", "needs_confirmation", "needs_login",
    })
