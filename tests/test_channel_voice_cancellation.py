from types import SimpleNamespace
from unittest.mock import AsyncMock

from packages.core.services.agent_subscription_service import ResolvedSubscription


async def test_public_voice_channel_turn_exposes_cooperative_cancel_and_status(monkeypatch):
    from packages.core.services import channel_agent_runtime, workspace_runtime
    from packages.core.services.voice import work_queue

    captured = {}

    class DummySession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def resolve_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            tool_profile="workspace_agent",
            workspace_id="workspace",
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=None,
            is_master=False,
            mcp_allowed_names=None,
            extra_context="",
        )

    async def prepare_appendix(*_args, **_kwargs):
        return SimpleNamespace(
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=SimpleNamespace(),
        )

    async def execute_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="", stop_reason="cancelled")

    monkeypatch.setattr(channel_agent_runtime, "async_session", lambda: DummySession())
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", resolve_runtime)
    monkeypatch.setattr(
        channel_agent_runtime,
        "resolve_channel_base_prompt",
        AsyncMock(return_value="base"),
    )
    monkeypatch.setattr(
        channel_agent_runtime,
        "runtime_prepare_prompt_appendix_for_turn",
        prepare_appendix,
    )
    monkeypatch.setattr(
        channel_agent_runtime,
        "runtime_execute_channel_agent_loop",
        execute_loop,
    )
    interrupted = AsyncMock(return_value=True)
    monkeypatch.setattr(work_queue, "voice_work_was_interrupted", interrupted)

    result = await channel_agent_runtime.run_channel_agent_turn(
        entity_id="entity",
        agent_id="agent",
        user_id="owner",
        conversation_id="conversation",
        current_message="Run the report",
        history=[],
        sender_ctx={"role": "external"},
        subscription=ResolvedSubscription(
            id="subscription",
            agent_id="agent",
            workspace_id="workspace",
            custom_prompt="You are helpful.",
        ),
        runtime_metadata={
            "voice_session_mode": "chat_gateway",
            "voice_origin_message_id": "message",
        },
    )

    assert callable(captured["is_cancelled"])
    assert await captured["is_cancelled"]() is True
    interrupted.assert_awaited_once()
    assert interrupted.await_args.kwargs == {
        "message_id": "message",
        "conversation_id": "conversation",
    }
    assert result is not None
    assert result.content == ""
    assert result.stop_reason == "cancelled"


async def test_twilio_voice_turn_cooperatively_cancels_when_call_binding_changes(
    monkeypatch,
):
    from packages.core.services import channel_agent_runtime, workspace_runtime
    from packages.core.services.voice import binding as voice_binding
    from packages.core.services.voice import work_queue

    captured = {}

    class DummySession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def resolve_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            tool_profile="workspace_agent",
            workspace_id="workspace",
            task_id=None,
            thread_ref_kind=None,
            thread_ref_id=None,
            bound_tool_names=None,
            is_master=False,
            mcp_allowed_names=None,
            extra_context="",
        )

    async def prepare_appendix(*_args, **_kwargs):
        return SimpleNamespace(
            tool_schemas=[],
            allowed_tool_names=set(),
            envelope=SimpleNamespace(),
        )

    async def execute_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="", stop_reason="cancelled")

    monkeypatch.setattr(channel_agent_runtime, "async_session", lambda: DummySession())
    monkeypatch.setattr(workspace_runtime, "resolve_workspace_runtime", resolve_runtime)
    monkeypatch.setattr(
        channel_agent_runtime,
        "resolve_channel_base_prompt",
        AsyncMock(return_value="base"),
    )
    monkeypatch.setattr(
        channel_agent_runtime,
        "runtime_prepare_prompt_appendix_for_turn",
        prepare_appendix,
    )
    monkeypatch.setattr(
        channel_agent_runtime,
        "runtime_execute_channel_agent_loop",
        execute_loop,
    )
    interrupted = AsyncMock(return_value=False)
    binding_is_valid = AsyncMock(return_value=False)
    monkeypatch.setattr(work_queue, "voice_work_was_interrupted", interrupted)
    monkeypatch.setattr(
        voice_binding,
        "twilio_call_binding_is_valid",
        binding_is_valid,
    )

    await channel_agent_runtime.run_channel_agent_turn(
        entity_id="entity",
        agent_id="agent",
        user_id="owner",
        conversation_id="conversation",
        current_message="Run the report",
        history=[],
        sender_ctx={"role": "external"},
        subscription=ResolvedSubscription(
            id="subscription",
            agent_id="agent",
            workspace_id="workspace",
            custom_prompt="You are helpful.",
        ),
        runtime_metadata={
            "voice_session_mode": "chat_gateway",
            "voice_origin_message_id": "message",
            "twilio_call_session_id": "call-session",
        },
    )

    assert await captured["is_cancelled"]() is True
    interrupted.assert_awaited_once()
    binding_is_valid.assert_awaited_once_with(
        SimpleNamespace(),
        "call-session",
        conversation_id="conversation",
    )


async def test_private_voice_chat_turn_uses_the_same_receipt_scoped_cancel(monkeypatch):
    from packages.core.ai.agentic_loop import AgenticResult
    from packages.core import database
    from packages.core.services import chat_service
    from packages.core.services.voice import work_queue

    captured = {}

    class DummySession:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def resolve_context(*_args, **_kwargs):
        return (
            "system",
            [],
            [],
            SimpleNamespace(
                workspace_id=None,
                task_id=None,
                runtime_envelope=None,
                tool_profile=None,
                allowed_tool_names=None,
                user=None,
                entity=None,
                model="openai/test",
                hinted_tool_names=set(),
            ),
        )

    async def execute_loop(**kwargs):
        captured.update(kwargs)
        assert await kwargs["is_cancelled"]() is True
        return AgenticResult(
            content="",
            messages=[],
            usage={},
            rounds=0,
            stop_reason="cancelled",
        )

    interrupted = AsyncMock(return_value=True)
    monkeypatch.setattr(database, "async_session", lambda: DummySession())
    monkeypatch.setattr(work_queue, "voice_work_was_interrupted", interrupted)
    monkeypatch.setattr(
        chat_service,
        "resolve_runtime_chat_context",
        resolve_context,
    )
    monkeypatch.setattr(
        chat_service,
        "runtime_execute_chat_agent_loop",
        execute_loop,
    )

    result = await chat_service.run_chat_message(
        "Run the report",
        "conversation",
        entity_id="entity",
        user_id="owner",
        runtime_metadata={
            "voice_session_mode": "chat_gateway",
            "origin_user_message_id": "message",
        },
    )

    assert callable(captured["is_cancelled"])
    interrupted.assert_awaited_once()
    assert interrupted.await_args.kwargs == {
        "message_id": "message",
        "conversation_id": "conversation",
    }
    assert result["stop_reason"] == "cancelled"
