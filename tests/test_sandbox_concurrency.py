from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

SANDBOX_SERVICE_ROOT = Path(__file__).resolve().parents[1] / "sandbox-service"
sys.path.insert(0, str(SANDBOX_SERVICE_ROOT))

from sandbox.concurrency import (  # noqa: E402
    SandboxConcurrencyExceeded,
    SandboxConcurrencyGate,
    SandboxGateConfig,
)
from api import routes as sandbox_routes  # noqa: E402
from sandbox.models import (  # noqa: E402
    ContainerConfig,
    CreateSandboxResponse,
    SandboxStatus,
)
from sandbox.skill_runner import SkillRunner  # noqa: E402
from sandbox.docker_backend import DockerSandbox  # noqa: E402
from sandbox.security import SecurityError  # noqa: E402


def test_sandbox_runner_rejects_unapproved_network_override(monkeypatch):
    import sandbox.skill_runner as skill_runner_module

    monkeypatch.setattr(skill_runner_module.app_config, "SANDBOX_NETWORK", "manor-sandbox")
    monkeypatch.setattr(
        skill_runner_module.app_config,
        "SANDBOX_ALLOWED_NETWORKS",
        ["manor-sandbox"],
        raising=False,
    )
    runner = SkillRunner()

    assert runner._resolve_config().network == "manor-sandbox"
    assert runner._resolve_config(ContainerConfig(network="none")).network == "none"
    for network in ("host", "bridge", "unapproved-network"):
        with pytest.raises(SecurityError, match=f"Docker network '{network}' is not allowed"):
            runner._resolve_config(ContainerConfig(network=network))


def test_networkless_override_is_always_allowed_on_a_dedicated_bridge_runner(monkeypatch):
    """External runners allow only their dedicated bridge (install.sh enforces
    that), yet the bash tool, workflow code nodes, and video_edit ask for
    ``network="none"``. Staging rejected every such call ("Docker network 'none'
    is not allowed; expected one of ['manor-sandbox']"). No network is strictly
    more isolated than any allowed bridge, so it never needs allowlisting."""

    import sandbox.skill_runner as skill_runner_module

    monkeypatch.setattr(skill_runner_module.app_config, "SANDBOX_NETWORK", "manor-sandbox")
    monkeypatch.setattr(
        skill_runner_module.app_config,
        "SANDBOX_ALLOWED_NETWORKS",
        ["manor-sandbox"],
        raising=False,
    )
    runner = SkillRunner()

    assert runner._resolve_config(ContainerConfig(network="none")).network == "none"
    for widened in ("bridge", "host"):
        with pytest.raises(SecurityError, match=f"Docker network '{widened}' is not allowed"):
            runner._resolve_config(ContainerConfig(network=widened))


class FakeRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}

    async def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        zset = self.zsets.setdefault(key, {})
        stale = [member for member, score in zset.items() if min_score <= score <= max_score]
        for member in stale:
            del zset[member]
        return len(stale)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.setdefault(key, {}))

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = seconds
        return True

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zsets.setdefault(key, {}).pop(member, None) is not None else 0


class FakeLuaRedis(FakeRedis):
    async def eval(self, script: str, number_of_keys: int, key: str, *args):
        assert number_of_keys == 1
        if "ZADD" in script and "XX" in script:
            token, now, ttl = args
            zset = self.zsets.setdefault(key, {})
            if token not in zset:
                return 0
            zset[token] = float(now)
            self.expires[key] = int(ttl)
            return 1

        token, now, ttl, limit = args
        zset = self.zsets.setdefault(key, {})
        stale = [member for member, score in zset.items() if score <= float(now) - int(ttl)]
        for member in stale:
            del zset[member]
        if len(zset) >= int(limit):
            self.expires[key] = int(ttl)
            return 0
        zset[str(token)] = float(now)
        self.expires[key] = int(ttl)
        return 1


@pytest.mark.asyncio
async def test_sandbox_global_active_gate_rejects_and_releases():
    redis = FakeRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(global_max_active=1, wait_timeout_seconds=0),
        redis_getter=lambda: redis,
        instance_id="test",
    )

    lease = await gate.acquire_active()
    with pytest.raises(SandboxConcurrencyExceeded):
        await gate.acquire_active()

    await lease.release()
    second = await gate.acquire_active()
    await second.release()


@pytest.mark.asyncio
async def test_sandbox_execute_gate_combines_instance_and_global_limits():
    redis = FakeRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=1,
            instance_max_executing=1,
            wait_timeout_seconds=0,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )

    lease = await gate.acquire_execute()
    with pytest.raises(SandboxConcurrencyExceeded):
        await gate.acquire_execute()

    await lease.release()
    second = await gate.acquire_execute()
    await second.release()


@pytest.mark.asyncio
async def test_zero_instance_execute_limit_disables_local_gate():
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=0,
            instance_max_executing=0,
            wait_timeout_seconds=0,
        )
    )

    leases = [await gate.acquire_execute() for _ in range(3)]
    assert gate.executing == 3

    for lease in leases:
        await lease.release()
    assert gate.executing == 0


