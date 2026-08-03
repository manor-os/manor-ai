import json

import pytest


def _chrome_approval_request(*, approval_id: str = "chrome-approval-1") -> dict:
    from packages.core.ai.runtime.provider_approvals import (
        normalize_provider_approval,
    )

    arguments = {
        "tabId": 123,
        "ref": "e7",
        "snapshot_id": "snap-1",
        "label": "Publish",
        "role": "button",
        "url": "https://www.linkedin.com/feed/",
        "input": "x" * 300,
        "files": [f"asset-{index}.png" for index in range(12)],
    }
    result = json.dumps(
        {
            "ok": False,
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": approval_id,
            "expires_at": "2099-07-19T12:00:00Z",
            "reason": "side_effect_action_requires_confirmation",
            "target_label": "Publish",
            "target_role": "button",
            "url": "https://www.linkedin.com/feed/",
            "data_summary": "Publish the prepared post",
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            },
        }
    )
    request = normalize_provider_approval(
        "mcp__chrome__click_element",
        arguments,
        result,
    )
    assert request is not None
    return request


def test_chrome_provider_approval_adapter_preserves_exact_retry_arguments() -> None:
    request = _chrome_approval_request()

    assert request["provider"] == "chrome"
    assert request["provider_approval_id"] == "chrome-approval-1"
    assert request["confirmation_tool"] == "mcp__chrome__confirm_action"
    assert request["confirmation_arguments"] == {
        "approvalId": "chrome-approval-1",
    }
    assert request["retry_tool"] == "mcp__chrome__click_element"
    assert request["retry_arguments"]["input"] == "x" * 300
    assert request["retry_arguments"]["files"] == [
        f"asset-{index}.png" for index in range(12)
    ]


def test_nested_tool_stream_records_provider_approval_without_exposing_it_to_sse() -> None:
    from packages.core.ai.runtime.streams import RuntimeToolStreamSink

    class Queue:
        def __init__(self) -> None:
            self.items: list[dict] = []

        def put_nowait(self, item: dict) -> None:
            self.items.append(item)

    queue = Queue()
    recorded: list[tuple[str, dict]] = []
    arguments = _chrome_approval_request()["retry_arguments"]
    result = json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": "chrome-approval-1",
            "expires_at": "2099-07-19T12:00:00Z",
            "target_label": "Publish",
            "retry_action": {
                "name": "mcp__chrome__click_element",
                "arguments": arguments,
            },
        }
    )
    sink = RuntimeToolStreamSink(
        event_queue=queue,
        record_tool_event=lambda event_type, payload: recorded.append(
            (event_type, payload)
        ),
        format_event=lambda event_type, payload: {
            "event": event_type,
            "payload": payload,
        },
        format_tool_arguments=lambda _name, args: {"ref": args.get("ref")},
        format_tool_result=lambda _name, value: value[:80],
        resolve_tool_status=lambda _value: "success",
    )

    sink.emit_tool_end(
        "mcp__chrome__click_element",
        result,
        args=arguments,
    )

    recorded_call = recorded[0][1]["tool_call"]
    streamed_call = queue.items[0]["payload"]["tool_call"]
    assert recorded_call["provider_approval"]["retry_arguments"] == arguments
    assert "provider_approval" not in streamed_call
    assert "provider_approval" not in json.dumps(queue.items[0])


@pytest.mark.asyncio
async def test_register_provider_approval_creates_standard_hitl_and_deduplicates(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider approval",
    )
    db_session.add(conversation)
    await db_session.flush()
    request = _chrome_approval_request()

    first = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    second = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )

    assert first is not None
    assert second is not None
    assert first["__hitl__"] is True
    assert first["hitl"]["id"] != request["provider_approval_id"]
    assert first["hitl"]["id"] == second["hitl"]["id"]
    assert first["hitl"]["type"] == "approval"
    # Provider gates now offer "Always approve": Manor records the operator's
    # standing decision per provider + action and answers the gate on their
    # behalf next time, instead of asking the same question every attempt.
    assert first["hitl"]["options"] == ["approve", "always_approve", "reject"]
    stored = await db_session.get(HitlRequest, first["hitl"]["id"])
    assert stored is not None
    assert stored.status == "pending"
    assert (stored.context or {}).get("kind") == "provider"
    assert (stored.context or {}).get("provider") == "chrome"
    assert (stored.context or {}).get("continuation", {}).get(
        "retry_arguments"
    ) == request["retry_arguments"]


