"""Every sandbox gets a disk-backed install dir that cannot leak or grow unbounded.

Runtime ``pip install`` / ``npm install -g`` land in an anonymous Docker volume
mounted at ``SANDBOX_LOCAL_DIR`` instead of the memory-backed tmpfs counted
against the container's memory limit. The contracts pinned here:

1. The runner mounts it on every container it creates, as a runner default
   that no caller override (``cfg.volumes``) can replace, keeping the
   read-only root and dropped capabilities.
2. Every container removal deletes the volume with the container
   (``docker rm -f -v``): destroy, reap, lost reclaim, failed create,
   stale-container replacement and the startup reconcile.
3. A volume left behind by a container removed some other way is swept
   (dangling + this deployment's label) at startup and on every reaper tick.
4. The volume driver cannot cap its size, so the reaper measures it and
   reclaims a sandbox over ``SANDBOX_LOCAL_DISK_LIMIT_MB``.
5. The image points the user-level install locations into that dir and owns
   it as the sandbox user (Docker copies it into each new volume).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.sandbox_runner_fakes import FakeDocker

import sandbox.docker_backend as docker_backend  # noqa: E402
import sandbox.skill_runner as skill_runner_module  # noqa: E402
from sandbox.docker_backend import SANDBOX_LOCAL_DIR, DockerSandbox  # noqa: E402
from sandbox.models import ContainerConfig, SandboxStatus, SkillManifest  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCKER_BACKEND = ROOT / "sandbox-service" / "sandbox" / "docker_backend.py"
DOCKERFILE = ROOT / "docker" / "Dockerfile.sandbox"


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_backend, "_run_docker", fake)
    return fake


def _mount_args(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]


# ── 1. mounted on every container, as a runner default ───────────────────


def test_every_container_gets_a_labelled_disk_volume_at_the_local_dir() -> None:
    sandbox = DockerSandbox("sbx-1", ContainerConfig(service_scope="deploy-a"))
    sandbox.container_name = "skill-sbx-demo"

    args = sandbox._build_create_args({})

    assert _mount_args(args) == [
        f"type=volume,dst={SANDBOX_LOCAL_DIR},"
        "volume-label=sandbox.service=deploy-a,volume-label=sandbox.id=sbx-1"
    ]
    # Still hardened: read-only root, every capability dropped.
    assert "--read-only" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    # Disk-backed, not another memory-backed tmpfs.
    assert not any(
        arg.startswith(SANDBOX_LOCAL_DIR) for i, arg in enumerate(args) if args[i - 1] == "--tmpfs"
    )


def test_caller_volumes_cannot_replace_the_local_dir_mount() -> None:
    cfg = ContainerConfig(volumes=["/srv/data:/workspace:ro"])
    args = DockerSandbox("sbx-2", cfg)._build_create_args({})

    assert ["--volume", "/srv/data:/workspace:ro"] == args[
        args.index("--volume"):args.index("--volume") + 2
    ]
    assert any(f"dst={SANDBOX_LOCAL_DIR}," in mount for mount in _mount_args(args))


# ── 2. removed with the container on every path ──────────────────────────


def _docker_rm_calls(tree: ast.AST) -> list[ast.List]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List) and len(node.elts) >= 2:
            head = [elt.value for elt in node.elts[:2] if isinstance(elt, ast.Constant)]
            if head == ["docker", "rm"]:
                calls.append(node)
    return calls


def test_every_container_removal_in_the_backend_also_removes_its_volumes() -> None:
    """Guard the class: any ``docker rm`` the runner issues must carry ``-v``."""
    tree = ast.parse(DOCKER_BACKEND.read_text())
    removals = _docker_rm_calls(tree)

    assert len(removals) >= 3  # destroy, stale replacement, reconcile
    for node in removals:
        flags = [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
        assert "-v" in flags, f"docker rm without -v at line {node.lineno} leaks the local-disk volume"
    # No other module of the runner removes containers behind the backend's back.
    for module in (ROOT / "sandbox-service").rglob("*.py"):
        if module == DOCKER_BACKEND:
            continue
        assert not _docker_rm_calls(ast.parse(module.read_text())), module


@pytest.mark.asyncio
async def test_destroy_removes_the_container_with_its_volume(docker) -> None:
    sandbox = DockerSandbox("sbx-3", ContainerConfig())
    sandbox.container_name = "skill-sbx-demo"
    sandbox.status = SandboxStatus.READY

    await sandbox.destroy()

    assert ["docker", "rm", "-f", "-v", "skill-sbx-demo"] in docker.calls


@pytest.mark.asyncio
async def test_failed_create_removes_the_half_built_container_with_its_volume(docker, monkeypatch):
    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda *_a, **_k: True)
    monkeypatch.setattr(docker_backend, "_image_exists_locally", lambda *_a, **_k: True)
    docker.exists = False
    docker.inject_fails = True

    real = docker.__call__

    def create_then_fail(args, **kwargs):
        if args[1] == "create":
            docker.calls.append(list(args))
            docker.exists = True
            return docker_backend.subprocess.CompletedProcess(args, 0, b"", b"")
        if args[1] == "start":
            docker.calls.append(list(args))
            docker.running = True
            return docker_backend.subprocess.CompletedProcess(args, 0, b"", b"")
        return real(args, **kwargs)

    monkeypatch.setattr(docker_backend, "_run_docker", create_then_fail)
    runner = SkillRunner()
    skill = SkillManifest(name="demo", skill_dir=str(ROOT), scripts=[])

    with pytest.raises(Exception):
        await runner._create_sandbox_once(skill, {}, None, None, False, sandbox_id="sbx-4")

    create = next(call for call in docker.calls if call[1] == "create")
    assert any(f"dst={SANDBOX_LOCAL_DIR}," in mount for mount in _mount_args(create))
    assert ["docker", "rm", "-f", "-v", create[create.index("--name") + 1]] in docker.calls
    assert runner._sandboxes == {}


def test_reconcile_removes_orphans_with_their_volumes(monkeypatch) -> None:
    calls = []

    def fake_run_docker(args, **_kwargs):
        calls.append(args)
        return docker_backend.subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(docker_backend, "_run_docker", fake_run_docker)
    docker_backend.remove_managed_sandbox_container("abc123")

    assert calls == [["docker", "rm", "-f", "-v", "abc123"]]


# ── 3. dangling volumes are swept ────────────────────────────────────────


def test_dangling_volume_sweep_only_targets_this_deployment(monkeypatch) -> None:
    calls = []

    def fake_run_docker(args, **_kwargs):
        calls.append(args)
        out = b"vol-a\nvol-b\n" if args[2] == "ls" else b""
        code = 1 if args[-1] == "vol-b" else 0  # claimed again in between: kept
        return docker_backend.subprocess.CompletedProcess(args, code, out, b"in use")

    monkeypatch.setattr(docker_backend, "_run_docker", fake_run_docker)

    assert docker_backend.remove_dangling_sandbox_volumes("deploy-a") == 1
    assert calls[0] == [
        "docker", "volume", "ls", "-q",
        "--filter", "dangling=true", "--filter", "label=sandbox.service=deploy-a",
    ]
    assert calls[1:] == [["docker", "volume", "rm", "vol-a"], ["docker", "volume", "rm", "vol-b"]]


@pytest.mark.asyncio
async def test_startup_and_every_reaper_tick_sweep_dangling_volumes(docker, monkeypatch) -> None:
    runner = SkillRunner()
    docker.dangling_volumes = ["vol-left-by-a-crash"]
    await runner._reconcile_managed_containers()
    assert docker.dangling_volumes == []

    source = Path(skill_runner_module.__file__).read_text()
    loop = source[source.index("async def _prune_loop"):source.index("async def _renew_active_leases")]
    assert "await self._sweep_dangling_volumes()" in loop
    # The install dir's size bound is the disk watchdog's (one policy for every
    # disk), never a whole-sandbox reclaim from the reaper.
    assert "_enforce_local_disk_quota" not in source and "mark_disk_quota_exceeded" not in source


# ── 4. size bound: one disk policy (sandbox.disk_quota) ──────────────────


def test_the_install_dir_is_a_disk_the_watchdog_bounds(monkeypatch) -> None:
    """Shared by every work area of the user's sandbox: its writers are
    charged, and the container is recycled only once idle and still over."""

    from sandbox.disk_quota import DiskRoot

    monkeypatch.setattr(skill_runner_module.app_config, "SANDBOX_LOCAL_DISK_LIMIT_MB", 100)
    sandbox = DockerSandbox("sbx-roots", ContainerConfig())
    [install] = [root for root in sandbox.disk_roots() if root.path == SANDBOX_LOCAL_DIR]
    assert install == DiskRoot(
        path=SANDBOX_LOCAL_DIR, limit_bytes=100 * 1024 * 1024, areas_dir=None, filesystem=False,
        recycle_when_idle=True, interval_seconds=60,
    )


def test_disk_limit_zero_leaves_the_install_dir_unbounded(monkeypatch) -> None:
    monkeypatch.setattr(skill_runner_module.app_config, "SANDBOX_LOCAL_DISK_LIMIT_MB", 0)
    sandbox = DockerSandbox("sbx-roots", ContainerConfig())
    assert SANDBOX_LOCAL_DIR not in [root.path for root in sandbox.disk_roots()]


@pytest.mark.asyncio
async def test_a_writer_over_the_limit_is_stopped_and_the_sandbox_kept_while_work_runs(monkeypatch) -> None:
    from sandbox.disk_quota import DiskUsage, enforce_disk_quota, execution_digest
    from sandbox.models import SandboxStatus

    monkeypatch.setattr(skill_runner_module.app_config, "SANDBOX_LOCAL_DISK_LIMIT_MB", 100)
    sandbox = DockerSandbox("sbx-big", ContainerConfig(max_concurrent_executions=4, memory="1g", pids_limit=512))
    sandbox.status = SandboxStatus.READY
    sandbox.container_name = "c-big"
    stopped: list[str] = []

    async def stop_for_disk(kills):
        stopped.extend(kill.execution_id for kill in kills)

    sandbox.stop_for_disk = stop_for_disk
    sandbox.claim_command("npm", "npm install -g huge", 600, fair_key="conv-A")
    sandbox.claim_command("other", "python report.py", 60, fair_key="conv-B")
    [install] = [root for root in sandbox.disk_roots() if root.path == SANDBOX_LOCAL_DIR]

    plan = await enforce_disk_quota(
        sandbox, [DiskUsage(install, used_bytes=101 * 1024 * 1024, writers=frozenset({execution_digest("npm")}))],
    )

    assert stopped == ["npm"], "only the writer is stopped"
    assert set(sandbox.disk_refusals) == {"conv-A"} and plan.recycle_reason is None
    assert not sandbox.is_lost, "never reclaimed while other work runs in it"


# ── 5. the image side of the contract ────────────────────────────────────


def test_image_points_user_installs_into_the_local_dir_it_owns() -> None:
    source = DOCKERFILE.read_text()

    assert f"install -d -o sandbox -g sandbox -m 0755 {SANDBOX_LOCAL_DIR}" in source
    env = source[source.index(f"ENV SANDBOX_LOCAL_DIR={SANDBOX_LOCAL_DIR}"):]
    env = env[:env.index("\n\n") if "\n\n" in env else len(env)]
    for variable in ("PYTHONUSERBASE", "NPM_CONFIG_PREFIX", "NPM_CONFIG_CACHE"):
        match = re.search(rf"{variable}=(\S+)", env)
        assert match and match.group(1).startswith(f"{SANDBOX_LOCAL_DIR}/"), variable
    assert f"{SANDBOX_LOCAL_DIR}/python/bin" in env and f"{SANDBOX_LOCAL_DIR}/npm-global/bin" in env
    # At runtime nothing points installs at the memory-backed home tmpfs
    # (the build-time npm values earlier in the file are overridden here).
    assert "/home/sandbox" not in env
    # Created after every build-time install, so no root-owned leftovers are
    # copied into the volume; and before the image drops to the sandbox user.
    assert source.index(f"-m 0755 {SANDBOX_LOCAL_DIR}") > source.index("COPY --from=skill-manifests")
    assert source.index(f"-m 0755 {SANDBOX_LOCAL_DIR}") < source.index("USER sandbox")
