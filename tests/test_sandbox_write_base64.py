import base64
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SERVICE = ROOT / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE))

# The service imports its own top-level ``config``. Another test module may
# already have bound that name to a different file — packages/core/ai/skills/
# pptx/scripts/config.py is also called ``config`` — and a sys.path insert
# does nothing about an entry already in sys.modules. Whoever imported first
# wins, so this passed alone and failed in a full collection.
sys.modules.pop("config", None)

fs_bridge = pytest.importorskip("sandbox.fs_bridge")


class _FakeConfig:
    workdir = "/skill"


class _FakeSandbox:
    container_name = "test-container"
    config = _FakeConfig()

    def __init__(self) -> None:
        self.exec = AsyncMock(return_value=None)


@pytest.fixture()
def bridge():
    b = fs_bridge.FsBridge(_FakeSandbox())
    return b


@pytest.mark.asyncio
async def test_write_file_base64_pipes_decoded_bytes(bridge, monkeypatch):
    captured = {}

    def fake_run_docker(args, input_data=None, timeout=None):
        captured["args"] = args
        captured["input"] = input_data
        return None

    monkeypatch.setattr("sandbox.docker_backend._run_docker", fake_run_docker)

    raw = b"\x89PNG\r\n\x1a\nbinary-bytes\x00\xff"
    n = await bridge.write_file_base64("images/page_01.png", base64.b64encode(raw).decode("ascii"))

    assert n == len(raw)
    assert captured["input"] == raw  # raw bytes piped, not utf-8 text


@pytest.mark.asyncio
async def test_write_file_base64_rejects_invalid_base64(bridge):
    with pytest.raises(IOError):
        await bridge.write_file_base64("x.png", "not!valid!base64!!")


@pytest.mark.asyncio
async def test_write_file_base64_rejects_empty(bridge):
    with pytest.raises(IOError):
        await bridge.write_file_base64("x.png", base64.b64encode(b"").decode("ascii"))
