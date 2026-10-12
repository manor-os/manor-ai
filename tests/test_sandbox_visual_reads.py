"""Rendered sandbox pages reach vision as images, after owner authorization."""
from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from packages.core.ai.agentic_loop import _file_image_observation
from packages.core.ai.tools import file_tools, sandbox_tools


def image_bytes(format_name):
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 255, 255)).save(stream, format=format_name)
    return stream.getvalue()


@pytest.fixture
def client(monkeypatch):
    fake = SimpleNamespace(
        read_file_base64=AsyncMock(), read_file=AsyncMock(), close=AsyncMock(),
    )
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", AsyncMock(return_value=None))
    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", AsyncMock(return_value=fake))
    return fake


async def read_image(path="/skill/tmp/pdfs/page.png"):
    return await sandbox_tools._sandbox_handler(
        entity_id="entity", user_id="owner", conversation_id="chat",
        action="read_file", params={"sandbox_id": "owned-sandbox", "path": path},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extension,format_name,mime", [
    ("PNG", "PNG", "image/png"), ("jpg", "JPEG", "image/jpeg"),
    ("jpeg", "JPEG", "image/jpeg"), ("webp", "WEBP", "image/webp"),
])
@pytest.mark.parametrize("tool_name", ["sandbox", "sandbox_read_file", "read_file"])
async def test_rendered_page_is_a_visual_observation_with_safe_text_result(
    client, extension, format_name, mime, tool_name,
):
    raw = image_bytes(format_name)
    path = f"/skill/tmp/pdfs/page.{extension}"
    encoded = base64.b64encode(raw).decode()
    client.read_file_base64.return_value = SimpleNamespace(path=path, size=len(raw), content_base64=encoded)

    result = await read_image(path)
    payload = json.loads(result)
    assert "content" not in payload
    assert payload["read_mode"] == "image"
    assert payload["image"]["mime_type"] == mime
    client.read_file_base64.assert_awaited_once_with(
        sandbox_id="owned-sandbox", path=path, max_size=file_tools.READ_FILE_MAX_IMAGE_BYTES,
    )
    client.read_file.assert_not_awaited()
    client.close.assert_awaited_once()

    observation, safe_result = _file_image_observation(tool_name, result)
    assert observation["role"] == "user"
    assert "Untrusted" in observation["content"][0]["text"]
    assert observation["content"][1]["image_url"]["url"] == f"data:{mime};base64,{encoded}"
    assert encoded not in safe_result
    assert json.loads(safe_result)["image"]["delivery"] == "ephemeral_multimodal"


@pytest.mark.asyncio
async def test_visual_read_rejects_other_owner_before_contacting_sandbox(client, monkeypatch):
    denied = json.dumps({"error": {"code": "sandbox_access_denied"}})
    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", AsyncMock(return_value=denied))
    assert await read_image() == denied
    sandbox_tools._get_client_for_sandbox.assert_not_awaited()
    client.read_file_base64.assert_not_awaited()


@pytest.mark.asyncio
async def test_visual_read_checks_size_even_if_service_returns_oversized_metadata(client):
    raw = image_bytes("PNG")
    client.read_file_base64.return_value = SimpleNamespace(
        path="page.png", size=file_tools.READ_FILE_MAX_IMAGE_BYTES + 1,
        content_base64=base64.b64encode(raw).decode(),
    )
    payload = json.loads(await read_image())
    assert payload["error"]["code"] == "sandbox_read_failed"
    assert "image" not in payload
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_visual_read_rejects_invalid_base64(client):
    client.read_file_base64.return_value = SimpleNamespace(path="page.png", size=4, content_base64="!bad!")
    payload = json.loads(await read_image())
    assert payload["error"]["code"] == "sandbox_read_failed"
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_visual_projection_rejects_mismatched_file_signature(client):
    raw = image_bytes("PNG")
    client.read_file_base64.return_value = SimpleNamespace(
        path="page.jpg", size=len(raw), content_base64=base64.b64encode(raw).decode(),
    )
    observation, safe_result = _file_image_observation("sandbox", await read_image("page.jpg"))
    assert observation is None
    assert json.loads(safe_result)["image"]["reason"] == "invalid_image_signature"
    assert "base64" not in safe_result


@pytest.mark.asyncio
async def test_sandbox_text_reads_keep_pagination(client):
    client.read_file.return_value = SimpleNamespace(
        path="/skill/SKILL.md", content="x" * 10001, size=10001, truncated=False,
    )
    payload = json.loads(await read_image("/skill/SKILL.md"))
    assert len(payload["content"]) == 10000
    assert payload["next_offset"] == 10000
    assert payload["truncated"] is True
    client.read_file_base64.assert_not_awaited()
