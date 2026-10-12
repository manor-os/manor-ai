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

    def __init__(self, base_url, timeout=None, api_token=None):
        self.base_url = base_url
        self.api_token = api_token
        self.calls: list = []
        self.closed = False
        _FakeClient.instances.append(self)

    async def write_file_base64(self, sandbox_id, path, content_base64, mkdir=True):
        self.calls.append((sandbox_id, path, content_base64))

    async def close(self):
        self.closed = True


class _Settings:
    SANDBOX_SERVICE_URL = "http://sandbox:8000"
    SANDBOX_API_TOKEN = "test-sandbox-token"


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
    assert client.api_token == "test-sandbox-token"
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


@pytest.mark.asyncio
async def test_binding_key_scopes_the_lookup_when_conversation_is_absent(monkeypatch):
    """A task-worker run has no conversation; the sandbox is keyed by its binding key."""

    _FakeClient.instances = []
    looked_up: list = []

    async def fake_load(binding_key):
        looked_up.append(binding_key)
        return {"sandbox_id": "sb1"} if binding_key == "U01ABC" else None

    monkeypatch.setattr("packages.core.ai.runtime.runtime_load_sandbox_context", fake_load)
    monkeypatch.setattr("packages.core.config.get_settings", lambda: _Settings())
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", _FakeClient)

    ok = await _deliver_image_to_sandbox(
        conversation_id=None,
        sandbox_binding_key="U01ABC",
        sandbox_path="images/page_01.png",
        image_bytes=b"png",
    )

    assert ok is True
    assert looked_up == ["U01ABC"]
    assert _FakeClient.instances[-1].calls[0][0] == "sb1"


@pytest.mark.asyncio
async def test_real_conversation_still_wins_over_a_null_binding_key(monkeypatch):
    _FakeClient.instances = []
    looked_up: list = []

    async def fake_load(binding_key):
        looked_up.append(binding_key)
        return {"sandbox_id": "sb1"}

    monkeypatch.setattr("packages.core.ai.runtime.runtime_load_sandbox_context", fake_load)
    monkeypatch.setattr("packages.core.config.get_settings", lambda: _Settings())
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", _FakeClient)

    assert (
        await _deliver_image_to_sandbox(
            conversation_id="c1",
            sandbox_binding_key=None,
            sandbox_path="x.png",
            image_bytes=b"x",
        )
        is True
    )
    assert looked_up == ["c1"]


@pytest.mark.asyncio
async def test_save_keeps_the_real_conversation_for_persistence(monkeypatch):
    """Only the sandbox lookup takes the key; persistence keeps the real conversation.

    The binding key is a sandbox scope, not an identity: a document row or a
    workspace binding stamped with it would record a conversation that does
    not exist.
    """

    from packages.core.ai.tools import extended_tools

    delivered: dict = {}
    document_origin: dict = {}

    async def fake_deliver(*, conversation_id, sandbox_binding_key, sandbox_path, image_bytes):
        delivered.update(
            {
                "conversation_id": conversation_id,
                "sandbox_binding_key": sandbox_binding_key,
                "sandbox_path": sandbox_path,
            }
        )
        return True

    async def fake_bill(**_kwargs):
        return None

    def fake_write(_entity_id, rel_path, _data, **_kwargs):
        document_origin["rel_path"] = rel_path
        return "/tmp/" + rel_path

    monkeypatch.setattr(extended_tools, "_deliver_image_to_sandbox", fake_deliver)
    monkeypatch.setattr(extended_tools, "_bill_media", fake_bill)
    monkeypatch.setattr(
        "packages.core.ai.runtime.file_actions.runtime_write_entity_file_atomic",
        fake_write,
    )

    image_url = await extended_tools._save_generated_image_bytes(
        entity_id="ent1",
        user_id="user1",
        prompt="a chart",
        model="gpt-image-1",
        size="1024x1024",
        image_bytes=b"\x89PNG",
        mime="image/png",
        is_byok=False,
        conversation_id=None,
        sandbox_binding_key="U01ABC",
        save_to_knowledge=False,
        sandbox_path="images/page_01.png",
    )

    assert image_url.startswith("/api/v1/fs/ent1/")
    # The sandbox lookup gets the key; the real conversation stays absent, and
    # it is the one every persistence argument below would have carried.
    assert delivered["sandbox_binding_key"] == "U01ABC"
    assert delivered["conversation_id"] is None
    assert delivered["sandbox_path"] == "images/page_01.png"
