"""An image read by read_file reaches the model as pixels, or says why not.

Driven through the real Runtime harness and agentic loop with the real
read_file handler over a real Knowledge Document; only the model round is
faked. Regression for the 2026-10-13 video QA eval: frame PNGs read by
``document_id`` left the model with transcript text that looked like bare
metadata, and it judged frames it never saw.
"""
from __future__ import annotations

import importlib
import json
import struct
import zlib

import pytest

import packages.core.database as db_module
from packages.core.ai.runtime.tool_gateway import call_tool_schema
from packages.core.ai.tools import file_tools
from packages.core.config import get_settings
from packages.core.models.base import generate_ulid
from packages.core.models.user import Entity, User, UserMembership
from packages.core.services.auth_service import hash_password
from packages.core.services.document_service import create_document

FRAME_PATH = "Video-Analysis/frames/frame-003-000028000ms.png"


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")
    )


@pytest.fixture
async def frame(tmp_path, db_session):
    # db_session prepares the schema; rows go through the module session the
    # read_file handler itself uses.
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    entity_id = generate_ulid()
    target = tmp_path / entity_id / FRAME_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(_png())
    async with db_module.async_session() as db:
        db.add(Entity(id=entity_id, name="Frame QA entity"))
        await db.flush()
        user = User(
            entity_id=entity_id, email=f"qa-{entity_id.lower()}@test.com", display_name="QA",
            password_hash=hash_password("pass123"), role="owner", status="active",
        )
        db.add(user)
        await db.flush()
        db.add(UserMembership(user_id=user.id, entity_id=entity_id, role="owner", status="active", is_primary=True))
        document = await create_document(
            db, entity_id, name=target.name, fs_path=FRAME_PATH, visibility="entity", created_by=user.id, owner_id=user.id,
        )
        ids = {"entity_id": entity_id, "user_id": user.id, "document_id": document.id}
        await db.commit()
    try:
        yield ids
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


def _image_urls(messages: list[dict]) -> list[str]:
    return [
        block["image_url"]["url"]
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]


async def _run(monkeypatch, frame, call: dict, *, model=None, rounds_before_answer: int = 0):
    """One read round, then the model answers (after ``rounds_before_answer`` empty rounds)."""

    requests: list[list[dict]] = []

    async def fake_round(messages, tools, **_kwargs):
        requests.append(messages)
        if len(requests) == 1:
            return "", [call], {"prompt": 1, "completion": 1, "total": 2}
        if len(requests) <= 1 + rounds_before_answer:
            return "", None, {}  # provider returned nothing at all
        return "Frame checked.", None, {"prompt": 1, "completion": 1, "total": 2}

    loop_module = importlib.import_module("packages.core.ai.agentic_loop")
    monkeypatch.setattr(loop_module, "runtime_execute_agentic_round_tool_completion", fake_round)
    completions_module = importlib.import_module("packages.core.ai.runtime.completions")
    billing_module = importlib.import_module("packages.core.ai.runtime.billing")

    async def requested_route(*, model=None, **_kwargs):
        return model, {}, None

    monkeypatch.setattr(completions_module, "runtime_resolve_text_completion_route", requested_route)
    monkeypatch.setattr(billing_module, "runtime_ensure_billing_context", lambda *_a, **_k: None)

    async def execute(name, arguments):
        assert name == "read_file"  # call_tool is unwrapped before execution
        return await file_tools._read_file(frame["entity_id"], user_id=frame["user_id"], **arguments)

    gateway = call["name"] == "call_tool"
    harness = importlib.import_module("packages.core.ai.runtime.harness")
    result = await harness.runtime_execute_agentic_loop(
        runtime_envelope=None,
        system_prompt="Visually verify rendered video frames.",
        user_message="Check the settled sphere frame.",
        tools=[call_tool_schema()] if gateway else [file_tools.READ_FILE_SCHEMA],
        gateway_tool_schemas=[file_tools.READ_FILE_SCHEMA] if gateway else None,
        entity_id=frame["entity_id"],
        user_id=frame["user_id"],
        agent_id=None,
        tool_executor=execute,
        model=model,
        max_rounds=5,
    )
    tool_results = [
        json.loads(message["content"])
        for message in result.messages
        if message.get("role") == "tool"
    ]
    assert len(tool_results) == 1
    return requests, tool_results[0], result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "wrapped"),
    [("document_id", False), ("document_id", True), ("path", False)],
    ids=["document_id", "call_tool_document_id", "path"],
)
async def test_image_read_delivers_pixels_to_the_next_model_request(monkeypatch, frame, wrapped, address):
    arguments = {"document_id": frame["document_id"]} if address == "document_id" else {"path": FRAME_PATH}
    call = (
        {"id": "read-1", "name": "call_tool", "arguments": {"name": "read_file", "arguments": arguments}}
        if wrapped
        else {"id": "read-1", "name": "read_file", "arguments": arguments}
    )

    requests, tool_result, result = await _run(monkeypatch, frame, call)

    assert len(requests) == 2
    assert _image_urls(requests[0]) == []
    [data_url] = _image_urls(requests[1])
    assert data_url.startswith("data:image/png;base64,iVBORw0KGgo")
    # The transcript states the delivery truthfully and never keeps the bytes.
    assert tool_result["image"]["delivered"] is True
    assert "next model request" in tool_result["image"]["note"]
    assert tool_result["resolved_path"] == FRAME_PATH
    assert data_url not in json.dumps(result.messages)


@pytest.mark.asyncio
async def test_pixels_survive_a_model_round_that_returned_nothing(monkeypatch, frame):
    """The retry of an empty round still carries the image it never answered."""

    call = {"id": "read-1", "name": "read_file", "arguments": {"document_id": frame["document_id"]}}
    requests, tool_result, _result = await _run(monkeypatch, frame, call, rounds_before_answer=1)

    assert len(requests) == 3
    assert len(_image_urls(requests[1])) == 1
    assert len(_image_urls(requests[2])) == 1
    assert tool_result["image"]["delivered"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_arguments", "model", "reason"),
    [
        ({"include_image": False}, None, "include_image_false"),
        ({}, "deepseek/deepseek-v4-flash", "model_does_not_accept_images"),
    ],
    ids=["include_image_false", "text_only_model"],
)
async def test_undelivered_image_read_says_why(monkeypatch, frame, extra_arguments, model, reason):
    call = {
        "id": "read-1", "name": "read_file",
        "arguments": {"document_id": frame["document_id"], **extra_arguments},
    }

    requests, tool_result, _result = await _run(monkeypatch, frame, call, model=model)

    assert _image_urls(requests[1]) == []
    assert tool_result["image"]["delivered"] is False
    assert tool_result["image"]["reason"] == reason
    assert "No image pixels were sent" in tool_result["image"]["note"]
    assert tool_result["source_sha256"]