@pytest.mark.asyncio
async def test_cancelled_execute_global_admission_restores_local_slot():
    class BlockingRejectRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.admission_started = asyncio.Event()
            self.allow_result = asyncio.Event()

        async def eval(self, script: str, number_of_keys: int, key: str, *args):
            self.admission_started.set()
            await self.allow_result.wait()
            return 0

    redis = BlockingRejectRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=1,
            instance_max_executing=1,
            wait_timeout_seconds=0,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    admission = asyncio.create_task(gate.acquire_execute())
    await redis.admission_started.wait()

    admission.cancel()
    await asyncio.sleep(0)
    redis.allow_result.set()

    with pytest.raises(asyncio.CancelledError):
        await admission

    assert gate._execute_semaphore._value == 1
    assert await redis.zcard("manor:sandbox:executing") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admission_method", "redis_key"),
    [
        ("acquire_active", "manor:sandbox:active"),
        ("acquire_execute", "manor:sandbox:executing"),
    ],
)
async def test_cancel_after_global_slot_commit_compensates_token(
    admission_method: str,
    redis_key: str,
):
    class CommitThenBlockRedis(FakeLuaRedis):
        def __init__(self) -> None:
            super().__init__()
            self.slot_committed = asyncio.Event()
            self.allow_result = asyncio.Event()

        async def eval(self, script: str, number_of_keys: int, key: str, *args):
            result = await super().eval(script, number_of_keys, key, *args)
            self.slot_committed.set()
            await self.allow_result.wait()
            return result

    redis = CommitThenBlockRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_active=1,
            global_max_executing=1,
            instance_max_executing=1,
            wait_timeout_seconds=0,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    admission = asyncio.create_task(getattr(gate, admission_method)())
    await redis.slot_committed.wait()
    assert await redis.zcard(redis_key) == 1

    admission.cancel()
    await asyncio.sleep(0)
    redis.allow_result.set()

    with pytest.raises(asyncio.CancelledError):
        await admission

    assert await redis.zcard(redis_key) == 0
    if admission_method == "acquire_execute":
        assert gate._execute_semaphore._value == 1


@pytest.mark.asyncio
async def test_cancelled_execute_lease_release_is_retryable_and_returns_local_slot():
    class CancelOnceRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.remove_calls = 0

        async def zrem(self, key: str, member: str) -> int:
            self.remove_calls += 1
            if self.remove_calls == 1:
                raise asyncio.CancelledError()
            return await super().zrem(key, member)

    redis = CancelOnceRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=1,
            instance_max_executing=1,
            wait_timeout_seconds=0,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    lease = await gate.acquire_execute()

    with pytest.raises(asyncio.CancelledError):
        await lease.release()

    assert gate._execute_semaphore._value == 1
    assert lease._released is False

    await lease.release()
    await lease.release()

    assert redis.remove_calls == 2
    assert await redis.zcard("manor:sandbox:executing") == 0
    assert gate._execute_semaphore._value == 1


@pytest.mark.asyncio
async def test_concurrent_lease_releases_remove_and_return_slot_once():
    class BlockingRemoveRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.remove_started = asyncio.Event()
            self.allow_remove = asyncio.Event()
            self.remove_calls = 0

        async def zrem(self, key: str, member: str) -> int:
            self.remove_calls += 1
            self.remove_started.set()
            await self.allow_remove.wait()
            return await super().zrem(key, member)

    redis = BlockingRemoveRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=1,
            instance_max_executing=1,
            wait_timeout_seconds=0,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    lease = await gate.acquire_execute()

    first_release = asyncio.create_task(lease.release())
    await redis.remove_started.wait()
    second_release = asyncio.create_task(lease.release())
    await asyncio.sleep(0)
    redis.allow_remove.set()
    await asyncio.gather(first_release, second_release)

    assert redis.remove_calls == 1
    assert await redis.zcard("manor:sandbox:executing") == 0
    assert gate._execute_semaphore._value == 1


@pytest.mark.asyncio
async def test_cancelled_direct_lease_release_completes_once_without_leaks():
    class BlockingRemoveRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.remove_started = asyncio.Event()
            self.allow_remove = asyncio.Event()
            self.remove_calls = 0

        async def zrem(self, key: str, member: str) -> int:
            self.remove_calls += 1
            self.remove_started.set()
            await self.allow_remove.wait()
            return await super().zrem(key, member)

    redis = BlockingRemoveRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=1,
            instance_max_executing=1,
            wait_timeout_seconds=0,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    lease = await gate.acquire_execute()
    release_task = asyncio.create_task(lease.release())
    await redis.remove_started.wait()

    release_task.cancel()
    await asyncio.sleep(0)
    try:
        assert release_task.done() is False
    finally:
        redis.allow_remove.set()

    with pytest.raises(asyncio.CancelledError):
        await release_task

    assert lease._released is True
    assert redis.remove_calls == 1
    assert await redis.zcard("manor:sandbox:executing") == 0
    assert gate._execute_semaphore._value == 1

    await lease.release()
    assert redis.remove_calls == 1
    assert gate._execute_semaphore._value == 1


