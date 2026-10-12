"""A render owns its execution sandbox, never the editable session's processes."""

import asyncio
import base64
import json
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import video_edit_tools


def test_native_render_uses_review_browser_mode_and_preserves_quality_gate(tmp_path, monkeypatch):
    runner_path = Path(__file__).parents[1] / "packages/core/ai/skills/video-edit/scripts/video_runtime.py"
    spec = importlib.util.spec_from_file_location("video_runtime_render_test", runner_path)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    commands, emitted = [], []
    monkeypatch.setattr(runtime, "PROJECT", tmp_path)
    monkeypatch.setattr(runtime, "_check", lambda: subprocess.CompletedProcess([], 0, "passed", ""))
    monkeypatch.setattr(runtime, "_render_resources", lambda workers: {"workers": workers})

    def run(command, *, timeout):
        commands.append(command)
        if command[0] == "hyperframes":
            (tmp_path / "renders/final.mp4").write_bytes(b"actual-output")
            return subprocess.CompletedProcess(command, 0, "rendered", "")
        return subprocess.CompletedProcess(command, 0, '{"format":{"duration":"30"}}', "")

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "_emit", lambda payload, **kwargs: emitted.append(payload))
    runtime._render("renders/final.mp4", "high", 1)
    assert {"--no-browser-gpu", "--strict-all", "--no-best-effort"} <= set(commands[0])
    assert commands[0][-2:] == ["--workers", "1"]
    assert emitted[0]["status"] == "ok"
    assert emitted[0]["files"] == ["renders/final.mp4"]

    def fail_gate(payload, **kwargs):
        assert payload["code"] == "quality_gate_failed"
        raise SystemExit(kwargs["exit_code"])

    commands.clear()
    monkeypatch.setattr(runtime, "_check", lambda: subprocess.CompletedProcess([], 1, "finding", ""))
    monkeypatch.setattr(runtime, "_resource_snapshot", lambda: {})
    monkeypatch.setattr(runtime, "_emit", fail_gate)
    with pytest.raises(SystemExit):
        runtime._render("renders/final.mp4", "high", 1)
    assert commands == []


@pytest.mark.parametrize("outcome", ["success", "error", "transport_error", "cancelled"])
def test_render_uses_fresh_sandbox_and_cleans_only_its_execution(tmp_path, monkeypatch, outcome):
    source = tmp_path / "index.html"
    source.write_text("approved source")
    uploaded = {}
    destroyed = []
    created = []
    closed = []

    class Client:
        async def create_from_builtin(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(sandbox_id="render-owned")

        async def touch(self, sandbox_id):
            pytest.fail("render must not reuse or touch the authoring sandbox")

        async def write_file_base64(self, *, sandbox_id, path, content_base64, mkdir):
            assert sandbox_id == "render-owned"
            uploaded[path] = base64.b64decode(content_base64)

        async def exec(self, *, sandbox_id, command, timeout):
            assert sandbox_id == "render-owned"
            assert "video_runtime.py render" in command
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            if outcome == "transport_error":
                raise OSError("sandbox transport failed")
            return SimpleNamespace(
                exit_code=0 if outcome == "success" else 1,
                stdout=json.dumps({
                    "status": "ok" if outcome == "success" else "error",
                    "code": "render_failed" if outcome == "error" else "",
                    "files": ["renders/final.mp4"],
                }),
                stderr="capture failed" if outcome == "error" else "",
            )

        async def read_file_base64(self, *, sandbox_id, path, max_size):
            assert outcome == "success"
            assert sandbox_id == "render-owned"
            return SimpleNamespace(content_base64=base64.b64encode(b"video").decode())

        async def destroy(self, sandbox_id):
            destroyed.append(sandbox_id)

        async def close(self):
            closed.append(True)

    from packages.core import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(SANDBOX_SERVICE_URL="http://test"))
    monkeypatch.setattr(video_edit_tools, "_video_sandbox_client", lambda **kwargs: Client())
    call = video_edit_tools._run_video_sandbox(
        project_dir=tmp_path, operation="render", sandbox_id="authoring-preserved",
        upload_sources=False,
    )
    if outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(call)
    elif outcome == "transport_error":
        with pytest.raises(video_edit_tools.VideoEditRuntimeUnavailable, match="transport failed"):
            asyncio.run(call)
    else:
        code, payload, outputs, stderr = asyncio.run(call)
        assert code == (0 if outcome == "success" else 1)
        assert outputs == ({"renders/final.mp4": b"video"} if outcome == "success" else {})
        if outcome == "error":
            assert payload["code"] == "render_failed"
            assert stderr == "capture failed"
    assert len(created) == 1
    assert uploaded == {"/skill/project/index.html": b"approved source"}
    assert destroyed == ["render-owned"]
    assert closed == [True]
    assert source.read_text() == "approved source"


def test_review_keeps_existing_authoring_session(tmp_path, monkeypatch):
    touched, destroyed = [], []

    class Client:
        async def touch(self, sandbox_id):
            touched.append(sandbox_id)

        async def exec(self, **kwargs):
            return SimpleNamespace(exit_code=1, stdout='{"status":"error","code":"quality_gate_failed"}', stderr="findings")

        async def destroy(self, sandbox_id):
            destroyed.append(sandbox_id)

        async def close(self):
            pass

    from packages.core import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(SANDBOX_SERVICE_URL="http://test"))
    monkeypatch.setattr(video_edit_tools, "_video_sandbox_client", lambda **kwargs: Client())
    code, payload, _, _ = asyncio.run(video_edit_tools._run_video_sandbox(
        project_dir=tmp_path, operation="review", sandbox_id="authoring-preserved", upload_sources=False,
    ))
    assert code == 1
    assert payload["code"] == "quality_gate_failed"
    assert touched == ["authoring-preserved"]
    assert not destroyed
