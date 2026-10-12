"""Exercise actual ASGI disconnect cancellation, not only generator.aclose()."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from starlette.responses import StreamingResponse

from packages.core.ai.runtime import ChatSurface
from packages.core.services import chat_service


@pytest.mark.asyncio
async def test_http_disconnect_shields_interrupted_persistence_from_cancel_scope():
    disconnect = anyio.Event()
    save_attempts = []
    saved = []
    published = []

    async def publish(channel, payload):
        published.append((channel, payload))

    redis = SimpleNamespace(publish=publish)

    async def context(*args, **kwargs):
        return 'system', [], [], SimpleNamespace(
            workspace_id=None, task_id=None, runtime_envelope=None,
            tool_profile=None, allowed_tool_names=None, user=None, entity=None,
        )

    async def idle_loop(**kwargs):
        await kwargs['stream_handler']('process_note', {'content': 'working'})
        await asyncio.Event().wait()
        raise AssertionError('Idle loop should be cancelled on disconnect')

    async def interrupted_save(**kwargs):
        save_attempts.append(kwargs)
        # A real database await is a cancellation checkpoint. A synchronous
        # AsyncMock result would incorrectly hide the ASGI cancellation bug.
        await anyio.lowlevel.checkpoint()
        saved.append(kwargs)
        return 'assistant-1'

    async def send(event):
        if event['type'] == 'http.response.body' and b'event: process_note' in event.get('body', b''):
            disconnect.set()

    async def receive():
        await disconnect.wait()
        return {'type': 'http.disconnect'}

    with (
        patch.object(chat_service, 'get_chat_turn_cancel_generation', new=AsyncMock(return_value=0)),
        patch.object(chat_service, 'resolve_runtime_chat_context', new=context),
        patch.object(chat_service, 'runtime_execute_chat_agent_loop', new=idle_loop),
        patch.object(chat_service, 'save_or_update_assistant_stream_message', new=AsyncMock(return_value='assistant-1')),
        patch.object(chat_service, 'save_assistant_stream_error_message', new=AsyncMock()),
        patch.object(chat_service, 'save_assistant_stream_interrupted_message', new=interrupted_save),
        patch.object(chat_service, 'runtime_persist_chat_stream_runtime_events', new=AsyncMock()),
        patch.object(chat_service, 'record_chat_llm_usage', new=AsyncMock()),
        patch('packages.core.cache._get_redis', new=AsyncMock(return_value=redis)),
    ):
        source = chat_service.stream_chat_response(
            'Read my properties', 'conversation-1', entity_id='entity-1', user_id='user-1',
            assistant_message_id='assistant-1', persist_messages=True,
            runtime_surface=ChatSurface.GLOBAL_OWNER_CHAT,
        )
        response = StreamingResponse(source, media_type='text/event-stream')
        # Starlette's pre-2.4 ASGI path receives http.disconnect and cancels
        # its task group's AnyIO scope, as a browser/navigation disconnect can.
        with anyio.fail_after(3):
            await response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send)
        pending = list(chat_service._SNAPSHOT_PUBLISH_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert len(save_attempts) == 1
    assert len(saved) == 1, 'An actual HTTP disconnect must finish the interrupted row write'
    assert saved[0]['message_id'] == 'assistant-1'
    snapshots = [json.loads(payload)['data']['status'] for _, payload in published
                 if json.loads(payload).get('event') == 'chat_stream_snapshot']
    assert 'done' not in snapshots
    assert snapshots and snapshots[-1] == 'interrupted'