@pytest.mark.asyncio
async def test_sandbox_global_active_gate_recovers_expired_slots():
    redis = FakeRedis()
    now = 1_000.0
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(global_max_active=1, ttl_seconds=10, wait_timeout_seconds=0),
        redis_getter=lambda: redis,
        instance_id="test",
        clock=lambda: now,
    )

    await gate.acquire_active()

    now = 1_011.0
    recovered = await gate.acquire_active()

    assert await redis.zcard("manor:sandbox:active") == 1
    await recovered.release()


@pytest.mark.asyncio
async def test_sandbox_concurrency_lease_renews_active_slot():
    redis = FakeLuaRedis()
    now = 1_000.0
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(global_max_active=1, ttl_seconds=10, wait_timeout_seconds=0),
        redis_getter=lambda: redis,
        instance_id="test",
        clock=lambda: now,
    )

    lease = await gate.acquire_active()
    token = next(iter(redis.zsets["manor:sandbox:active"]))

    now = 1_008.0
    renewed = await lease.renew()

    assert renewed is True
    assert redis.zsets["manor:sandbox:active"][token] == 1_008.0
    assert redis.expires["manor:sandbox:active"] == 10
    await lease.release()


@pytest.mark.asyncio
async def test_redis_acquire_timeout_compensates_known_token():
    class CommitThenDelayRedis(FakeLuaRedis):
        def __init__(self) -> None:
            super().__init__()
            self.allow_result = asyncio.Event()

        async def eval(self, script: str, number_of_keys: int, key: str, *args):
            result = await super().eval(script, number_of_keys, key, *args)
            await self.allow_result.wait()
            return result

    redis = CommitThenDelayRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_active=1,
            wait_timeout_seconds=0,
            operation_timeout_seconds=0.01,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )

    async def unblock_later() -> None:
        await asyncio.sleep(0.05)
        redis.allow_result.set()

    unblock_task = asyncio.create_task(unblock_later())
    try:
        with pytest.raises(SandboxConcurrencyExceeded, match="unavailable"):
            await gate.acquire_active()
    finally:
        redis.allow_result.set()
        await unblock_task

    assert await redis.zcard("manor:sandbox:active") == 0


@pytest.mark.asyncio
async def test_redis_operation_timeout_bounds_lease_lifecycle():
    class DelayLeaseRedis(FakeLuaRedis):
        def __init__(self) -> None:
            super().__init__()
            self.eval_count = 0
            self.allow_renew = asyncio.Event()
            self.remove_started = asyncio.Event()
            self.allow_remove = asyncio.Event()

        async def eval(self, script: str, number_of_keys: int, key: str, *args):
            self.eval_count += 1
            result = await super().eval(script, number_of_keys, key, *args)
            if self.eval_count > 1:
                await self.allow_renew.wait()
            return result

        async def zrem(self, key: str, member: str) -> int:
            self.remove_started.set()
            await self.allow_remove.wait()
            return await super().zrem(key, member)

    redis = DelayLeaseRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(
            global_max_executing=1,
            instance_max_executing=1,
            operation_timeout_seconds=0.01,
        ),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    lease = await gate.acquire_execute()

    async def unblock_later() -> None:
        await asyncio.sleep(0.05)
        redis.allow_renew.set()

    unblock_task = asyncio.create_task(unblock_later())
    try:
        with pytest.raises(SandboxConcurrencyExceeded, match="unavailable"):
            await lease.renew()
    finally:
        redis.allow_renew.set()
        await unblock_task

    release_task = asyncio.create_task(lease.release())
    await redis.remove_started.wait()

    try:
        await asyncio.wait_for(asyncio.shield(release_task), timeout=0.2)
    finally:
        redis.allow_remove.set()
        await release_task

    assert gate._execute_semaphore._value == 1


@pytest.mark.asyncio
async def test_skill_runner_shutdown_releases_active_sandbox_leases():
    class DummySandbox:
        destroyed = False

        async def destroy(self) -> None:
            self.destroyed = True

    class DummyLease:
        released = False

        async def release(self) -> None:
            self.released = True

    runner = SkillRunner()
    sandbox = DummySandbox()
    lease = DummyLease()
    runner._sandboxes["sandbox-1"] = sandbox
    runner._active_leases["sandbox-1"] = lease

    await runner.shutdown()

    assert sandbox.destroyed is True
    assert lease.released is True
    assert runner._sandboxes == {}
    assert runner._active_leases == {}


