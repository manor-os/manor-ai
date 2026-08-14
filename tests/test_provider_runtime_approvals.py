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
            "action_key": "click_element",
            "approvalId": approval_id,
            "expires_at": "2099-07-19T12:00:00Z",
            "reason": "side_effect_action_requires_confirmation",
            "confirmation_mode": "always_action_time",
            "policy_category": "representational_communication",
            "preapproved": False,
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
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "preapproved": False,
    }
    assert request["confirmation_mode"] == "always_action_time"
    assert request["policy_category"] == "representational_communication"
    assert request["preapproved"] is False
    assert request["retry_tool"] == "mcp__chrome__click_element"
    assert request["retry_arguments"]["input"] == "x" * 300
    assert request["retry_arguments"]["files"] == [
        f"asset-{index}.png" for index in range(12)
    ]


def test_provider_continuation_retries_chrome_with_standard_token_only() -> None:
    """Retry the exact approved Chrome action with only its provider token."""
    from packages.core.ai.agentic_loop import _retry_call_after_tool_continuation
    from packages.core.ai.runtime.provider_approvals import provider_approval_runtime_metadata

    request = _chrome_approval_request()
    metadata = provider_approval_runtime_metadata(
        {
            "kind": "provider",
            "provider": "chrome",
            "provider_approval_id": request["provider_approval_id"],
            "continuation": request,
        }
    )
    continuation = metadata["forced_tool_calls"][0]["arguments"][
        "__manor_tool_continuation"
    ]

    retry = _retry_call_after_tool_continuation(
        {"ok": True, "status": "approved", "approvalToken": "fresh-token"},
        continuation,
        {"mcp__chrome__click_element"},
    )

    assert retry["arguments"]["approvalToken"] == "fresh-token"
    assert "approval_token" not in retry["arguments"]
    assert retry["arguments"]["ref"] == request["retry_arguments"]["ref"]


def test_provider_continuation_restores_chrome_confirmation_receipt() -> None:
    """A resumed Chat turn must restore the receipt required by confirm_action."""
    from packages.core.ai.runtime.chrome_routing import (
        runtime_blocked_chrome_workflow_contract,
    )
    from packages.core.ai.runtime.provider_approvals import (
        provider_approval_runtime_metadata,
    )

    request = _chrome_approval_request()
    metadata = provider_approval_runtime_metadata(
        {
            "kind": "provider",
            "provider": "chrome",
            "provider_approval_id": request["provider_approval_id"],
            "continuation": request,
        }
    )

    assert metadata is not None
    blocked = runtime_blocked_chrome_workflow_contract(
        tool_name="mcp__chrome__confirm_action",
        arguments={"approvalId": request["provider_approval_id"]},
        runtime_metadata=metadata,
        active_user_message="Use Chrome to publish content on LinkedIn",
    )

    assert blocked is None


def test_preapproved_chrome_confirmation_clears_collected_provider_gate() -> None:
    from packages.core.ai.runtime.provider_approvals import ProviderApprovalCollector

    collector = ProviderApprovalCollector()
    request = _chrome_approval_request()
    collector.capture(
        request["retry_tool"],
        request["retry_arguments"],
        {
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        },
    )
    assert collector.pending_request() is not None

    collector.capture(
        "mcp__chrome__confirm_action",
        {
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "preapproved": True,
        },
        {
            "ok": True,
            "status": "approved",
            "approvalId": request["provider_approval_id"],
            "approvalToken": "single-use-token",
        },
    )

    assert collector.pending_request() is None


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
    assert recorded_call["raw_result"] == result
    assert recorded_call["provider_approval"]["retry_arguments"] == arguments
    assert "raw_result" not in streamed_call
    assert "provider_approval" not in streamed_call
    assert "raw_result" not in json.dumps(queue.items[0])
    assert "provider_approval" not in json.dumps(queue.items[0])


