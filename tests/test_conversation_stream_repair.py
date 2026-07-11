from __future__ import annotations

from packages.core.services.conversation_messages import assistant_stream_interrupted_meta


def test_assistant_stream_interrupted_meta_preserves_checkpoint_context() -> None:
    meta = assistant_stream_interrupted_meta({
        "stream_status": "streaming",
        "stream_checkpoint": True,
        "assistant_blocks": [{"type": "text", "phase": "opening", "text": "partial"}],
    })

    assert meta["stream_status"] == "interrupted"
    assert meta["stream_interrupted"] is True
    assert meta["stream_checkpoint"] is True
    assert meta["assistant_blocks"][0]["text"] == "partial"