@pytest.mark.asyncio
async def test_cancelled_destroy_lease_release_retains_registry_until_retry():
    class BlockingRemoveRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.remove_started = asyncio.Event()
            self.allow_remove = asyncio.Event()
            self.remove_calls = 0

        async def zrem(self, key: str, member: str) -> int:
            self.remove_calls += 1
            self.remove_started.set()
            await self.allow_remove.wait()
            return await super().zrem(key, member)

    class IdempotentSandbox:
        def __init__(self) -> None:
            self.destroy_count = 0

        async def destroy(self) -> None:
            self.destroy_count += 1

    redis = BlockingRemoveRedis()
    gate = SandboxConcurrencyGate(
        SandboxGateConfig(global_max_active=1, wait_timeout_seconds=0),
        redis_getter=lambda: redis,
        instance_id="test",
    )
    lease = await gate.acquire_active()
    runner = SkillRunner()
    assert await runner._local_active_capacity.try_acquire() is True
    sandbox = IdempotentSandbox()
    bridge = object()
    runner._sandboxes["sandbox-1"] = sandbox
    runner._fs_bridges["sandbox-1"] = bridge
    runner._active_leases["sandbox-1"] = lease

    destroy_task = asyncio.create_task(runner.destroy_sandbox("sandbox-1"))
    await redis.remove_started.wait()
    destroy_task.cancel()
    await asyncio.sleep(0)
    try:
        assert destroy_task.done() is False
    finally:
        redis.allow_remove.set()

    with pytest.raises(asyncio.CancelledError):
        await destroy_task

    assert runner._sandboxes["sandbox-1"] is sandbox
    assert runner._fs_bridges["sandbox-1"] is bridge
    assert runner._active_leases["sandbox-1"] is lease
    assert runner._local_active_capacity.reserved == 1
    assert lease._released is True

    await runner.destroy_sandbox("sandbox-1")

    assert sandbox.destroy_count == 2
    assert redis.remove_calls == 1
    assert runner._sandboxes == {}
    assert runner._fs_bridges == {}
    assert runner._active_leases == {}
    assert runner._local_active_capacity.reserved == 0


@pytest.mark.asyncio
async def test_shutdown_waits_for_admitted_create_then_destroys_it(monkeypatch, tmp_path):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "shutdown-overlap-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Shutdown Overlap Skill\n", encoding="utf-8")

    first_setup_started = asyncio.Event()
    allow_first_setup = asyncio.Event()
    setup_calls = 0
    sandboxes = []

    class OverlapDockerSandbox:
        def __init__(self, sandbox_id, config, **_):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING
            self.destroyed = False
            sandboxes.append(self)

        async def setup(self, skill, env, auto_install=True, baked=None):
            nonlocal setup_calls
            setup_calls += 1
            if setup_calls == 1:
                first_setup_started.set()
                await allow_first_setup.wait()
            self.status = SandboxStatus.READY

        async def destroy(self):
            self.destroyed = True
            self.status = SandboxStatus.DESTROYED

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 2)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", OverlapDockerSandbox)

    runner = SkillRunner()
    create_task = asyncio.create_task(
        runner.create_sandbox(str(skill_dir), env={}, auto_install=False)
    )
    await first_setup_started.wait()

    shutdown_task = asyncio.create_task(runner.shutdown())
    await asyncio.sleep(0)

    try:
        with pytest.raises(RuntimeError, match="shutting down"):
            await runner.create_sandbox(str(skill_dir), env={}, auto_install=False)
    finally:
        allow_first_setup.set()
        create_result, shutdown_result = await asyncio.gather(
            create_task,
            shutdown_task,
            return_exceptions=True,
        )

    assert isinstance(create_result, CreateSandboxResponse)
    assert shutdown_result is None
    assert setup_calls == 1
    assert sandboxes[0].destroyed is True
    assert runner._sandboxes == {}
    assert runner._local_active_capacity.reserved == 0


@pytest.mark.asyncio
async def test_skill_runner_prune_loop_renews_active_sandbox_leases(monkeypatch):
    class DummyLease:
        def __init__(self) -> None:
            self.renew_count = 0

        async def renew(self) -> bool:
            self.renew_count += 1
            return True

    lease = DummyLease()
    runner = SkillRunner()
    runner._active_leases["sandbox-1"] = lease

    sleeps = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await runner._prune_loop()

    assert lease.renew_count == 1


