import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SERVICE = ROOT / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE))

# See test_sandbox_write_base64: a sys.path insert cannot dislodge a name
# already bound in sys.modules, and ``config`` is claimed by another test.
sys.modules.pop("config", None)

docker_backend = pytest.importorskip("sandbox.docker_backend")
models = pytest.importorskip("sandbox.models")
skill_runner = pytest.importorskip("sandbox.skill_runner")


def _ready_sandbox(skill_name: str):
    sandbox = docker_backend.DockerSandbox(
        "sandbox-test",
        models.ContainerConfig(),
    )
    sandbox.status = models.SandboxStatus.READY
    sandbox.last_used_at = time.time() - 60
    sandbox.skill = models.SkillManifest(
        name=skill_name,
        skill_dir="/old-skill",
    )
    return sandbox


@pytest.mark.asyncio
async def test_same_skill_reload_preserves_existing_workdir(monkeypatch):
    sandbox = _ready_sandbox("pptx")
    inject = AsyncMock()
    monkeypatch.setattr(sandbox, "_inject_files", inject)

    await sandbox.load_skill(
        skill_name="pptx",
        files={"SKILL.md": "# PPTX", "run.py": "print('ok')"},
        auto_install=False,
        idle_threshold=0,
    )

    inject.assert_awaited_once()
    assert inject.await_args.kwargs == {"clear_existing": False}


@pytest.mark.asyncio
async def test_different_skill_reload_clears_existing_workdir(monkeypatch):
    sandbox = _ready_sandbox("pptx")
    inject = AsyncMock()
    monkeypatch.setattr(sandbox, "_inject_files", inject)

    await sandbox.load_skill(
        skill_name="pdf",
        files={"SKILL.md": "# PDF", "run.py": "print('ok')"},
        auto_install=False,
        idle_threshold=0,
    )

    inject.assert_awaited_once()
    assert inject.await_args.kwargs == {"clear_existing": True}


@pytest.mark.asyncio
async def test_create_from_files_keeps_requested_skill_identity(monkeypatch):
    runner = skill_runner.SkillRunner()
    setup = AsyncMock()
    monkeypatch.setattr(docker_backend.DockerSandbox, "setup", setup)

    result = await runner.create_sandbox_from_files(
        skill_name="pptx",
        files={"SKILL.md": "# PPTX", "run.py": "print('ok')"},
        env={},
        auto_install=False,
    )

    assert result.skill.name == "pptx"
    assert setup.await_args.args[0].name == "pptx"


@pytest.mark.asyncio
async def test_idle_pruner_reclaims_ready_sandbox_but_never_active_command(monkeypatch):
    runner = skill_runner.SkillRunner()
    ready = _ready_sandbox("video-edit-runtime")
    ready.last_used_at = time.time() - 601
    ready_destroy = AsyncMock()
    monkeypatch.setattr(ready, "destroy", ready_destroy)

    executing = _ready_sandbox("video-edit-runtime")
    executing.sandbox_id = "sandbox-executing"
    executing.status = models.SandboxStatus.EXECUTING
    executing.last_used_at = time.time() - 601
    executing_destroy = AsyncMock()
    monkeypatch.setattr(executing, "destroy", executing_destroy)

    runner._sandboxes = {"ready": ready, "executing": executing}
    monkeypatch.setattr(skill_runner.app_config, "IDLE_TIMEOUT_SECONDS", 600)

    await runner._prune_idle()

    assert "ready" not in runner._sandboxes
    assert "executing" in runner._sandboxes
    ready_destroy.assert_awaited_once()
    executing_destroy.assert_not_awaited()


def test_self_hosted_sandbox_defaults_fit_five_slot_capacity():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    config_source = (SANDBOX_SERVICE / "config.py").read_text(encoding="utf-8")

    assert "${SANDBOX_MAX_SANDBOXES:-5}" in compose
    assert "${SANDBOX_IDLE_TIMEOUT:-600}" in compose
    assert 'os.getenv("SANDBOX_MAX_SANDBOXES", "5")' in config_source
    assert 'os.getenv("SANDBOX_IDLE_TIMEOUT", "600")' in config_source
