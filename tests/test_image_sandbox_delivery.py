import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.core.ai.tools.extended_tools import _deliver_image_to_sandbox  # noqa: E402


class _FakeClient:
    instances: list = []

    def __init__(self, base_url, timeout=None):
        self.base_url = base_url
        self.calls: list = []
        self.closed = False
        _FakeClient.instances.append(self)

    async def write_file_base64(self, sandbox_id, path, content_base64, mkdir=True):
        self.calls.append((sandbox_id, path, content_base64))

    async def close(self):
        self.closed = True


class _Settings:
    SANDBOX_SERVICE_URL = "http://sandbox:8000"


def _patch(monkeypatch, *, sandbox_ctx):
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_load_sandbox_context",
        AsyncMock(return_value=sandbox_ctx),
    )
    monkeypatch.setattr("packages.core.config.get_settings", lambda: _Settings())
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", _FakeClient)


@pytest.mark.asyncio
async def test_delivers_bytes_as_base64(monkeypatch):
    _FakeClient.instances = []
    _patch(monkeypatch, sandbox_ctx={"sandbox_id": "sb1"})

    raw = b"\x89PNGbinary\x00\xff payload"
    ok = await _deliver_image_to_sandbox(
        conversation_id="c1",
        sandbox_path="projects/p/images/page_01.png",
        image_bytes=raw,
    )

    assert ok is True
    client = _FakeClient.instances[-1]
    assert client.closed  # client always closed
    sid, path, b64 = client.calls[0]
    assert sid == "sb1"
    assert path == "projects/p/images/page_01.png"
    assert base64.b64decode(b64) == raw  # bytes survive the round-trip


@pytest.mark.asyncio
async def test_no_active_sandbox_is_noop(monkeypatch):
    _patch(monkeypatch, sandbox_ctx=None)
    ok = await _deliver_image_to_sandbox(
        conversation_id="c1",
        sandbox_path="x.png",
        image_bytes=b"x",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_missing_args_are_noops():
    assert await _deliver_image_to_sandbox(conversation_id=None, sandbox_path="x", image_bytes=b"x") is False
    assert await _deliver_image_to_sandbox(conversation_id="c", sandbox_path="", image_bytes=b"x") is False


@pytest.mark.asyncio
async def test_delivery_failure_is_swallowed(monkeypatch):
    # A delivery error must never bubble up and break image generation.
    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_load_sandbox_context",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    ok = await _deliver_image_to_sandbox(
        conversation_id="c1",
        sandbox_path="x.png",
        image_bytes=b"x",
    )
    assert ok is False