@pytest.mark.asyncio
async def test_active_lease_renew_keeps_local_and_quarantines_lost_global():
    class LostLease:
        def __init__(self) -> None:
            self.release_count = 0

        async def renew(self) -> bool:
            return False

        async def release(self) -> None:
            self.release_count += 1

    class FailedDestroySandbox:
        def __init__(self) -> None:
            self.status = SandboxStatus.READY
            self.destroy_count = 0

        async def destroy(self) -> None:
            self.destroy_count += 1
            self.status = SandboxStatus.ERROR
            raise RuntimeError("cleanup failed")

    runner = SkillRunner()
    local_lease = await SandboxConcurrencyGate(
        SandboxGateConfig(global_max_active=0)
    ).acquire_active()
    assert await runner._local_active_capacity.try_acquire() is True
    assert await runner._local_active_capacity.try_acquire() is True
    lost_lease = LostLease()
    lost_sandbox = FailedDestroySandbox()
    local_sandbox = FailedDestroySandbox()
    runner._active_leases["local-sandbox"] = local_lease
    runner._sandboxes["local-sandbox"] = local_sandbox
    runner._active_leases["lost-sandbox"] = lost_lease
    runner._sandboxes["lost-sandbox"] = lost_sandbox

    await runner._renew_active_leases()

    assert local_sandbox.destroy_count == 0
    assert local_sandbox.status == SandboxStatus.READY
    assert runner._sandboxes["local-sandbox"] is local_sandbox
    assert lost_sandbox.destroy_count == 1
    assert lost_sandbox.status == SandboxStatus.ERROR
    assert runner._sandboxes["lost-sandbox"] is lost_sandbox
    assert runner._active_leases["lost-sandbox"] is lost_lease
    assert runner._local_active_capacity.reserved == 2
    assert lost_lease.release_count == 0


@pytest.mark.asyncio
async def test_skill_runner_local_capacity_limit_uses_429_error(monkeypatch, tmp_path):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "capacity-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Capacity Skill\n", encoding="utf-8")

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 0)

    runner = SkillRunner()

    with pytest.raises(SandboxConcurrencyExceeded) as exc_info:
        await runner.create_sandbox(str(skill_dir), env={})

    assert "Max active sandbox instance limit reached" in str(exc_info.value)


@pytest.mark.asyncio
async def test_concurrent_creates_respect_local_active_limit(monkeypatch, tmp_path):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "concurrent-capacity-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Concurrent Capacity Skill\n", encoding="utf-8")

    setup_started = asyncio.Event()
    allow_setup = asyncio.Event()

    class BarrierDockerSandbox:
        def __init__(self, sandbox_id, config, **_):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING

        async def setup(self, skill, env, auto_install=True, baked=None):
            setup_started.set()
            await allow_setup.wait()
            self.status = SandboxStatus.READY

        async def destroy(self):
            self.status = SandboxStatus.DESTROYED

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 1)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", BarrierDockerSandbox)

    runner = SkillRunner()
    creates = [
        asyncio.create_task(runner.create_sandbox(str(skill_dir), env={}, auto_install=False))
        for _ in range(2)
    ]
    await setup_started.wait()
    # The skill scan runs in a worker thread, so the second create can reach
    # admission at any point; hold the first in setup until the second has
    # been decided, so the two creates really overlap.
    decided, _ = await asyncio.wait(creates, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    assert len(decided) == 1
    allow_setup.set()

    results = await asyncio.gather(*creates, return_exceptions=True)

    assert sum(isinstance(item, CreateSandboxResponse) for item in results) == 1
    assert sum(isinstance(item, SandboxConcurrencyExceeded) for item in results) == 1
    assert len(runner._sandboxes) == 1


@pytest.mark.asyncio
async def test_container_heavy_operations_hold_execute_gate_but_file_side_channel_does_not(
    monkeypatch, tmp_path
):
    """Create and load-skill take an execute slot; file reads/writes are side-channel
    operations that must work next to a running command, so they never wait on
    (or get rejected by) the execute gate."""
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "operation-gate-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Operation Gate Skill\n", encoding="utf-8")

    class ActiveLease:
        async def release(self) -> None:
            return None

        async def renew(self) -> bool:
            return True

    class ExecuteLease:
        async def __aenter__(self):
            gate.active += 1
            gate.max_active = max(gate.max_active, gate.active)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            gate.active -= 1

    class TrackingGate:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.acquire_count = 0

        async def acquire_active(self):
            return ActiveLease()

        async def acquire_execute(self):
            self.acquire_count += 1
            return ExecuteLease()

    gate = TrackingGate()

    class GatedSandbox:
        def __init__(self, sandbox_id, config, **_):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING
            self.skill = None
            self.created_at = 0.0
            self.last_used_at = 0.0

        def ensure_available(self) -> None:
            return None

        async def setup(self, skill, env, auto_install=True, baked=None):
            assert gate.active == 1
            self.skill = skill
            self.status = SandboxStatus.READY

        async def load_skill(self, **kwargs):
            assert gate.active == 1
            return self.skill

    class GatedBridge:
        fail_write = False

        def __init__(self, sandbox):
            self.sandbox = sandbox

        async def read_file(self, *args, **kwargs):
            assert gate.active == 0
            return "content", False

        async def read_file_base64(self, *args, **kwargs):
            assert gate.active == 0
            return "YmluYXJ5", 6

        async def write_file(self, *args, **kwargs):
            assert gate.active == 0
            if self.fail_write:
                raise RuntimeError("write failed")

        async def write_file_base64(self, *args, **kwargs):
            assert gate.active == 0

        async def read_key_skill_files(self, *args, **kwargs):
            assert gate.active == 0
            return {"main.py": "print('ok')"}

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 1)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", GatedSandbox)
    monkeypatch.setattr(skill_runner_module, "FsBridge", GatedBridge)

    runner = SkillRunner()
    runner._gate = gate
    created = await runner.create_sandbox(str(skill_dir), env={}, auto_install=False)
    sandbox_id = created.sandbox_id

    await runner.read_file(sandbox_id, "/skill/a.txt")
    await runner.read_file_base64(sandbox_id, "/skill/image.png")
    await runner.write_file(sandbox_id, "/skill/b.txt", "value")
    await runner.write_file_base64(sandbox_id, "/skill/image.png", "YmluYXJ5")
    await runner.get_skill_context(sandbox_id)
    await runner.load_skill_into_sandbox(
        sandbox_id,
        "replacement",
        {"SKILL.md": "# Replacement"},
    )

    bridge = runner._fs_bridges[sandbox_id]
    bridge.fail_write = True
    with pytest.raises(RuntimeError, match="write failed"):
        await runner.write_file(sandbox_id, "/skill/fail.txt", "value")

    assert gate.acquire_count == 2  # create + load-skill
    assert gate.max_active == 1
    assert gate.active == 0


