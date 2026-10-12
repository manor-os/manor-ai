import base64
import sys
from pathlib import Path
from types import SimpleNamespace
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
        self.exec = AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="", stderr=""))


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
        captured["timeout"] = timeout
        return None

    monkeypatch.setattr("sandbox.docker_backend._run_docker", fake_run_docker)

    raw = b"\x89PNG\r\n\x1a\nbinary-bytes\x00\xff"
    n = await bridge.write_file_base64("images/page_01.png", base64.b64encode(raw).decode("ascii"))

    assert n == len(raw)
    assert bridge._sandbox.exec.call_args.kwargs["timeout"] == 30
    assert captured["timeout"] == 120
    assert captured["input"] == raw  # raw bytes piped, not utf-8 text
    bridge._sandbox.exec.assert_awaited_once_with("mkdir -p /skill/images", timeout=30)


@pytest.mark.asyncio
async def test_write_file_base64_rejects_invalid_base64(bridge):
    with pytest.raises(IOError):
        await bridge.write_file_base64("x.png", "not!valid!base64!!")


@pytest.mark.asyncio
async def test_write_file_base64_rejects_empty(bridge):
    with pytest.raises(IOError):
        await bridge.write_file_base64("x.png", base64.b64encode(b"").decode("ascii"))


@pytest.mark.asyncio
async def test_write_file_reports_an_unwritable_parent_directory(bridge):
    """A failed mkdir used to be ignored and resurface as an opaque 500 from cat."""
    bridge._sandbox.exec = AsyncMock(
        return_value=SimpleNamespace(
            exit_code=1, stdout="", stderr="mkdir: cannot create directory '/workspace': Read-only file system"
        )
    )
    with pytest.raises(IOError) as exc_info:
        await bridge.write_file("/workspace/build.py", "print(1)")
    message = str(exc_info.value)
    assert "Read-only file system" in message
    assert "/tmp" in message and "working directory" in message


@pytest.mark.asyncio
async def test_write_file_wraps_a_failed_pipe_as_a_caller_error(bridge, monkeypatch):
    import sandbox.docker_backend as docker_backend

    def failing(args, **_kwargs):
        raise RuntimeError("Docker command failed (exit 2): sh: 1: cannot create /x: Directory nonexistent")

    monkeypatch.setattr(docker_backend, "_run_docker", failing)
    with pytest.raises(IOError, match="Directory nonexistent"):
        await bridge.write_file("/skill/out/x", "data", mkdir=False)


@pytest.mark.asyncio
async def test_write_file_keeps_infrastructure_failures_as_faults(bridge, monkeypatch):
    import sandbox.docker_backend as docker_backend

    def daemon_down(args, **_kwargs):
        raise RuntimeError("Docker command failed (exit 1): Cannot connect to the Docker daemon")

    monkeypatch.setattr(docker_backend, "_run_docker", daemon_down)
    with pytest.raises(RuntimeError, match="Docker daemon"):
        await bridge.write_file("/skill/x", "data", mkdir=False)


@pytest.mark.asyncio
async def test_binary_delivery_tolerates_docker_startup_and_keeps_size_limit(bridge):
    raw = b"%PDF-1.7\nsource-preserving output"
    encoded = base64.b64encode(raw).decode()
    calls = []

    async def slow_start(command, timeout):
        calls.append(command)
        if command.startswith("stat"):
            if timeout < 7:
                raise RuntimeError("Docker startup exceeded the command deadline")
            return SimpleNamespace(exit_code=0, stdout=str(len(raw)), stderr="")
        return SimpleNamespace(exit_code=0, stdout=encoded, stderr="")

    bridge._sandbox.exec = slow_start
    content, size = await bridge.read_file_base64("output/pdf/Agreement copy.pdf", max_size=len(raw))
    assert base64.b64decode(content) == raw
    assert size == len(raw)
    assert "'/skill/output/pdf/Agreement copy.pdf'" in calls[0]

    calls.clear()
    with pytest.raises(IOError, match="File too large"):
        await bridge.read_file_base64("output/pdf/Agreement copy.pdf", max_size=len(raw) - 1)
    assert len(calls) == 1  # Reject before encoding or transferring oversized bytes.


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read_file", "stat", "list_dir", "remove"])
async def test_small_file_commands_include_docker_startup_budget(bridge, operation):
    async def slow_start(command, timeout):
        if timeout < 12:
            raise RuntimeError("Docker startup exceeded the command deadline")
        stdout = "regular file|5|1" if command.startswith("stat") else "hello"
        return SimpleNamespace(exit_code=0, stdout=stdout, stderr="")

    bridge._sandbox.exec = slow_start
    await getattr(bridge, operation)("tmp/pdfs/source.pdf")
