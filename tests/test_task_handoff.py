"""A turn that hands work to Tasks does not also do it in Chat."""
import json

import pytest

from packages.core.ai.runtime.task_handoff import (
    HANDED_TO_TASKS_CODE,
    TASKS_REQUESTED_CODE,
    delegation_handoff_refusal,
)

DELEGATE = {"tasks": [{"agent": "成本分析员", "task": "测算单位毛利"}]}


def _turn(user: str, *results: dict) -> list[dict]:
    return [
        {"role": "system", "content": "context"},
        {"role": "user", "content": user},
        *({"role": "tool", "content": json.dumps(result, ensure_ascii=False)} for result in results),
    ]


def test_a_turn_that_created_tasks_does_not_also_delegate_their_work():
    """Live: three Tasks created, then every stage delegated in Chat and the Tasks started — run twice."""
    messages = _turn("好的，开始吧。", {"created": True, "task": {"id": "T1"}}, {"created": True, "task": {"id": "T2"}})
    refusal = delegation_handoff_refusal("delegate_to_agent", DELEGATE, messages)
    assert refusal["error"] == HANDED_TO_TASKS_CODE and refusal["details"]["task_ids"] == ["T1", "T2"]


def test_a_started_task_counts_as_handed_off():
    messages = _turn("开始执行。", {"updated": True, "started": True, "task": {"id": "T9"}})
    assert delegation_handoff_refusal("delegate_to_agent", DELEGATE, messages)["error"] == HANDED_TO_TASKS_CODE


def test_asking_for_tasks_sends_a_chat_delegation_back():
    """Live: "创建这些 Task 并开始执行" ran all three stages in one nine-minute Chat turn."""
    for request in ("计划可以。创建这些 Task 并开始执行。", "请拆成有先后依赖的任务", "Please create tasks for each stage."):
        refusal = delegation_handoff_refusal("delegate_to_agent", DELEGATE, _turn(request))
        assert refusal["error"] == TASKS_REQUESTED_CODE, request


def test_ordinary_delegation_and_earlier_turns_pass():
    assert delegation_handoff_refusal(
        "delegate_to_agent", DELEGATE, _turn("请让成本分析员和文案员分别处理周末新客活动。"),
    ) is None
    # A Task created in an earlier turn does not bind this one.
    earlier = [
        {"role": "user", "content": "创建一个 Task"},
        {"role": "tool", "content": json.dumps({"created": True, "task": {"id": "T1"}})},
        {"role": "user", "content": "再让文案员写一条标语。"},
    ]
    assert delegation_handoff_refusal("delegate_to_agent", DELEGATE, earlier) is None
    assert delegation_handoff_refusal("generate_file", {}, _turn("创建这些 Task")) is None