def test_nested_preapproved_confirmation_clears_collected_provider_gate() -> None:
    from packages.core.ai.runtime.provider_approvals import ProviderApprovalCollector
    from packages.core.ai.runtime.streams import RuntimeToolStreamSink

    collector = ProviderApprovalCollector()
    sink = RuntimeToolStreamSink(
        record_tool_event=lambda _event_type, payload: collector.capture_recorded_tool_call(
            payload["tool_call"]
        ),
        format_tool_arguments=lambda _name, args: dict(args or {}),
        format_tool_result=lambda _name, value: value[:80],
        resolve_tool_status=lambda _value: "success",
    )
    request = _chrome_approval_request()
    sink.emit_tool_end(
        request["retry_tool"],
        json.dumps({
            "status": "approval_required",
            "approval_required": True,
            "provider": "chrome",
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "retry_action": {
                "name": request["retry_tool"],
                "arguments": request["retry_arguments"],
            },
        }),
        args=request["retry_arguments"],
    )
    assert collector.pending_request() is not None

    sink.emit_tool_end(
        "mcp__chrome__confirm_action",
        json.dumps({
            "ok": True,
            "status": "approved",
            "approvalId": request["provider_approval_id"],
            "approvalToken": "single-use-token",
        }),
        args={
            "approvalId": request["provider_approval_id"],
            "confirmation_mode": "preapproval_allowed",
            "policy_category": "file_upload",
            "preapproved": True,
        },
    )

    assert collector.pending_request() is None


@pytest.mark.asyncio
async def test_stream_nested_manor_hitl_is_promoted_to_approval_event() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core.ai.runtime import ChatSurface
    from packages.core.ai.runtime.streams import runtime_skill_nested_tool_callbacks
    from packages.core.services.chat_service import stream_chat_response

    hitl_payload = json.dumps({
        "__hitl__": True,
        "error": "approval_required",
        "approval_token": "hitl-calendar-delete",
        "hitl": {
            "id": "hitl-calendar-delete",
            "type": "approval",
            "prompt": "Delete the temporary calendar event?",
            "action": "calendar.event.delete",
            "tool": "mcp__google_calendar__delete_event",
            "options": ["approve", "reject"],
        },
        "operation": {
            "tool": "mcp__google_calendar__delete_event",
            "action_key": "calendar.event.delete",
        },
    })

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
            skill_name="calendar",
            invoke_skill_args={"skill": "calendar"},
        )
        assert on_start is not None and on_end is not None
        on_start("mcp__google_calendar__delete_event", {"event_id": "event-1"})
        on_end(
            "mcp__google_calendar__delete_event",
            hitl_payload,
            12,
            {"event_id": "event-1"},
        )
        return AgenticResult(
            content="Waiting for approval.",
            messages=[],
            usage={},
            rounds=1,
            tool_calls_made=["invoke_skill"],
        )

    events: list[tuple[str, dict]] = []
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
            "packages.core.services.chat_service.runtime_persist_chat_stream_runtime_events",
            new=AsyncMock(),
        ),
        patch(
            "packages.core.services.chat_service.record_chat_llm_usage",
            new=AsyncMock(),
        ),
    ):
        async for raw in stream_chat_response(
            "Delete the temporary calendar event",
            "conv-nested-hitl",
            entity_id="entity-1",
            user_id="user-1",
            persist_messages=False,
            runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
        ):
            event_name = raw.split("\n", 1)[0].removeprefix("event: ")
            if "\ndata: " not in raw:
                continue
            payload = json.loads(raw.split("\ndata: ", 1)[1].strip())
            events.append((event_name, payload))

    approvals = [payload for event, payload in events if event == "hitl_required"]
    assert len(approvals) == 1
    assert approvals[0]["hitl"]["id"] == "hitl-calendar-delete"
    assert approvals[0]["operation"]["tool"] == "mcp__google_calendar__delete_event"


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
    assert first["hitl"]["options"] == [
        "approve", "always_approve", "revise", "reject",
    ]
    stored = await db_session.get(HitlRequest, first["hitl"]["id"])
    assert stored is not None
    assert stored.status == "pending"
    assert (stored.context or {}).get("kind") == "provider"
    assert (stored.context or {}).get("provider") == "chrome"
    assert (stored.context or {}).get("continuation", {}).get(
        "retry_arguments"
    ) == request["retry_arguments"]


