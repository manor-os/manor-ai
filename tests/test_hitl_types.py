"""Phase 2:HITL 类型体系。

记录层此前只承载 approve/deny 一种语义。加 hitl_type 后,同一张表要能
表达"要信息"(input)、"要审核"(review)、"要授权"(authorize)、
"要选择"(choice)、"出错了"(error)五种,且每种的文案必须回答
这是什么 / 为什么 / 你该做什么。
"""
import pytest
from sqlalchemy import select

from packages.core.constants.approvals import HitlType
from packages.core.models.hitl_request import HitlRequest
from packages.core.models.base import generate_ulid
from packages.core.models.execution import ExecutionStep


def test_hitl_type_vocabulary_is_closed():
    assert HitlType.values() == [
        "input", "review", "authorize", "choice", "error",
    ]


@pytest.mark.asyncio
async def test_approval_request_defaults_to_authorize(db_session):
    """回填语义:现存所有行本质都是 authorize,新行不指定时同此。"""
    req = HitlRequest(
        id=generate_ulid(), entity_id=generate_ulid(),
        risk_level="medium", origin_kind="step",
        dedup_key=f"step:{generate_ulid()}",
    )
    db_session.add(req)
    await db_session.flush()
    assert req.hitl_type == HitlType.AUTHORIZE.value
    assert req.payload == {}


@pytest.mark.asyncio
async def test_execution_step_last_execution_error_is_separate_from_error(db_session):
    """两个字段必须能同时持有不同内容 —— 这正是事故的修复点:
    error 承载"当前门禁原因",last_execution_error 承载"上次真实失败"。"""
    from packages.core.models.execution import ExecutionPlan

    entity_id = generate_ulid()
    plan_id = generate_ulid()
    db_session.add(ExecutionPlan(
        id=plan_id, entity_id=entity_id, plan_dag={}, status="running",
    ))
    step_id = generate_ulid()
    step = ExecutionStep(
        id=step_id, plan_id=plan_id, entity_id=entity_id,
        step_key="publish", kind="subagent", params={},
        step_status="waiting_human", attempt_count=1, max_attempts=3,
        risk_level="high",
    )
    step.last_execution_error = {
        "type": "StepResultFailed",
        "message": "no paired local worker online",
    }
    step.error = {"type": "StepApprovalRequired", "message": "needs approval"}
    db_session.add(step)
    await db_session.flush()
    await db_session.refresh(step)

    assert step.error["type"] == "StepApprovalRequired"
    assert step.last_execution_error["message"] == "no paired local worker online"


@pytest.mark.asyncio
async def test_mint_rejects_incomplete_payload(db_session):
    """缺必填字段直接抛错 —— 不允许创建"说不清自己是什么"的卡片。"""
    from packages.core.governance.approvals import (
        ApprovalOrigin, ApprovalSubject, mint_approval_request,
    )

    with pytest.raises(ValueError, match="action_to_take"):
        await mint_approval_request(
            db_session,
            subject=ApprovalSubject(entity_id=generate_ulid(), risk_level="high"),
            origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
            hitl_type=HitlType.ERROR.value,
            payload={"what_happened": "CLI offline", "why": "daemon not running"},
        )


@pytest.mark.asyncio
async def test_mint_accepts_complete_error_payload(db_session):
    from packages.core.governance.approvals import (
        ApprovalOrigin, ApprovalSubject, mint_approval_request,
    )

    req = await mint_approval_request(
        db_session,
        subject=ApprovalSubject(entity_id=generate_ulid(), risk_level="high"),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
        hitl_type=HitlType.ERROR.value,
        payload={
            "what_happened": "Chrome control unavailable",
            "why": "no paired local worker online",
            "action_to_take": "Reconnect the local worker on your laptop",
        },
    )
    assert req.hitl_type == "error"
    assert req.payload["action_to_take"]


@pytest.mark.asyncio
async def test_mint_defaults_to_authorize_with_empty_payload(db_session):
    """不传 type 时保持既有行为 —— 既有调用点无需全部改造。"""
    from packages.core.governance.approvals import (
        ApprovalOrigin, ApprovalSubject, mint_approval_request,
    )

    req = await mint_approval_request(
        db_session,
        subject=ApprovalSubject(entity_id=generate_ulid(), risk_level="medium"),
        origin=ApprovalOrigin(kind="step", step_id=generate_ulid()),
    )
    assert req.hitl_type == "authorize"