@pytest.mark.asyncio
async def test_register_provider_approval_rejects_incomplete_continuation(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Invalid provider approval",
        )
    )
    await db_session.flush()
    request = _chrome_approval_request()
    request.pop("confirmation_tool")

    result = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )

    assert result is None


@pytest.mark.asyncio
async def test_provider_approval_resolution_returns_generic_continuation_metadata(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Provider approval",
        )
    )
    await db_session.flush()
    request = _chrome_approval_request()
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=request,
    )
    assert hitl_data is not None
    hitl_id = hitl_data["hitl"]["id"]

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="approve",
    )

    assert resolution.message.startswith("[Runtime approval approved]")
    assert resolution.runtime_metadata == {
        "extra_tool_names": [
            "mcp__chrome__click_element",
            "mcp__chrome__confirm_action",
            "mcp__chrome__read_page",
        ],
        "forced_tool_calls": [
            {
                "name": "mcp__chrome__confirm_action",
                "arguments": {
                    "approvalId": "chrome-approval-1",
                    "__manor_tool_continuation": {
                        "kind": "retry_with_result_token",
                        "tool": "mcp__chrome__click_element",
                        "arguments": request["retry_arguments"],
                        "required_status": "approved",
                        "result_token_keys": [
                            "approvalToken",
                            "approval_token",
                        ],
                        "argument_token_key": "approvalToken",
                    },
                },
            }
        ],
        "approval_resume_guidance": (
            "Resume the approved provider action using the supplied forced "
            "tool continuation. Do not rediscover or alter the approved action."
        ),
    }


@pytest.mark.asyncio
async def test_provider_approval_rejection_never_returns_continuation(db_session) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Provider approval",
        )
    )
    await db_session.flush()
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(),
    )
    assert hitl_data is not None

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_data["hitl"]["id"],
        action="reject",
    )

    assert resolution.message.startswith("[Runtime approval rejected]")
    assert resolution.runtime_metadata is None


@pytest.mark.asyncio
async def test_provider_approval_expiry_and_duplicate_resolution_do_not_resume(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider approval expiry",
    )
    db_session.add(conversation)
    await db_session.flush()
    expired_request = _chrome_approval_request(approval_id="expired-provider")
    expired_request["expires_at"] = "2000-01-01T00:00:00Z"
    expired_hitl = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=expired_request,
    )
    assert expired_hitl is not None

    expired = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=expired_hitl["hitl"]["id"],
        action="approve",
    )
    assert expired.runtime_metadata is None
    expired_row = await db_session.get(HitlRequest, expired_hitl["hitl"]["id"])
    assert expired_row.status == "expired"

    active_hitl = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="active-provider"),
    )
    assert active_hitl is not None
    first = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=active_hitl["hitl"]["id"],
        action="approve",
    )
    second = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=active_hitl["hitl"]["id"],
        action="approve",
    )
    assert first.runtime_metadata is not None
    assert second.runtime_metadata is None


@pytest.mark.asyncio
async def test_chat_approval_turn_returns_provider_continuation_for_exact_hitl_id(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.services.chat_approvals import resolve_chat_approval_turn

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Structured provider approval",
        )
    )
    await db_session.flush()
    first = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="first-provider"),
    )
    second = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(approval_id="second-provider"),
    )
    assert first is not None and second is not None

    replacement, saved_text, save_user, runtime_metadata = (
        await resolve_chat_approval_turn(
            db_session,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            message=json.dumps(
                {"hitl_id": second["hitl"]["id"], "action": "approve"}
            ),
        )
    )

    assert replacement.startswith("[Runtime approval approved]")
    assert saved_text == "Approved the requested action."
    assert save_user is True
    assert runtime_metadata["forced_tool_calls"][0]["arguments"][
        "approvalId"
    ] == "second-provider"


@pytest.mark.asyncio
async def test_plain_reply_does_not_guess_between_provider_approvals(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_pending_runtime_approval_turn_from_reply,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Ambiguous provider approvals",
        )
    )
    await db_session.flush()
    for approval_id in ("first-provider", "second-provider"):
        assert await register_provider_runtime_approval(
            db_session,
            conversation_id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            request=_chrome_approval_request(approval_id=approval_id),
        )

    resolution = await resolve_pending_runtime_approval_turn_from_reply(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        message="yes",
    )

    assert resolution is None