@pytest.mark.asyncio
async def test_provider_always_approve_persists_and_auto_confirms_next_gate_end_to_end(
    db_session,
    monkeypatch,
) -> None:
    from packages.core import database
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
        runtime_auto_confirm_provider_approval,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.task import Conversation
    from packages.core.models.user import User

    class _SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _SessionContext())

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@example.com",
        password_hash="x",
        role="owner",
    ))
    db_session.add(Conversation(
        id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        title="Provider always approve E2E",
    ))
    await db_session.flush()

    first_request = _chrome_approval_request(approval_id="chrome-first")
    first_card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=first_request,
    )
    assert first_card is not None
    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=first_card["hitl"]["id"],
        action="always_approve",
    )
    assert resolution.runtime_metadata is not None
    await db_session.flush()

    second_request = _chrome_approval_request(approval_id="chrome-second")
    second_card = await register_provider_runtime_approval(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        request=second_request,
    )
    assert second_card is None, "standing grant must suppress the next approval card"

    provider_result = json.dumps({
        "ok": False,
        "status": "approval_required",
        "approval_required": True,
        "provider": "chrome",
        "action_key": "click_element",
        "approvalId": "chrome-second",
        "expires_at": "2099-07-19T12:00:00Z",
        "confirmation_mode": "always_action_time",
        "policy_category": "representational_communication",
        "target_label": "Publish",
        "target_role": "button",
        "url": "https://www.linkedin.com/feed/",
        "retry_action": {
            "name": second_request["retry_tool"],
            "arguments": second_request["retry_arguments"],
        },
    })
    calls: list[tuple[str, dict]] = []

    async def execute(name: str, arguments: dict) -> str:
        calls.append((name, arguments))
        if name == "mcp__chrome__confirm_action":
            return json.dumps({
                "ok": True,
                "status": "approved",
                "approvalToken": "single-use-next-token",
            })
        return json.dumps({"ok": True, "status": "published"})

    result = await runtime_auto_confirm_provider_approval(
        tool_name=second_request["retry_tool"],
        arguments=second_request["retry_arguments"],
        result=provider_result,
        execute=execute,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=None,
    )

    assert json.loads(result)["status"] == "published"
    assert [name for name, _ in calls] == [
        "mcp__chrome__confirm_action",
        "mcp__chrome__click_element",
    ]
    assert calls[0][1]["approvalId"] == "chrome-second"
    assert calls[1][1]["approvalToken"] == "single-use-next-token"


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
        "chrome_runtime_contract_v1": {
            "pending_chrome_confirmation": {
                "approval_id": "chrome-approval-1",
                "tool_name": "mcp__chrome__click_element",
                "confirmation_mode": "always_action_time",
                "policy_category": "representational_communication",
                "preapproved": False,
                "destination": "https://www.linkedin.com/feed/",
                "data_summary": "Publish the prepared post",
                "action_signature": (
                    "423b4684e0b313afac4bad56a84f1336b605ef1d44110f69cfda60d12dde93e7"
                ),
            }
        },
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
                    "confirmation_mode": "always_action_time",
                    "policy_category": "representational_communication",
                    "preapproved": False,
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
async def test_provider_publish_approval_revision_closes_old_gate_without_continuation(
    db_session,
) -> None:
    from packages.core.ai.runtime.approval_service import (
        register_provider_runtime_approval,
        resolve_runtime_approval_turn,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.hitl_request import HitlRequest
    from packages.core.models.task import Conversation

    entity_id = generate_ulid()
    user_id = generate_ulid()
    conversation_id = generate_ulid()
    db_session.add(
        Conversation(
            id=conversation_id,
            entity_id=entity_id,
            user_id=user_id,
            title="Revise provider approval",
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
    hitl_id = hitl_data["hitl"]["id"]

    resolution = await resolve_runtime_approval_turn(
        db_session,
        conversation_id=conversation_id,
        entity_id=entity_id,
        user_id=user_id,
        hitl_id=hitl_id,
        action="revise",
        revision_request="Make the opening shorter and remove the hashtag.",
    )

    assert "Do not confirm or retry" in resolution.message
    assert "Make the opening shorter" in resolution.message
    assert resolution.runtime_metadata == {
        "approval_kind": "provider_revision",
        "approval_resume_guidance": (
            "Revise the proposed content according to the user's instructions. "
            "Do not confirm or reuse the superseded provider approval; request "
            "approval again only for the new final external action."
        ),
    }
    stored = await db_session.get(HitlRequest, hitl_id)
    assert stored.status == "expired"
    assert stored.resolved_reason == "revision_requested"
    assert stored.decided_via == "chat_card_revise"


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