@pytest.mark.asyncio
async def test_cancelled_create_waits_for_setup_and_cleanup(monkeypatch, tmp_path):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "cancelled-create-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Cancelled Create Skill\n", encoding="utf-8")

    setup_started = asyncio.Event()
    allow_setup = asyncio.Event()
    destroy_started = asyncio.Event()
    allow_destroy = asyncio.Event()
    sandboxes = []

    class CancellableDockerSandbox:
        def __init__(self, sandbox_id, config, **_):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING
            self.setup_finished = False
            self.destroyed = False
            sandboxes.append(self)

        async def setup(self, skill, env, auto_install=True, baked=None):
            setup_started.set()
            await allow_setup.wait()
            self.setup_finished = True
            self.status = SandboxStatus.READY

        async def destroy(self):
            assert self.setup_finished is True
            destroy_started.set()
            await allow_destroy.wait()
            self.destroyed = True
            self.status = SandboxStatus.DESTROYED

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 1)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", CancellableDockerSandbox)

    runner = SkillRunner()
    create_task = asyncio.create_task(
        runner.create_sandbox(str(skill_dir), env={}, auto_install=False)
    )
    await setup_started.wait()

    create_task.cancel()
    await asyncio.sleep(0)

    assert create_task.done() is False
    assert sandboxes[0].destroyed is False

    allow_setup.set()
    await destroy_started.wait()
    assert create_task.done() is False

    allow_destroy.set()
    with pytest.raises(asyncio.CancelledError):
        await create_task

    assert sandboxes[0].destroyed is True
    assert runner._sandboxes == {}
    assert runner._local_active_capacity.reserved == 0


@pytest.mark.asyncio
async def test_failed_partial_create_cleanup_retains_sandbox_and_reservations(
    monkeypatch,
    tmp_path,
):
    import sandbox.skill_runner as skill_runner_module

    skill_dir = tmp_path / "failed-cleanup-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Failed Cleanup Skill\n", encoding="utf-8")

    sandboxes = []

    class FailedCleanupSandbox:
        def __init__(self, sandbox_id, config, **_):
            self.sandbox_id = sandbox_id
            self.config = config
            self.container_name = f"test-{sandbox_id}"
            self.status = SandboxStatus.CREATING
            sandboxes.append(self)

        async def setup(self, skill, env, auto_install=True, baked=None):
            self.status = SandboxStatus.ERROR
            raise RuntimeError("setup failed")

        async def destroy(self):
            self.status = SandboxStatus.ERROR
            raise RuntimeError("cleanup failed")

    class DummyLease:
        def __init__(self) -> None:
            self.released = False

        async def release(self) -> None:
            self.released = True

    class ExecuteLease:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class DummyGate:
        def __init__(self, lease) -> None:
            self.lease = lease

        async def acquire_active(self):
            return self.lease

        async def acquire_execute(self):
            return ExecuteLease()

    monkeypatch.setattr(skill_runner_module.app_config, "MAX_SANDBOXES", 1)
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", FailedCleanupSandbox)

    runner = SkillRunner()
    lease = DummyLease()
    runner._gate = DummyGate(lease)

    with pytest.raises(RuntimeError, match="setup failed"):
        await runner.create_sandbox(str(skill_dir), env={}, auto_install=False)

    sandbox = sandboxes[0]
    assert runner._sandboxes[sandbox.sandbox_id] is sandbox
    assert runner._active_leases[sandbox.sandbox_id] is lease
    assert runner._local_active_capacity.reserved == 1
    assert await runner._local_active_capacity.try_acquire() is False
    assert lease.released is False
    assert sandbox.status == SandboxStatus.ERROR


