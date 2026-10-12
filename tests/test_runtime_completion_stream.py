import json
from types import SimpleNamespace

import pytest

from apps.api.routers.chat import _durable_runtime_event_stream
from packages.core.services.runtime_event_stream import parse_sse_frame


@pytest.mark.unit
@pytest.mark.parametrize("publication_succeeds", [True, False])
async def test_terminal_stream_waits_briefly_for_commit_then_publication(
    monkeypatch, publication_succeeds,
):
    run = SimpleNamespace(
        id="run-completion",
        conversation_id="conversation-completion",
        assistant_message_id="message-completion",
        status="running",
        human_input_request_id=None,
    )
    text_frame = 'id: 1-0\nevent: text_delta\ndata: {"content":"Done"}\n\n'
    end_frame = 'id: 2-0\nevent: stream_end\ndata: {"persisted":true}\n\n'
    reads = []

    async def read_events(run_id, *, after_id, block_ms=4000):
        assert run_id == run.id
        reads.append((after_id, block_ms))
        if len(reads) == 1:
            return [("1-0", text_frame)]
        if block_ms == 1000 and publication_succeeds:
            return [("2-0", end_frame)]
        return []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, _model, run_id):
            assert run_id == run.id
            return SimpleNamespace(status="completed", status_reason=None)

    monkeypatch.setattr("packages.core.database.async_session", Session)
    monkeypatch.setattr(
        "packages.core.services.runtime_event_stream.read_runtime_sse_events",
        read_events,
    )

    frames = [frame async for frame in _durable_runtime_event_stream(run)]

    assert frames[1] == text_frame
    assert reads[:3] == [("0-0", 4000), ("1-0", None), ("1-0", 1000)]
    if publication_succeeds:
        assert frames[-1] == end_frame
        assert reads[3:] == [("2-0", None)]
    else:
        event, payload = parse_sse_frame(frames[-1])
        assert event == "runtime_status"
        assert json.loads(payload)["status"] == "completed"
        assert len(reads) == 3
