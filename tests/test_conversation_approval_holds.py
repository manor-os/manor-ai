"""A conversation hold turns what the user asked to review into an ask, and nothing else."""
from types import SimpleNamespace
from typing import get_args

import pytest

from packages.core.governance.approval_policy import ActionFacts, ApprovalCategory
from packages.core.governance.policy import PolicyDecision


def _db(holds, said=()):
    async def get(model, key, **_kwargs):
        return SimpleNamespace(id=key, entity_id="E1", meta={"approval_holds": holds})

    async def execute(_query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(said)))

    return SimpleNamespace(get=get, execute=execute)


@pytest.mark.asyncio
async def test_a_held_category_asks_where_the_mode_would_allow():
    """"Writes to Knowledge need my approval" was words only: risk-based mode wrote silently."""
    from packages.core.governance.service import _apply_conversation_holds

    facts = ActionFacts(action_key="workspace.file.create", category="files", impact="reversible")
    decision = await _apply_conversation_holds(
        _db([{"category": "files"}]), PolicyDecision(True), conversation_id="C1", facts=facts,
    )
    assert decision.allowed is False and decision.pause_for_hitl is True
    assert decision.matched_rule == "conversation_hold:files"
    assert decision.explanation["scope"] == {"kind": "conversation", "id": "C1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("before", [
    PolicyDecision(True),
    PolicyDecision(False, True, "approval.mode", "approval.mode"),
    PolicyDecision(False, False, "approval.rule", "deny-rule"),
])
async def test_holds_only_tighten(before):
    from packages.core.governance.service import _apply_conversation_holds

    holds = [{"category": category} for category in get_args(ApprovalCategory)]
    for category in get_args(ApprovalCategory):
        facts = ActionFacts(action_key="any.action", category=category, impact="reversible")
        after = await _apply_conversation_holds(_db(holds), before, conversation_id="C1", facts=facts)
        if not before.allowed:
            assert after is before  # an ask or deny is never loosened or rewritten
        else:
            # Held categories ask; unholdable ones (read, unknown) keep the decision.
            assert after.allowed is (category in {"read", "unknown"})
            assert after.allowed or after.pause_for_hitl  # a hold never denies


@pytest.mark.asyncio
async def test_no_conversation_or_no_hold_changes_nothing():
    from packages.core.governance.service import _apply_conversation_holds

    facts = ActionFacts(action_key="workspace.file.create", category="files", impact="reversible")
    allow = PolicyDecision(True)
    assert await _apply_conversation_holds(_db([{"category": "files"}]), allow,
                                           conversation_id=None, facts=facts) is allow
    assert await _apply_conversation_holds(_db([{"category": "publishing"}]), allow,
                                           conversation_id="C1", facts=facts) is allow


@pytest.mark.asyncio
async def test_the_hold_action_refuses_unknown_categories_and_needs_a_conversation():
    import json

    from packages.core.ai.tools.workspace_agent_tools import _workspace_hold_for_approval_handler

    unknown = json.loads(await _workspace_hold_for_approval_handler(
        entity_id="E1", conversation_id="C1", categories=["read"],
    ))
    assert unknown["error"] == "invalid_categories" and unknown["unknown"] == ["read"]
    alone = json.loads(await _workspace_hold_for_approval_handler(entity_id="E1", categories=["files"]))
    assert alone["error"] == "conversation_required"


def test_setting_a_hold_needs_no_approval_of_its_own():
    from packages.core.ai.runtime.approval_classifier import classify_runtime_tool

    classification = classify_runtime_tool(
        "workspace_agent", {"action": "hold_for_approval", "params": {"categories": ["files"]}},
    )
    assert classification.action is None


@pytest.mark.asyncio
async def test_the_users_own_words_hold_without_the_agent_remembering():
    """The Agent never set the hold; "任何写入 Knowledge 的操作必须人工审批" still stops the write."""
    from packages.core.governance.service import _apply_conversation_holds

    facts = ActionFacts(action_key="workspace.file.create", category="files", impact="reversible")
    said = ["为周末新客活动准备内部文案草案；任何写入 Knowledge 的操作必须人工审批。"]
    decision = await _apply_conversation_holds(_db([], said), PolicyDecision(True), conversation_id="C1", facts=facts)
    assert decision.pause_for_hitl is True and decision.matched_rule == "conversation_hold:files"


@pytest.mark.parametrize("text,held", [
    ("任何写入 Knowledge 的操作必须人工审批。", {"files"}),
    ("请求把草稿写入测试存储，先停在审批卡片。", {"files"}),
    ("发布前需要我批准", {"publishing"}),
    ("Send nothing until I give my approval.", {"communication"}),
    ("Ask me before you save the file.", {"files"}),
    ("先只出草案，等我批准。", {"files", "publishing", "communication", "deletion", "purchases"}),
    ("这次不需要审批，直接保存。", set()),
    ("写一段关于审批流程的说明。", set()),
    ("确认，由成本分析员执行。", set()),
    # Historical approvals and instructions about an existing gate must not
    # become a standing hold on every unrelated file the conversation creates.
    ("我已在这份审批卡上点击 Approve。", set()),
    ("每次需审批就先给具体对象/diff再触发，我会审批。", set()),
    ("请现在先完成配置并给可审批的真实diff（先输出draft ID/validation/具体修改，再调用apply触发审批）。", set()),
    ("不要只写方案或让用户再次批准范围，优先最小可运行原生实现。", set()),
    ("先不要再次 stage 或 apply，也不要再次调用会申请审批的 workspace 更新操作。", set()),
    ("If approval is required, show me the diff first.", set()),
    ("工作区配置修改必须经过我的批准。", {"workspace"}),
    ("保存文件前先让我批准。", {"files"}),
    ("文件要我批准才能保存。", {"files"}),
    ("保存文件每次需审批后才能继续。", {"files"}),
    ("保存文件都要批准。", {"files"}),
    ("保存文件不要再让我批准。", set()),
    ("Do not ask me before you save the file.", set()),
    ("如果需要审批就展示diff，保存文件仍必须人工审批。", {"files"}),
])
def test_review_demands_are_read_per_sentence(text, held):
    from packages.core.governance.conversation_holds import stated_hold_categories

    assert stated_hold_categories(text) == held


@pytest.mark.asyncio
async def test_configuration_approval_history_does_not_hold_a_new_internal_file():
    from packages.core.governance.service import _apply_conversation_holds

    said = [
        "我已在这份审批卡上点击 Approve。",
        "每次需审批就先给具体对象/diff再触发，我会审批。",
        "工作区配置修改必须经过我的批准。",
        "Save ONE new document, launch-handoff.md (unique name, create only).",
    ]
    facts = ActionFacts(action_key="workspace.file.create", category="files", impact="reversible")
    allow = PolicyDecision(True)
    assert await _apply_conversation_holds(_db([], said), allow, conversation_id="C1", facts=facts) is allow
    # A real saved file hold still takes precedence over the new request.
    assert (await _apply_conversation_holds(
        _db([{"category": "files"}], said), allow, conversation_id="C1", facts=facts,
    )).pause_for_hitl