@pytest.mark.asyncio
async def test_quarantined_sandbox_rejects_commands_and_file_mutation():
    class FailIfCalledBridge:
        async def write_file(self, *args, **kwargs):
            raise AssertionError("quarantined sandbox reached FsBridge")

    for status, destroy_requested in (
        (SandboxStatus.ERROR, False),
        (SandboxStatus.READY, True),
    ):
        runner = SkillRunner()
        sandbox = DockerSandbox("sandbox-1", ContainerConfig())
        sandbox.status = status
        sandbox._destroy_requested = destroy_requested
        runner._sandboxes["sandbox-1"] = sandbox
        runner._fs_bridges["sandbox-1"] = FailIfCalledBridge()

        with pytest.raises(RuntimeError, match="unavailable"):
            await runner.exec_command("sandbox-1", "echo unsafe")

        with pytest.raises(RuntimeError, match="unavailable"):
            await runner.load_skill_into_sandbox(
                "sandbox-1",
                "replacement",
                {"SKILL.md": "# Replacement"},
            )

        with pytest.raises(RuntimeError, match="unavailable"):
            await runner.write_file("sandbox-1", "/skill/output.txt", "unsafe")


@pytest.mark.asyncio
async def test_failed_docker_destroy_retains_registry_and_local_capacity(monkeypatch):
    import sandbox.docker_backend as docker_backend_module

    def fake_run_docker(args, **kwargs):
        if args[1] == "rm":
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"device busy")
        if args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, stdout=b"true\n", stderr=b"")
        raise AssertionError(f"Unexpected Docker command: {args}")

    class DummyLease:
        def __init__(self) -> None:
            self.released = False

        async def release(self) -> None:
            self.released = True

    monkeypatch.setattr(docker_backend_module, "_run_docker", fake_run_docker)

    runner = SkillRunner()
    assert await runner._local_active_capacity.try_acquire() is True
    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "test-sandbox-1"
    sandbox.status = SandboxStatus.READY
    lease = DummyLease()
    runner._sandboxes["sandbox-1"] = sandbox
    runner._active_leases["sandbox-1"] = lease

    with pytest.raises(RuntimeError, match="still exists"):
        await runner.destroy_sandbox("sandbox-1")

    assert runner._sandboxes["sandbox-1"] is sandbox
    assert runner._active_leases["sandbox-1"] is lease
    assert runner._local_active_capacity.reserved == 1
    assert lease.released is False
    assert sandbox.status == SandboxStatus.ERROR


@pytest.mark.asyncio
async def test_daemon_unavailable_destroy_retains_registry_and_capacity(monkeypatch):
    import sandbox.docker_backend as docker_backend_module

    daemon_error = b"Cannot connect to the Docker daemon at unix:///var/run/docker.sock. " + b"x" * 5000

    def fake_run_docker(args, **kwargs):
        if args[1] in {"rm", "inspect"}:
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=daemon_error)
        raise AssertionError(f"Unexpected Docker command: {args}")

    class DummyLease:
        def __init__(self) -> None:
            self.released = False

        async def release(self) -> None:
            self.released = True

    monkeypatch.setattr(docker_backend_module, "_run_docker", fake_run_docker)

    runner = SkillRunner()
    assert await runner._local_active_capacity.try_acquire() is True
    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "test-sandbox-1"
    sandbox.status = SandboxStatus.READY
    lease = DummyLease()
    runner._sandboxes["sandbox-1"] = sandbox
    runner._active_leases["sandbox-1"] = lease

    with pytest.raises(RuntimeError) as exc_info:
        await runner.destroy_sandbox("sandbox-1")

    assert "Cannot connect to the Docker daemon" in str(exc_info.value)
    assert len(str(exc_info.value)) < 800
    assert runner._sandboxes["sandbox-1"] is sandbox
    assert runner._active_leases["sandbox-1"] is lease
    assert runner._local_active_capacity.reserved == 1
    assert lease.released is False
    assert sandbox.status == SandboxStatus.ERROR


@pytest.mark.asyncio
async def test_missing_docker_container_destroy_is_idempotent(monkeypatch):
    import sandbox.docker_backend as docker_backend_module

    def fake_run_docker(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            1,
            stdout=b"",
            stderr=b"Error: No such container: test-sandbox-1",
        )

    monkeypatch.setattr(docker_backend_module, "_run_docker", fake_run_docker)

    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "test-sandbox-1"
    sandbox.status = SandboxStatus.READY

    await sandbox.destroy()

    assert sandbox.status == SandboxStatus.DESTROYED


def _runner_api_client(monkeypatch, runner):
    import importlib.util

    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(
        "_sandbox_service_main_concurrency_test", SANDBOX_SERVICE_ROOT / "main.py",
    )
    assert spec and spec.loader
    main = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, main)
    spec.loader.exec_module(main)
    monkeypatch.setattr(main.config, "API_TOKEN", "runner-test-secret")
    monkeypatch.setattr(sandbox_routes, "_runner", runner)
    # Not entering the client context deliberately avoids the Docker lifespan.
    return TestClient(
        main.app,
        raise_server_exceptions=False,
        headers={"X-Manor-Sandbox-Token": "runner-test-secret"},
    )


