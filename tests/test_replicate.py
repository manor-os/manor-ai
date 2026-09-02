from __future__ import annotations

import pytest


class _Response:
    status_code = 200
    text = "{}"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "id": "prediction-1",
            "status": "succeeded",
            "model": "black-forest-labs/flux-schnell",
            "input": {"prompt": "a test image"},
            "output": ["https://replicate.delivery/test.png"],
        }


class _Client:
    requests: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url: str, *, headers: dict, json: dict):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return _Response()


@pytest.mark.asyncio
async def test_replicate_image_caps_num_outputs_before_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": "a test image", "num_outputs": 99},
        "r8-test",
    )

    assert result["isError"] is False
    assert _Client.requests[0]["json"]["input"]["num_outputs"] == 4
    assert "prediction-1" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_replicate_rejects_empty_image_prompt_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": "   "},
        "r8-test",
    )

    assert result["isError"] is True
    assert "prompt" in result["content"][0]["text"].lower()
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_non_string_prompt_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": 123},
        "r8-test",
    )

    assert result["isError"] is True
    assert "prompt" in result["content"][0]["text"].lower()
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_run_model_rejects_non_object_input_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "run_model",
        {"model": "owner/model", "input": []},
        "r8-test",
    )

    assert result["isError"] is True
    assert "input" in result["content"][0]["text"].lower()
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_boolean_num_outputs_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": "a test image", "num_outputs": True},
        "r8-test",
    )

    assert result["isError"] is True
    assert "integer" in result["content"][0]["text"]
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_fractional_num_outputs_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": "a test image", "num_outputs": 1.5},
        "r8-test",
    )

    assert result["isError"] is True
    assert "integer" in result["content"][0]["text"]
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_zero_num_outputs_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": "a test image", "num_outputs": 0},
        "r8-test",
    )

    assert result["isError"] is True
    assert "at least 1" in result["content"][0]["text"]
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_boolean_duration_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_video",
        {"prompt": "a test video", "duration": True},
        "r8-test",
    )

    assert result["isError"] is True
    assert "integer" in result["content"][0]["text"]
    assert _Client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, -1, "0", "-1"])
async def test_replicate_rejects_non_positive_duration_without_submission(monkeypatch, duration):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_video",
        {"prompt": "a test video", "duration": duration},
        "r8-test",
    )

    assert result["isError"] is True
    assert "greater than 0" in result["content"][0]["text"]
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_blank_token_without_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(
        "generate_image",
        {"prompt": "a test image"},
        "   ",
    )

    assert result["isError"] is True
    assert "token is missing" in result["content"][0]["text"].lower()
    assert _Client.requests == []


@pytest.mark.asyncio
async def test_replicate_rejects_non_object_arguments_before_submission(monkeypatch):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool("generate_image", [], "r8-test")

    assert result["isError"] is True
    assert "object" in result["content"][0]["text"].lower()
    assert _Client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("generate_image", {"prompt": "test", "seed": 1.5}, "seed"),
        ("generate_image", {"prompt": "test", "seed": True}, "seed"),
        ("generate_video", {"prompt": "test", "model": 123}, "model"),
        ("generate_video", {"prompt": "test", "aspect_ratio": 123}, "aspect_ratio"),
    ],
)
async def test_replicate_rejects_invalid_argument_types_before_submission(
    monkeypatch, tool, arguments, field
):
    from packages.core.ai.mcp import replicate

    _Client.requests = []
    monkeypatch.setattr(replicate.httpx, "AsyncClient", _Client)

    result = await replicate.call_tool(tool, arguments, "r8-test")

    assert result["isError"] is True
    assert field in result["content"][0]["text"].lower()
    assert _Client.requests == []
