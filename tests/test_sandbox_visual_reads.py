"""Rendered sandbox pages reach vision as images, after owner authorization."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from packages.core.ai import agentic_loop as loop_module
from packages.core.ai.agentic_loop import (
    FILE_VISUAL_HISTORY_MAX_BYTES,
    _file_image_observation,
    _file_visual_context_tail,
    _remember_file_visual_observation,
)
from packages.core.ai.runtime.file_images import FILE_IMAGE_DELIVERY_CONTRACT
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
    assert json.loads(safe_result)["image"]["delivered"] is True


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


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    }


def _image_read(name: str, raw: bytes) -> str:
    return json.dumps({
        "path": name, "resolved_path": name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "image": {
            "data_url": "data:image/png;base64," + base64.b64encode(raw).decode(),
            "bytes": len(raw),
            "delivery": FILE_IMAGE_DELIVERY_CONTRACT,
        },
    })


@pytest.mark.asyncio
async def test_file_images_remain_available_for_a_later_multiturn_comparison(monkeypatch):
    first, second = image_bytes("PNG"), image_bytes("PNG") + b"second"
    calls: list[list[dict]] = []

    async def fake_completion(messages, tools, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return "", [{"id": "first", "name": "read_file", "arguments": {"path": "product-front.png"}}], {}
        if len(calls) == 2:
            return "", [{"id": "second", "name": "read_file", "arguments": {"path": "invoice-page.png"}}], {}
        return "comparison complete", None, {}

    async def execute(_name, args):
        return _image_read(args["path"], first if args["path"] == "product-front.png" else second)

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_completion)
    result = await loop_module.agentic_loop(
        system_prompt="Compare supplied product and invoice images.", user_message="compare",
        tools=[_tool("read_file")], tool_executor=execute, max_rounds=4,
        metadata={"context_window_tokens": 64_000},
    )

    assert result.content == "comparison complete"
    third_request = calls[2]
    images = [block for message in third_request for block in (message.get("content") if isinstance(message.get("content"), list) else []) if block.get("type") == "image_url"]
    assert len(images) == 2
    assert any("product-front.png" in str(message.get("content")) for message in third_request)
    assert any("invoice-page.png" in str(message.get("content")) for message in third_request)
    assert all("data:image/png;base64" not in str(message.get("content")) for message in result.messages)


@pytest.mark.asyncio
async def test_batch_rereads_of_more_frames_than_fit_end_instead_of_looping(monkeypatch):
    """Production loop: a review read 7+ frames per round while only 4 are
    kept, so the first ones were evicted unseen and re-read forever. Only
    deliverable pixels are sent, re-reads are capped, and the turn stops."""
    frames = {f"frame-{index}.png": image_bytes("PNG") + bytes([index]) for index in range(7)}
    requests: list[list[dict]] = []

    async def fake_completion(messages, tools, **kwargs):
        requests.append(messages)
        return "", [
            {"id": f"r{len(requests)}-{name}", "name": "read_file", "arguments": {"path": name}}
            for name in frames
        ], {}

    async def final_completion(messages, **kwargs):
        requests.append(messages)
        return "review incomplete", {}

    async def execute(_name, args):
        return _image_read(args["path"], frames[args["path"]])

    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_completion)
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_final_completion", final_completion)
    result = await loop_module.agentic_loop(
        system_prompt="Review the frames.", user_message="review",
        tools=[_tool("read_file")], tool_executor=execute, max_rounds=50,
        metadata={"context_window_tokens": 128_000},
    )

    def images(messages):
        return [
            block for message in messages
            for block in (message.get("content") if isinstance(message.get("content"), list) else [])
            if block.get("type") == "image_url"
        ]

    assert all(len(images(request)) <= loop_module.FILE_VISUAL_HISTORY_MAX_IMAGES for request in requests)
    first_round = [
        json.loads(message["content"])["image"] for message in requests[1] if message.get("role") == "tool"
    ]
    assert [image["delivered"] for image in first_round] == [True] * 4 + [False] * 3
    assert {image.get("reason") for image in first_round[4:]} == {"round_image_limit"}
    assert "reread_limit" in str(requests[-1])
    assert result.rounds < 20
    assert result.error_detail == {"stop": "no_progress"}
    assert "tool use has stopped" in str(requests[-1])


def test_file_visual_tail_evicts_oldest_images_when_request_budget_is_small():
    observation = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
    history = [
        {"identity": "older", "path": "product-front.png", "document_id": "product-doc", "bytes": 10, "observation": observation},
        {"identity": "newer", "path": "invoice-page.png", "document_id": "invoice-doc", "bytes": 10, "observation": observation},
    ]
    tail = _file_visual_context_tail(history, base_messages=[], tools=[], output_reserve=1_000, window=10_000)
    assert len([message for message in tail if isinstance(message.get("content"), list)]) == 1
    assert "invoice-page.png" in " ".join(str(message.get("content")) for message in tail)


def test_file_visual_tail_reports_when_no_image_fits_the_request_budget():
    observation = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
    tail = _file_visual_context_tail(
        [{"identity": "invoice", "path": "invoice-page.png", "document_id": "invoice-doc", "bytes": 10, "observation": observation}],
        base_messages=[], tools=[], output_reserve=1_000, window=8_000,
    )
    assert len(tail) == 1
    assert "no image can fit" in tail[0]["content"]
    assert _file_visual_context_tail(
        [], base_messages=[], tools=[], output_reserve=1_000, window=8_000,
    ) == []


def test_file_visual_history_deduplicates_and_evicts_to_its_decoded_byte_limit():
    history = []
    observation = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}
    for index, sha in enumerate(("a" * 64, "b" * 64, "a" * 64)):
        _remember_file_visual_observation(history, observation, json.dumps({
            "document_id": f"document-{index}", "resolved_path": f"invoice-{index}.png",
            "source_sha256": sha,
            "image": {"bytes": FILE_VISUAL_HISTORY_MAX_BYTES // 2 + 1},
        }))
    assert [entry["identity"] for entry in history] == ["a" * 64]
    assert history[0]["document_id"] == "document-2"


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