@pytest.mark.parametrize(
    "runner_method,method,path,body",
    [
        (
            "create_sandbox_from_files",
            "POST",
            "/api/v1/sandbox/create-from-files",
            {"skill_name": "capacity-smoke", "files": {"SKILL.md": "# Capacity Smoke"}},
        ),
        (
            "exec_command",
            "POST",
            "/api/v1/sandbox/sandbox-1/exec",
            {"command": "python run.py"},
        ),
        (
            "run_skill",
            "POST",
            "/api/v1/skill/run",
            {"skill_dir": "/tmp/capacity-smoke", "commands": ["python run.py"]},
        ),
    ],
)
def test_capacity_rejections_answer_429_with_a_code(monkeypatch, runner_method, method, path, body):
    class RejectingRunner:
        pass

    def reject(*_args, **_kwargs):
        raise SandboxConcurrencyExceeded("Max active sandbox limit reached (1)")

    runner = RejectingRunner()
    setattr(runner, runner_method, reject)
    client = _runner_api_client(monkeypatch, runner)

    response = client.request(method, path, json=body)

    assert response.status_code == 429, response.text
    assert response.json()["code"] == "capacity_exceeded"
    assert "Max active sandbox limit reached" in response.json()["detail"]


@pytest.mark.asyncio
async def test_operation_guard_executing_lease_is_renewed_while_held():
    """A long exec outlives the gate TTL; the prune loop must keep its slot fresh."""

    class RenewableLease:
        def __init__(self):
            self.renewals = 0
            self.released = False

        async def renew(self):
            self.renewals += 1
            return True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.released = True

    lease = RenewableLease()

    class FakeGate:
        async def acquire_execute(self):
            return lease

    runner = SkillRunner()
    runner._gate = FakeGate()
    async with runner._operation_guard():
        await runner._renew_executing_leases()
        assert lease.renewals == 1
    assert lease.released is True
    await runner._renew_executing_leases()
    assert lease.renewals == 1  # released leases are no longer tracked
    assert runner._executing_leases == set()


@pytest.mark.asyncio
async def test_executing_lease_renewal_failure_is_logged_not_raised():
    class BrokenLease:
        async def renew(self):
            raise RuntimeError("redis down")

    runner = SkillRunner()
    runner._executing_leases.add(BrokenLease())
    await runner._renew_executing_leases()


@pytest.mark.asyncio
async def test_status_and_cancel_work_while_exec_holds_state_lock_and_executing_slot(monkeypatch):
    """Pollers must reach status/cancel for a 1h background exec that holds every gate."""
    from sandbox import docker_backend
    from sandbox.models import ExecutionStatus, ExecutionStatusResponse

    def fake_run(args, timeout=None, allow_failure=False, **_kw):
        command = args[-1] if args else ""
        if args[:4] == ["docker", "exec", "ctr", "python"]:  # event mailbox reader
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if isinstance(command, str) and command.startswith("cat "):
            return subprocess.CompletedProcess(args, 0, stdout=b"321\n", stderr=b"")
        if isinstance(command, str) and "kill -TERM" in command:
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if isinstance(command, str) and "kill -0" in command:
            return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"")
        raise AssertionError(f"Unexpected Docker command: {args}")

    monkeypatch.setattr(docker_backend, "_run_docker", fake_run)
    runner = SkillRunner()
    runner._gate = SandboxConcurrencyGate(SandboxGateConfig(instance_max_executing=1, wait_timeout_seconds=0))
    sandbox = DockerSandbox("sandbox-1", ContainerConfig())
    sandbox.container_name = "ctr"
    sandbox.status = SandboxStatus.READY
    # A 1h command holds the sandbox's command claim (the runner's per-sandbox
    # exclusivity since the claim state machine replaced the state lock).
    sandbox.claim_command("exec-1", "python long.py", 3600)
    runner._sandboxes["sandbox-1"] = sandbox
    key = ("sandbox-1", "exec-1")
    runner._execution_records[key] = ExecutionStatusResponse(
        sandbox_id="sandbox-1", execution_id="exec-1", status=ExecutionStatus.RUNNING, created_at=1.0,
    )
    runner._execution_locks[key] = asyncio.Lock()

    assert sandbox.holds_command("exec-1")
    async with runner._operation_guard():
        # The executing slot is exhausted: guarded operations are refused...
        with pytest.raises(SandboxConcurrencyExceeded):
            async with runner._operation_guard():
                pass
        # ...but status and cancel still go through.
        status = await asyncio.wait_for(runner.get_execution_status("sandbox-1", "exec-1"), timeout=2)
        assert status.status == ExecutionStatus.RUNNING
        assert await asyncio.wait_for(runner.cancel_execution("sandbox-1", "exec-1"), timeout=5) is True
