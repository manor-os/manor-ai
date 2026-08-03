"""事故修复:真实失败原因必须活到用户面前。

2026-07-30:操作者批准 15 次,始终看不到真实原因(本地 worker 守护进程离线)。
后端在重新过审批门禁时把 step.error 整体覆盖成审批模板,冲掉了它。
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from packages.core.dispatcher.service import Dispatcher
from packages.core.governance.approvals import grant_approval
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionPlan, ExecutionStep
from packages.core.models.worker import SubscriptionWorker, Worker
from packages.core.models.workspace import Agent, AgentSubscription, Workspace



@pytest.mark.asyncio
async def test_gate_write_does_not_destroy_last_execution_error(db_session):
    """门禁覆盖 step.error 是对的(它表达"当前为何被拦"),
    但不得触碰 last_execution_error。"""
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    db_session.add(Workspace(
        id=workspace_id, entity_id=entity_id, name="WS", operating_model={},
    ))
    plan_id = generate_ulid()
    db_session.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        plan_dag={}, status="running", task_id=generate_ulid(),
    ))
    step_id = generate_ulid()
    db_session.add(ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id, workspace_id=workspace_id,
        step_key="publish_via_chrome", kind="subagent", params={},
        step_status="pending", attempt_count=1, max_attempts=3, risk_level="high",
        last_execution_error={
            "type": "StepResultFailed",
            "message": "the Chrome MCP reported no paired local worker online.",
        },
    ))
    await db_session.flush()

    step = (await db_session.execute(
        select(ExecutionStep).where(ExecutionStep.id == step_id)
    )).scalar_one()
    step.error = {"type": "StepApprovalRequired", "message": "High-risk step needs approval."}
    await db_session.flush()
    await db_session.refresh(step)

    assert step.error["type"] == "StepApprovalRequired"
    assert "no paired local worker" in step.last_execution_error["message"], (
        "真实失败原因被门禁写入冲掉了 —— 这正是事故根因"
    )


# ── 真正要盯住的那条:真实的 fail_lease → 真实的门禁 ───────────────────

async def _gated_scenario(db):
    """一个需要审批、能被真 worker 领走的 workspace step。

    结构照搬 tests/test_dispatcher_unified_approval_gate.py 的 _scenario:
    只有完整的 workspace/agent/subscription/worker 链条,被批准的步骤才
    真的会被派发出去(否则测不到 fail_lease 之后的那次重新过闸)。
    """
    entity_id = generate_ulid()
    workspace_id = generate_ulid()
    plan_id = generate_ulid()
    step_id = generate_ulid()
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
            status="running", execution_mode="live",
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
    return {"entity_id": entity_id, "step_id": step_id, "worker": worker}


async def _open_request(db, entity_id):
    return (await db.execute(
        select(HitlRequest).where(
            HitlRequest.entity_id == entity_id,
            HitlRequest.status == "pending",
        )
    )).scalars().first()


async def _noop(**_kwargs):
    return None




@pytest.mark.asyncio
async def test_expired_lease_also_records_the_real_failure(db_session, monkeypatch):
    """租约超时是 fail_lease 的孪生路径:worker 死了没回报,同样是真实
    执行失败,步骤同样会回到队列重新撞门禁。"""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(
        "packages.core.governance.service.post_hitl_card", _noop,
    )
    s = await _gated_scenario(db_session)
    dispatcher = Dispatcher()

    await dispatcher.checkout_steps_for_worker(db_session, s["worker"], max_n=1)
    request = await _open_request(db_session, s["entity_id"])
    await grant_approval(db_session, request, by_user_id="operator", via="chat_card")
    step = await db_session.get(ExecutionStep, s["step_id"])
    step.step_status = "pending"
    step.error = None
    step.current_lease_id = None
    await db_session.flush()

    lease, _ = (await dispatcher.checkout_steps_for_worker(
        db_session, s["worker"], max_n=1,
    ))[0]
    lease.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    assert await dispatcher.expire_leases(db_session) == 1
    step = await db_session.get(ExecutionStep, s["step_id"])
    assert step.last_execution_error["type"] == "lease_expired"


# ── Task 4:已知错误签名 → 可执行建议 ─────────────────────────────────



def test_unknown_error_returns_none():
    from packages.core.governance.error_signatures import classify_execution_error

    assert classify_execution_error({
        "type": "StepResultFailed", "message": "something nobody has seen before",
    }) is None


def test_classify_tolerates_missing_and_malformed_input():
    from packages.core.governance.error_signatures import classify_execution_error

    assert classify_execution_error(None) is None
    assert classify_execution_error({}) is None
    assert classify_execution_error({"message": None}) is None




@pytest.mark.asyncio
async def test_unrecognized_failure_falls_back_to_the_raw_message(
    db_session, monkeypatch,
):
    """签名表认不出来时,卡片仍然是 error 类型,而且照抄原文 ——
    宁可给不出修法,也不能把真实原因换成「需要批准」。"""
    from packages.core.constants.approvals import ApprovalStatus, HitlType

    monkeypatch.setattr(
        "packages.core.governance.service.post_hitl_card", _noop,
    )
    s = await _gated_scenario(db_session)
    dispatcher = Dispatcher()

    step = await db_session.get(ExecutionStep, s["step_id"])
    step.attempt_count = 1
    step.last_execution_error = {
        "type": "StepResultFailed",
        "message": "upstream returned 503 from a service nobody has a runbook for",
    }
    await db_session.flush()

    assert await dispatcher.checkout_steps_for_worker(
        db_session, s["worker"], max_n=1,
    ) == []

    req = (await db_session.execute(
        select(HitlRequest).where(
            HitlRequest.origin_step_id == s["step_id"],
            HitlRequest.status == ApprovalStatus.PENDING.value,
        )
    )).scalars().one()
    assert req.hitl_type == HitlType.ERROR.value
    assert "503" in req.payload["what_happened"]
    assert req.payload["action_link"] is None
    assert req.payload["is_transient"] is False


# ── Task 5:瞬时失败退避,不烧重试次数 ───────────────────────────────

async def _requests_for_step(db, step_id):
    return (await db.execute(
        select(HitlRequest)
        .where(HitlRequest.origin_step_id == step_id)
        .order_by(HitlRequest.created_at.asc())
    )).scalars().all()


def _fast_forward_backoff(step):
    """等价于「退避时间到了」—— 把 next_retry_at 摘掉。

    checkout 内部自己取 now(),没法注入时钟,所以只能从这一头拨。"""
    step.error = {
        k: v for k, v in (step.error or {}).items() if k != "next_retry_at"
    }


async def _approved_and_dispatched(db, dispatcher, s):
    """跑到「人已批准、步骤已派发」这一刻,返回租约。"""
    assert await dispatcher.checkout_steps_for_worker(db, s["worker"], max_n=1) == []
    request = await _open_request(db, s["entity_id"])
    await grant_approval(db, request, by_user_id="operator", via="chat_card")
    step = await db.get(ExecutionStep, s["step_id"])
    step.step_status = "pending"
    step.error = None
    step.human_input_prompt = None
    step.current_lease_id = None
    await db.flush()
    leased = await dispatcher.checkout_steps_for_worker(db, s["worker"], max_n=1)
    assert len(leased) == 1
    return leased[0][0]




@pytest.mark.asyncio
async def test_non_transient_failure_still_burns_an_attempt(db_session, monkeypatch):
    """退避只给认得出来的瞬时失败。认不出来的照旧扣次数 ——
    否则这就成了一套绕过重试预算的后门。"""
    monkeypatch.setattr("packages.core.governance.service.post_hitl_card", _noop)
    s = await _gated_scenario(db_session)
    dispatcher = Dispatcher()

    lease = await _approved_and_dispatched(db_session, dispatcher, s)
    await dispatcher.fail_lease(db_session, lease.id, error={
        "type": "StepResultFailed",
        "message": "something nobody has seen before",
    })
    step = await db_session.get(ExecutionStep, s["step_id"])
    assert step.attempt_count == 1
    assert "transient_retry" not in step.last_execution_error


def test_transient_window_expires_even_when_the_count_has_not():
    """6 次和 30 分钟是「先到者为准」。队列被压住时,次数可能很久
    都用不完 —— 那也不能让用户干等半小时以上还看不到卡片。"""
    from datetime import datetime, timedelta, timezone

    from packages.core.services.retry_policy import (
        TRANSIENT_MAX_WINDOW_SECONDS,
        advance_transient_retry,
    )

    started = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fresh = advance_transient_retry(None, now=started)
    assert fresh.count == 1 and fresh.exhausted is False

    marker = {"transient_retry": fresh.as_marker()}
    still_ok = advance_transient_retry(marker, now=started + timedelta(minutes=29))
    assert still_ok.count == 2 and still_ok.exhausted is False

    too_late = advance_transient_retry(
        marker, now=started + timedelta(seconds=TRANSIENT_MAX_WINDOW_SECONDS),
    )
    assert too_late.exhausted is True, "超过 30 分钟就该交给人"


def test_escalated_streak_starts_over():
    """升级给人之后,streak 归零 —— 人已经介入过了,那是两段不同的
    连续失败。每次重新开始都仍然要一次人工决定,不会变成静默死循环。"""
    from datetime import datetime, timezone

    from packages.core.services.retry_policy import advance_transient_retry

    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    spent = {"transient_retry": {
        "count": 7, "first_failed_at": now.isoformat(), "exhausted": True,
    }}
    assert advance_transient_retry(spent, now=now).count == 1