@pytest.mark.asyncio
async def test_a_turn_cannot_end_with_stages_nothing_will_start():
    """Live: the turn ended with stages 2 and 3 pending, unlinked, never to run."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.chat_evidence import ChatStopGate
    from packages.core.ai.runtime.final_response import FinalResponseAction

    async def stranded(scope):
        return [{"id": "T2", "title": "文案"}, {"id": "T3", "title": "清单"}]

    async def none(scope):
        return []

    gate = ChatStopGate(workspace_scope=SimpleNamespace(), locale="zh-CN", stranded_task_reader=stranded,
                        task_reader=none, current_turn_task_reader=none)
    first = await gate.validate("三个阶段都已开始。")
    assert first.action is FinalResponseAction.CONTINUE_EXECUTION
    assert first.diagnostic["error"] == "tasks_stranded" and "T2" in first.diagnostic["message"]
    # One nudge per turn: the reply is accepted after it.
    assert (await gate.validate("已处理。")).action is FinalResponseAction.ACCEPT


def test_a_change_after_creation_outranks_the_description_in_the_brief():
    """Live: stages 2 and 3 got "预算改为740元" yet wrote 900/24 from their description and stage 1."""
    from types import SimpleNamespace

    from packages.core.workers.task_agent_loop import task_brief

    task = SimpleNamespace(
        title="内部文案", description="预算900元，目标24位新客。", expected_output=None,
        details={"changes": [{"at": "t", "text": "预算改为740元，目标改为20位新客。"}],
                 "dep_outputs": [{"task_title": "成本测算", "result_summary": "按900元/24人测算"}]},
    )
    brief = task_brief(task)
    change = brief.index("## Changes since this Task was created")
    assert brief.index("## Description") < change < brief.index("## Outputs of the Tasks this one depends on")
    assert "override the description above and the outputs of earlier Tasks" in brief
    # Live: an updated report still explained the discount total with the replaced 120 元.
    assert "no sentence keeps a value a change replaced" in brief
    assert "预算改为740元" in brief[change:]


@pytest.mark.asyncio
async def test_a_running_loop_receives_a_change_between_rounds_once():
    """Live: stage 1 was running when the budget changed and delivered 900 anyway."""
    from unittest.mock import AsyncMock, patch

    from packages.core.ai.agentic_loop import agentic_loop

    seen: list[list[dict]] = []

    async def completion(messages, tools, **kwargs):
        seen.append([dict(message) for message in messages])
        if len(seen) == 1:
            return "", [{"id": "c1", "name": "lookup", "arguments": {}}], {"prompt": 1, "completion": 1, "total": 2}
        return "按740元重算完成。", None, {"prompt": 1, "completion": 1, "total": 2}

    updates = AsyncMock(side_effect=[["预算改为740元。"], [], []])
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}
    with patch("packages.core.ai.agentic_loop.runtime_execute_agentic_round_tool_completion", side_effect=completion):
        result = await agentic_loop(
            system_prompt="You run a Task.", user_message="成本测算", tools=[tool],
            tool_executor=AsyncMock(return_value="ok"), incoming_updates=updates,
        )
    assert result.content.startswith("按740元")
    delivered = [m for m in seen[1] if m.get("role") == "user" and "预算改为740元" in str(m.get("content"))]
    assert len(delivered) == 1 and "Written by the Manor runtime" in delivered[0]["content"]
    assert not any("预算改为740元" in str(m.get("content")) for m in seen[0])


def test_a_chat_turn_reading_a_task_does_not_make_it_the_origin():
    """Live: get_task_details on a completed stage returned approval_origin_not_executable."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_execution import runtime_approval_origin_task_id

    context = SimpleNamespace(task_id="T1")
    chat = SimpleNamespace(surface=ChatSurface.WORKSPACE_CHAT, task_id=None)
    task_run = SimpleNamespace(surface=ChatSurface.WORKSPACE_CHAT, task_id="T1")
    workflow = SimpleNamespace(surface=ChatSurface.WORKFLOW_AGENT_STEP, task_id=None)
    assert runtime_approval_origin_task_id(context, chat) is None
    assert runtime_approval_origin_task_id(context, task_run) == "T1"
    assert runtime_approval_origin_task_id(context, workflow) == "T1"
    assert runtime_approval_origin_task_id(context, None) == "T1"


@pytest.mark.asyncio
async def test_asked_for_tasks_and_one_created_the_turn_creates_the_rest():
    """Live: only stage 1 was created; the reply promised to create stages 2 and 3 after it completed."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.chat_evidence import ChatStopGate
    from packages.core.ai.runtime.final_response import FinalResponseAction

    async def lone(scope):
        return [{"id": "T1", "title": "财务测算"}]

    async def none(scope):
        return []

    gate = ChatStopGate(workspace_scope=SimpleNamespace(), locale="zh-CN", lone_task_reader=lone,
                        stranded_task_reader=none, task_reader=none, current_turn_task_reader=none)
    first = await gate.validate("Task 1 已启动，完成后我会创建 Task 2。")
    assert first.action is FinalResponseAction.CONTINUE_EXECUTION
    assert first.diagnostic["error"] == "tasks_requested_one_created"
    assert "Chat does not run again when a Task completes" in first.diagnostic["message"]
    assert (await gate.validate("三个 Task 已创建。")).action is FinalResponseAction.ACCEPT


def test_task_requests_are_recognized():
    from packages.core.ai.runtime.task_handoff import user_asked_for_tasks

    assert user_asked_for_tasks("计划可以。创建这些 Task 并开始执行。")
    assert not user_asked_for_tasks("把这份报告总结一下")
