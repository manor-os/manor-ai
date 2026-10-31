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
    monkeypatch.setattr(runtime, "_check", lambda **_: subprocess.CompletedProcess([], 0, "passed", ""))
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
    monkeypatch.setattr(runtime, "_check", lambda **_: subprocess.CompletedProcess([], 1, "finding", ""))
    monkeypatch.setattr(runtime, "_resource_snapshot", lambda: {})
    monkeypatch.setattr(runtime, "_emit", fail_gate)
    with pytest.raises(SystemExit):
        runtime._render("renders/final.mp4", "high", 1)
    assert commands == []

    # A reviewed source skips the duplicate strict check; render itself stays strict.
    def no_check():
        raise AssertionError("the reviewed hash already passed this check")

    monkeypatch.setattr(runtime, "_check", no_check)
    monkeypatch.setattr(runtime, "_emit", lambda payload, **kwargs: emitted.append(payload))
    emitted.clear()
    runtime._render("renders/final.mp4", "high", 1, reviewed=True)
    assert {"--strict-all", "--no-best-effort"} <= set(commands[0])
    assert emitted[0]["status"] == "ok"


@pytest.fixture
def local_runner(monkeypatch):
    from packages.core.config import get_settings
    from packages.core.services import sandbox_sdk
    from sandbox_entry_fakes import FakeRunner

    runner = FakeRunner()
    settings = get_settings()
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", runner.client_class)
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", "http://test")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "local")
    return runner


def _context():
    return SimpleNamespace(
        entity_id="entity-1", user_id="user-1", agent_id="agent-1", conversation_id="chat-1",
        sandbox_binding_key="chat-1", runtime_run_id=None, runtime_tool_call_id=None,
        task_id=None, parent_skill=None,
    )


@pytest.mark.parametrize("outcome", ["success", "error", "transport_error", "cancelled"])
def test_render_uses_fresh_sandbox_and_cleans_only_its_execution(tmp_path, local_runner, outcome):
    source = tmp_path / "index.html"
    source.write_text("approved source")

    def execute(sandbox_id, command, container):
        assert sandbox_id != "authoring-preserved"
        assert "video_runtime.py render" in command
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        if outcome == "transport_error":
            raise OSError("sandbox transport failed")
        if outcome == "success":
            container["files"]["/skill/project/renders/final.mp4"] = b"video"
        return SimpleNamespace(
            exit_code=0 if outcome == "success" else 1,
            stdout=json.dumps({
                "status": "ok" if outcome == "success" else "error",
                "code": "render_failed" if outcome == "error" else "",
                "files": ["renders/final.mp4"],
            }),
            stderr="capture failed" if outcome == "error" else "",
        )

    local_runner.exec_handler = execute
    uploaded: dict[str, bytes] = {}
    original = local_runner.client_class.write_file_base64

    async def record_upload(self, sandbox_id, path, content_base64, mkdir=True):
        uploaded[path] = base64.b64decode(content_base64)
        return await original(self, sandbox_id, path, content_base64, mkdir)

    local_runner.client_class.write_file_base64 = record_upload
    call = video_edit_tools._run_video_sandbox(
        project_dir=tmp_path, operation="render", sandbox_id="authoring-preserved",
        upload_sources=False, context=_context(),
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
    [created] = local_runner.created
    assert uploaded == {"/skill/project/index.html": b"approved source"}
    # The render's own container, and only it, is destroyed whatever happened.
    assert local_runner.destroyed == [created["sandbox_id"]]
    assert local_runner.live() == set()
    assert not [call for name, call in local_runner.calls if name == "touch"]
    assert source.read_text() == "approved source"


def test_review_keeps_existing_authoring_session(tmp_path, local_runner):
    import asyncio as _asyncio

    session_client = local_runner.client_class()
    created = _asyncio.run(session_client.create_from_builtin(skill_name="video-edit-runtime", files={}))
    local_runner.calls.clear()
    local_runner.created.clear()
    local_runner.exec_handler = lambda sandbox_id, command, container: SimpleNamespace(
        exit_code=1, stdout='{"status":"error","code":"quality_gate_failed"}', stderr="findings",
    )
    code, payload, _, _ = asyncio.run(video_edit_tools._run_video_sandbox(
        project_dir=tmp_path, operation="review", sandbox_id=created.sandbox_id, upload_sources=False,
        context=_context(),
    ))
    assert code == 1
    assert payload["code"] == "quality_gate_failed"
    assert [call["sandbox_id"] for name, call in local_runner.calls if name == "touch"] == [created.sandbox_id]
    assert local_runner.created == []
    assert local_runner.destroyed == []


def _restart_retry_fakes(monkeypatch):
    now = [0.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(video_edit_tools, "_restart_retry_clock", lambda: now[0])
    monkeypatch.setattr(video_edit_tools, "_restart_retry_sleep", sleep)
    return sleeps


def test_a_restart_symptom_is_waited_out_in_process(monkeypatch):
    from packages.core.services.sandbox_sdk.exceptions import SandboxConnectionError

    sleeps = _restart_retry_fakes(monkeypatch)
    attempts = []

    async def attempt():
        attempts.append(1)
        if len(attempts) < 3:
            raise SandboxConnectionError("Cannot connect to Sandbox Service")
        return "done"

    assert asyncio.run(video_edit_tools._retry_sandbox_restart("Video render", attempt)) == "done"
    assert len(attempts) == 3 and sleeps == [2.0, 4.0]


def test_a_durable_call_reports_a_restart_as_transient_without_retrying(monkeypatch):
    """Re-admitting in the same durable call re-attaches the lost container,
    so the call fails transient at once and the agent's next call gets a fresh one."""

    from packages.core.services.sandbox_sdk.exceptions import SandboxConnectionError

    sleeps = _restart_retry_fakes(monkeypatch)
    attempts = []

    async def attempt():
        attempts.append(1)
        raise SandboxConnectionError("Cannot connect to Sandbox Service")

    with pytest.raises(video_edit_tools.VideoEditRuntimeUnavailable) as raised:
        asyncio.run(video_edit_tools._retry_sandbox_restart("Video render", attempt, retry_in_process=False))
    assert len(attempts) == 1 and sleeps == []
    payload = json.loads(video_edit_tools._runtime_unavailable_error(raised.value))
    assert payload["error"] == "runtime_unavailable"
    assert payload["details"]["transient"] is True and payload["details"]["retryable"] is True


def test_only_a_durable_ticket_admission_skips_the_in_process_retry(monkeypatch):
    from packages.core.config import get_settings

    settings = get_settings()
    call = SimpleNamespace(runtime_run_id="01RUN", runtime_tool_call_id="call-1", parent_skill=None)
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "durable")
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "external-runner")
    assert video_edit_tools._admission_retries_in_process(call) is False
    # A nested Skill loop and a call outside a durable run admit inline.
    assert video_edit_tools._admission_retries_in_process(SimpleNamespace(**{**vars(call), "parent_skill": "p"}))
    assert video_edit_tools._admission_retries_in_process(SimpleNamespace(runtime_run_id=None))
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "local")
    assert video_edit_tools._admission_retries_in_process(call) is True


def test_a_capacity_refusal_keeps_its_code_and_is_not_transient():
    error = video_edit_tools.VideoEditRuntimeUnavailable("busy", code="capacity_full", retryable=True)
    payload = json.loads(video_edit_tools._runtime_unavailable_error(error))
    assert payload["error"] == "capacity_full"
    assert payload["details"] == {"retryable": True, "transient": False}
