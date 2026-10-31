from __future__ import annotations

import asyncio
import base64
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))

from sandbox import docker_backend  # noqa: E402
from sandbox.docker_backend import DockerSandbox  # noqa: E402
from sandbox.models import ContainerConfig, ExecRequest, SandboxStatus  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402


def test_exec_timeout_allows_one_hour():
    assert ExecRequest(command="true", timeout=3600).timeout == 3600
    with pytest.raises(Exception):
        ExecRequest(command="true", timeout=3601)


@pytest.mark.asyncio
async def test_read_file_range_parses_size_header(monkeypatch):
    calls = []

    def fake_run(args, timeout=None, allow_failure=False, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b"11\nhello", stderr=b"")

    monkeypatch.setattr(docker_backend, "_run_docker", fake_run)
    sbx = DockerSandbox("s1", ContainerConfig())
    sbx.container_name = "ctr"
    data, size = await sbx.read_file_range("/work/.agent/activity.jsonl", 3, 5)
    assert (data, size) == (b"hello", 11)
    assert calls[0][:4] == ["docker", "exec", "ctr", "python"]
    assert calls[0][-3:] == ["/work/.agent/activity.jsonl", "3", "5"]


@pytest.mark.asyncio
async def test_read_file_range_missing_file(monkeypatch):
    monkeypatch.setattr(
        docker_backend,
        "_run_docker",
        lambda args, timeout=None, allow_failure=False, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=b"-1\n", stderr=b""),
    )
    sbx = DockerSandbox("s1", ContainerConfig())
    with pytest.raises(FileNotFoundError):
        await sbx.read_file_range("/work/.agent/x", 0, 10)


@pytest.mark.asyncio
async def test_read_file_range_works_while_a_command_holds_the_sandbox(monkeypatch):
    """A background exec holds the sandbox's command claim for its whole run; tailing must not need it."""
    monkeypatch.setattr(
        docker_backend,
        "_run_docker",
        lambda args, timeout=None, allow_failure=False, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=b"3\nabc", stderr=b""),
    )
    sbx = DockerSandbox("s1", ContainerConfig())
    sbx.container_name = "ctr"
    sbx.status = SandboxStatus.READY
    sbx.claim_command("exec-bg", "python long.py", 3600)
    assert sbx.holds_command("exec-bg")
    data, size = await asyncio.wait_for(sbx.read_file_range("/work/.agent/a", 0, 10), timeout=2)
    assert (data, size) == (b"abc", 3)


@pytest.fixture
def tail_runner():
    runner = SkillRunner()
    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "ctr"
    sandbox.status = SandboxStatus.EXECUTING
    seen = []

    async def fake_range(path, offset, limit):
        seen.append(path)
        blob = b"0123456789"
        return blob[offset:offset + limit], len(blob)

    sandbox.read_file_range = fake_range
    runner._sandboxes[sandbox.sandbox_id] = sandbox
    runner.seen_paths = seen
    return runner, sandbox


@pytest.fixture
def tail_client(monkeypatch, tail_runner):
    from fastapi.testclient import TestClient

    runner, _sandbox = tail_runner
    spec = importlib.util.spec_from_file_location("_sandbox_service_main_tail_test", SANDBOX_SERVICE_ROOT / "main.py")
    main = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, main)
    spec.loader.exec_module(main)
    from api import routes

    monkeypatch.setattr(main.config, "API_TOKEN", "t")
    monkeypatch.setattr(routes, "_runner", runner)
    client = TestClient(main.app, raise_server_exceptions=False)
    yield client
    client.close()


def test_tail_route_pages_by_offset_even_while_executing(tail_client):
    h = {"X-Manor-Sandbox-Token": "t"}
    r = tail_client.post(
        "/api/v1/sandbox/sandbox-1/files/tail",
        json={"path": "/work/.agent/activity.jsonl", "offset": 4, "limit": 3},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert base64.b64decode(body["data_base64"]) == b"456"
    assert (body["next_offset"], body["size"], body["eof"]) == (7, 10, False)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/work/.agent/../../etc/passwd",
        "/work/.agent/../secret",
        "/work/.agentx/log",
        "/work/.agent",
        "work/.agent/activity.jsonl",
    ],
)
def test_tail_route_rejects_paths_outside_allowed_prefix(tail_client, tail_runner, path):
    r = tail_client.post(
        "/api/v1/sandbox/sandbox-1/files/tail",
        json={"path": path, "offset": 0, "limit": 3},
        headers={"X-Manor-Sandbox-Token": "t"},
    )
    assert r.status_code == 400, r.text
    assert tail_runner[0].seen_paths == []


def test_tail_route_reports_missing_sandbox(tail_client):
    r = tail_client.post(
        "/api/v1/sandbox/nope/files/tail",
        json={"path": "/work/.agent/activity.jsonl", "offset": 0, "limit": 3},
        headers={"X-Manor-Sandbox-Token": "t"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_tail_file_normalizes_path_and_skips_availability_guard(tail_runner):
    runner, sandbox = tail_runner
    sandbox.ensure_available = lambda: (_ for _ in ()).throw(AssertionError("tail must not gate on availability"))
    # The fixture's sandbox is EXECUTING: a running command holds it.
    result = await asyncio.wait_for(
        runner.tail_file("sandbox-1", "/work/.agent//sub/../activity.jsonl", 0, 10), timeout=2
    )
    assert result.path == "/work/.agent/activity.jsonl"
    assert runner.seen_paths == ["/work/.agent/activity.jsonl"]
    assert result.eof is True


def test_tail_route_rejects_null_byte_path(tail_client, tail_runner):
    r = tail_client.post(
        "/api/v1/sandbox/sandbox-1/files/tail",
        json={"path": "/work/.agent/a\u0000b", "offset": 0, "limit": 3},
        headers={"X-Manor-Sandbox-Token": "t"},
    )
    assert r.status_code == 400, r.text
    assert tail_runner[0].seen_paths == []


def test_tail_route_rejects_overlong_path_and_huge_offset(tail_client):
    h = {"X-Manor-Sandbox-Token": "t"}
    url = "/api/v1/sandbox/sandbox-1/files/tail"
    long_path = "/work/.agent/" + "a" * 4100
    assert tail_client.post(url, json={"path": long_path, "offset": 0, "limit": 3}, headers=h).status_code == 422
    huge = {"path": "/work/.agent/activity.jsonl", "offset": 2**62 + 1, "limit": 3}
    assert tail_client.post(url, json=huge, headers=h).status_code == 422


@pytest.mark.asyncio
async def test_tail_file_offset_past_end_clamps_next_offset(tail_runner):
    runner, _sandbox = tail_runner
    result = await runner.tail_file("sandbox-1", "/work/.agent/activity.jsonl", 50, 10)
    assert base64.b64decode(result.data_base64) == b""
    assert (result.next_offset, result.size, result.eof) == (10, 10, True)


@pytest.mark.asyncio
async def test_tail_file_next_offset_is_exact_when_log_grows_mid_read(tail_runner):
    runner, sandbox = tail_runner

    async def growing_range(path, offset, limit):
        # size was stat'd as 10, but the file grew before the read finished.
        return b"456789ABCDEF"[: limit], 10

    sandbox.read_file_range = growing_range
    result = await runner.tail_file("sandbox-1", "/work/.agent/activity.jsonl", 4, 12)
    data = base64.b64decode(result.data_base64)
    assert data == b"456789ABCDEF"
    assert result.next_offset == 4 + len(data)
    assert result.size == 10
    assert result.eof is True