@pytest.mark.asyncio
async def test_cancelling_provider_approval_resolves_existing_hitl_card(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        cancel_pending_runtime_approvals,
        register_provider_runtime_approval,
    )
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    conversation = Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Cancelled provider approval",
    )
    db_session.add(conversation)
    await db_session.flush()
    hitl_data = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=_chrome_approval_request(),
    )
    assert hitl_data is not None
    hitl_id = hitl_data["hitl"]["id"]
    message = Message(
        id=generate_ulid(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        message_kind="hitl_request",
        meta={"hitl_requests": [{"id": hitl_id, "type": "approval"}]},
    )
    db_session.add(message)
    await db_session.flush()

    cancelled = await cancel_pending_runtime_approvals(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_ids=[hitl_id],
    )

    assert cancelled == 1
    cancelled_row = await db_session.get(HitlRequest, hitl_id)
    assert cancelled_row.status == "expired"
    assert cancelled_row.resolved_reason == "request_stopped"
    await db_session.refresh(message)
    assert message.meta["hitl_requests"][0]["resolved"] is True
    assert message.meta["hitl_requests"][0]["resolution"] == "cancelled"


def test_runtime_chat_context_contains_no_provider_specific_approval_resume() -> None:
    from pathlib import Path

    source = Path("packages/core/services/runtime_chat_context.py").read_text()
    assert "_is_chrome_pending_action_confirmation" not in source
    assert "_chrome_approval_resume_for_confirmation" not in source
    assert "__manor_chrome_retry" not in source
    assert "approval_resume_guidance" in source


def test_generic_runtime_modules_do_not_embed_chrome_approval_projection() -> None:
    from pathlib import Path

    streams_source = Path("packages/core/ai/runtime/streams.py").read_text()
    approvals_source = Path("packages/core/ai/runtime/approvals.py").read_text()
    assert "_CHROME_APPROVAL_PROJECTION_KEYS" not in streams_source
    assert "_chrome_retry_arguments_for_persistence" not in streams_source
    assert "is_chrome_approval" not in approvals_source


@pytest.mark.asyncio
async def test_non_stream_nested_provider_approval_is_persisted_as_hitl(
    db_session,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select

    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.ai.runtime.streams import runtime_skill_nested_tool_callbacks
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation, Message
    from packages.core.services.chat_service import run_chat_message

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Non-stream provider approval",
        )
    )
    await db_session.flush()
    request = _chrome_approval_request()
    result_payload = json.dumps(
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "expires_at": request["expires_at"],
            "target_label": request["target_label"],
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        }
    )

    async def fake_context(*_args, **_kwargs):
        return (
            "system",
            [{"type": "function", "function": {"name": "invoke_skill"}}],
            [],
            SimpleNamespace(
                workspace_id=None,
                task_id=None,
                runtime_envelope=None,
                tool_profile=None,
                allowed_tool_names=None,
                user=None,
                entity=None,
            ),
        )

    async def fake_loop(**_kwargs):
        on_start, on_end = runtime_skill_nested_tool_callbacks(
            skill_name="chrome",
            invoke_skill_args={"skill": "chrome"},
        )
        assert on_start is not None and on_end is not None
        on_start(request["retry_tool"], request["retry_arguments"])
        on_end(
            request["retry_tool"],
            result_payload,
            42,
            request["retry_arguments"],
        )
        return AgenticResult(
            content="Waiting for approval.",
            messages=[],
            usage={},
            rounds=1,
            tool_calls_made=["invoke_skill"],
        )

    with (
        patch(
            "packages.core.services.chat_service.resolve_runtime_chat_context",
            new=fake_context,
        ),
        patch(
            "packages.core.services.chat_service.runtime_execute_chat_agent_loop",
            new=fake_loop,
        ),
        patch(
            "packages.core.services.model_resolver.resolve_model_for_user",
            new=AsyncMock(return_value="openai/gpt-5.5"),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_llm_usage",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.resolve_author_subscription_id",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_runtime_learning",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "packages.core.services.chat_service.schedule_learning_candidate_applies",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.runtime_persist_chat_runtime_events",
            new=AsyncMock(),
        ),
    ):
        result = await run_chat_message(
            "Publish the prepared post",
            conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            db=db_session,
        )

    assert result["hitl_requests"]
    hitl_id = result["hitl_requests"][0]["id"]
    message = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.role == "assistant",
            )
        )
    ).scalar_one()
    assert message.message_kind == "hitl_request"
    assert message.meta["hitl_requests"][0]["id"] == hitl_id
