"""Skill env reaches processes per ``docker exec`` and is replaced by load_skill.

Baking env into ``docker create`` made a warm container carry its first
skill's credentials forever, so a credentialed skill could never share a
container. With exec-time injection the env travels in a 0600 env file per
exec and ``load_skill`` swaps it, so the next skill never sees the previous
secrets.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SANDBOX_SERVICE = ROOT / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE))
sys.modules.pop("config", None)

docker_backend = pytest.importorskip("sandbox.docker_backend")
models = pytest.importorskip("sandbox.models")


def _ready_sandbox(skill_name: str = "xlsx"):
    sandbox = docker_backend.DockerSandbox("sandbox-env", models.ContainerConfig())
    sandbox.container_name = "sandbox-env-container"
    sandbox.status = models.SandboxStatus.READY
    sandbox.last_used_at = time.time() - 60
    sandbox.skill = models.SkillManifest(name=skill_name, skill_dir="/old-skill")
    return sandbox


class _DockerRecorder:
    """Records docker invocations and snapshots any env file while it exists."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.env_files: list[dict[str, str]] = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        if "--env-file" in args:
            path = args[args.index("--env-file") + 1]
            assert oct(os.stat(path).st_mode & 0o777) == "0o600"
            content = Path(path).read_text(encoding="utf-8")
            self.env_files.append(dict(line.split("=", 1) for line in content.splitlines() if line))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")


@pytest.mark.asyncio
async def test_exec_receives_the_skill_env_through_a_temp_env_file(monkeypatch):
    recorder = _DockerRecorder()
    monkeypatch.setattr(docker_backend, "_run_docker", recorder)
    sandbox = _ready_sandbox()
    sandbox._skill_env = {"API_KEY": "secret", "MULTI": "line one\nline two"}

    await sandbox.exec("echo hi", timeout=5)

    with_env = [call for call in recorder.calls if "--env-file" in call]
    assert len(with_env) == 1, recorder.calls
    exec_args = with_env[0]
    assert exec_args[:3] == ["docker", "exec", "-i"]
    assert "--env" not in exec_args and "secret" not in " ".join(exec_args), "no value ever reaches argv"
    # A value with a newline cannot live in an env file verbatim: it is
    # base64-encoded there and listed for the launcher to decode.
    import base64

    assert recorder.env_files == [
        {
            "API_KEY": "secret",
            "MULTI": base64.b64encode(b"line one\nline two").decode(),
            "MANOR_SANDBOX_B64_ENV": "MULTI",
        }
    ]
    env_file = exec_args[exec_args.index("--env-file") + 1]
    assert not os.path.exists(env_file), "the env file is removed once the exec has run"


@pytest.mark.asyncio
async def test_exec_without_skill_env_passes_no_env_file(monkeypatch):
    recorder = _DockerRecorder()
    monkeypatch.setattr(docker_backend, "_run_docker", recorder)
    sandbox = _ready_sandbox()

    await sandbox.exec("echo hi", timeout=5)

    assert not any("--env-file" in call for call in recorder.calls)


def test_container_create_never_bakes_the_skill_env():
    sandbox = _ready_sandbox()
    args = sandbox._build_create_args({})
    assert "--env" not in args


@pytest.mark.asyncio
async def test_setup_keeps_env_for_exec_instead_of_create(monkeypatch):
    sandbox = docker_backend.DockerSandbox("sandbox-env", models.ContainerConfig())
    ensure = AsyncMock()
    monkeypatch.setattr(sandbox, "_ensure_container", ensure)
    monkeypatch.setattr(sandbox, "_inject_files", AsyncMock())
    skill = models.SkillManifest(name="xlsx", skill_dir="/skill-src")

    await sandbox.setup(skill, {"API_KEY": "secret"}, auto_install=False)

    ensure.assert_awaited_once()
    assert ensure.await_args.args[1] == {}, "create must not receive the skill env"
    assert sandbox._skill_env == {"API_KEY": "secret"}


@pytest.mark.asyncio
async def test_load_skill_replaces_the_previous_skill_env(monkeypatch):
    sandbox = _ready_sandbox("xlsx")
    sandbox._skill_env = {"XLSX_KEY": "secret"}
    monkeypatch.setattr(sandbox, "_inject_files", AsyncMock())

    await sandbox.load_skill(
        skill_name="docx",
        files={"SKILL.md": "# DOCX"},
        auto_install=False,
        idle_threshold=0,
        env={"DOCX_KEY": "other"},
    )
    assert sandbox._skill_env == {"DOCX_KEY": "other"}

    await sandbox.load_skill(
        skill_name="pptx",
        files={"SKILL.md": "# PPTX"},
        auto_install=False,
        idle_threshold=0,
    )
    assert sandbox._skill_env == {}, "a skill without env clears the previous secrets"


@pytest.mark.asyncio
async def test_multi_line_values_are_decoded_by_the_launcher_not_passed_on_argv(monkeypatch):
    """A PEM key must arrive intact without ever being on the docker command line."""
    import base64

    recorder = _DockerRecorder()
    monkeypatch.setattr(docker_backend, "_run_docker", recorder)
    sandbox = _ready_sandbox()
    pem = "-----BEGIN " + "PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----"
    sandbox._skill_env = {"API_KEY": "secret", "PEM": pem}

    await sandbox.exec("echo hi", timeout=5)

    exec_args = next(call for call in recorder.calls if "--env-file" in call)
    assert "--env" not in exec_args
    assert recorder.env_files == [
        {"API_KEY": "secret", "PEM": base64.b64encode(pem.encode()).decode(), "MANOR_SANDBOX_B64_ENV": "PEM"}
    ]
    launcher = exec_args[exec_args.index("-c") + 1]
    assert "MANOR_SANDBOX_B64_ENV" in launcher and "b64decode" in launcher


def test_docker_timeout_errors_never_carry_env_values():
    args = ["docker", "exec", "--env", "PEM=-----BEGIN\nSECRET", "-e", "TOKEN=abc", "c", "sh"]
    redacted = docker_backend._redact_docker_args(args)
    assert redacted == ["docker", "exec", "--env", "PEM=<redacted>", "-e", "TOKEN=<redacted>", "c", "sh"]
    assert "SECRET" not in " ".join(redacted) and "abc" not in " ".join(redacted)


def test_invalid_env_keys_are_reported_not_written():
    security = pytest.importorskip("sandbox.security")
    # Only what docker's env-file parser cannot take is refused: whitespace or
    # "=" in the key (the whole exec would fail) and a leading "#" (dropped).
    safe, blocked = security.sanitize_env_vars(
        {"API KEY": "v", "#COMMENT": "v", "A=B": "v", "MY-KEY": "v", "9LIVES": "v", "GOOD_KEY": "v"},
        {"API KEY", "#COMMENT", "A=B", "MY-KEY", "9LIVES", "GOOD_KEY"},
    )
    assert safe == {"MY-KEY": "v", "9LIVES": "v", "GOOD_KEY": "v"}
    assert set(blocked) == {"API KEY", "#COMMENT", "A=B"}


@pytest.mark.asyncio
async def test_env_file_is_removed_when_the_command_cannot_be_built(monkeypatch):
    sandbox = _ready_sandbox()
    sandbox._skill_env = {"API_KEY": "secret"}
    written: list[str] = []
    original = sandbox._prepare_skill_env

    def prepare(**kwargs):
        env_file, argv = original(**kwargs)
        written.append(env_file)
        return env_file, argv

    monkeypatch.setattr(sandbox, "_prepare_skill_env", prepare)

    def broken(*_args, **_kwargs):
        raise RuntimeError("cannot build args")

    monkeypatch.setattr(sandbox, "_execution_command_args", broken)

    with pytest.raises(RuntimeError, match="cannot build args"):
        await sandbox.exec("echo hi", timeout=5)

    assert written and not os.path.exists(written[0])


def test_startup_sweep_removes_only_stale_env_files(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "sbx-env-old.env"
    fresh = tmp_path / "sbx-env-new.env"
    other = tmp_path / "unrelated.env"
    for path in (stale, fresh, other):
        path.write_text("K=v\n")
    old = time.time() - 3 * 24 * 3600
    os.utime(stale, (old, old))

    removed = docker_backend.sweep_stale_skill_env_files()

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists() and other.exists()


def test_startup_sweep_with_zero_age_removes_every_env_file(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    fresh = tmp_path / "sbx-env-fresh.env"
    fresh.write_text("K=v\n")
    assert docker_backend.sweep_stale_skill_env_files(max_age_seconds=0) == 1
    assert not fresh.exists()


def test_reserved_marker_name_is_never_taken_from_the_skill():
    security = pytest.importorskip("sandbox.security")
    safe, blocked = security.sanitize_env_vars(
        {"MANOR_SANDBOX_B64_ENV": "PEM", "OK": "1"}, {"MANOR_SANDBOX_B64_ENV", "OK"}
    )
    assert safe == {"OK": "1"} and blocked == ["MANOR_SANDBOX_B64_ENV"]
    sandbox = _ready_sandbox()
    sandbox._skill_env = {"MANOR_SANDBOX_B64_ENV": "PEM", "OK": "1"}
    env_file, _ = sandbox._prepare_skill_env()
    try:
        assert Path(env_file).read_text() == "OK=1\n"
    finally:
        os.unlink(env_file)


def test_installer_path_omits_multi_line_values_instead_of_encoding_them():
    sandbox = _ready_sandbox()
    sandbox._skill_env = {"PEM": "a\nb", "TOKEN": "t"}
    env_file, _ = sandbox._prepare_skill_env(decode_multiline=False)
    try:
        assert Path(env_file).read_text() == "TOKEN=t\n"
    finally:
        os.unlink(env_file)
    sandbox._skill_env = {"PEM": "a\nb"}
    assert sandbox._prepare_skill_env(decode_multiline=False) == (None, [])
