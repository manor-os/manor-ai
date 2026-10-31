"""Memory admission and caller-override bounds on the Sandbox runner.

1. A container is admitted against the host's memory budget (its ``--memory``
   limit is reserved), not just counted; ``MAX_SANDBOXES`` stays a hard cap.
2. The reservation is returned on every way a container ends: destroy, reap,
   loss, a failed create.
3. A full runner answers 429 ``runner_full`` with the memory numbers, before
   anything is created.
4. A request above the per-container maximum is clamped (with a warning), so
   it can never be unschedulable on a smaller runner.
5. Callers may override only bounded resource/timeout fields; every other
   ``ContainerConfig`` field is runner-owned and rejected with 400.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from tests.sandbox_runner_fakes import (
    API_TOKEN,
    FakeDocker,
    load_service_main,
    register_sandbox,
)

import sandbox.docker_backend as docker_backend  # noqa: E402
import sandbox.skill_runner as skill_runner_module  # noqa: E402
from sandbox.capacity import (  # noqa: E402
    MIB,
    HostResources,
    compute_limits,
    format_size,
    parse_size,
)
from sandbox.errors import ConfigOverrideRejected, RunnerFull  # noqa: E402
from sandbox.models import ContainerConfig, CreateSandboxResponse, SandboxStatus  # noqa: E402
from sandbox.overrides import OVERRIDABLE_FIELDS, RUNNER_OWNED_FIELDS  # noqa: E402
from sandbox.security import SecurityError  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402

GIB = 1024 * MIB


@pytest.fixture
def budget(monkeypatch):
    """A runner sized like a 2 GiB, 1-CPU staging droplet with the default policy."""

    config = skill_runner_module.app_config
    monkeypatch.setattr(config, "HOST_MEMORY", "2g")
    monkeypatch.setattr(config, "HOST_CPUS", 1.0)
    monkeypatch.setattr(config, "MEMORY_OVERCOMMIT", 2.0)
    monkeypatch.setattr(config, "MEMORY_RESERVE", "256m")
    monkeypatch.setattr(config, "MEMORY_BUDGET", "")
    monkeypatch.setattr(config, "MAX_CONTAINER_MEMORY", "")
    monkeypatch.setattr(config, "MAX_SANDBOXES", 5)
    monkeypatch.setattr(config, "SANDBOX_MEMORY", "512m")
    monkeypatch.setattr(config, "SANDBOX_CPUS", 1.0)
    return config


class _StubSandbox:
    """Stands in for DockerSandbox: records the config it was created with."""

    instances: list["_StubSandbox"] = []
    setup_error: BaseException | None = None
    destroy_error: BaseException | None = None

    def __init__(self, sandbox_id, config, **_):
        self.sandbox_id = sandbox_id
        self.config = config
        self.container_name = f"stub-{sandbox_id}"
        self.status = SandboxStatus.CREATING
        self.skill = None
        self.created_at = time.time()
        self.last_used_at = time.time()
        self.busy_deadline = None
        self.expires_at = None
        self.active_command = None
        self.active_execution_id = None
        self.destroyed = False
        type(self).instances.append(self)

    async def setup(self, skill, env, auto_install=True, baked=None):
        if type(self).setup_error is not None:
            raise type(self).setup_error
        self.skill = skill
        self.status = SandboxStatus.READY

    async def destroy(self):
        if type(self).destroy_error is not None:
            raise type(self).destroy_error
        self.destroyed = True
        self.status = SandboxStatus.DESTROYED

    def ensure_available(self):
        return None


@pytest.fixture
def stub_sandboxes(monkeypatch):
    _StubSandbox.instances = []
    _StubSandbox.setup_error = None
    _StubSandbox.destroy_error = None
    monkeypatch.setattr(skill_runner_module, "DockerSandbox", _StubSandbox)
    return _StubSandbox


@pytest.fixture
def skill_dir(tmp_path):
    path = tmp_path / "demo-skill"
    path.mkdir()
    (path / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    return str(path)


async def _create(runner, skill_dir, **overrides) -> CreateSandboxResponse:
    config = ContainerConfig(**overrides) if overrides else None
    return await runner.create_sandbox(skill_dir, env={}, config_overrides=config, auto_install=False)


# ── 1. the budget ──


def test_budget_formula_and_defaults_fit_todays_deployments():
    """The documented math: today's sandbox counts still fit, video still runs."""

    def limits(host_gib: float):
        return compute_limits(
            HostResources(int(host_gib * GIB), 1.0, "test"), overcommit=2.0, reserve="256m"
        )

    staging, prod = limits(2), limits(4)
    assert staging.budget == (2 * 2048 - 256) * MIB
    assert staging.max_container == (2048 - 256) * MIB
    assert prod.budget == (2 * 4096 - 256) * MIB
    # Today's shapes: 5 x 512m (default), 5 x 256m (staging env), 10 x 384m (prod env).
    for host in (staging, prod):
        assert 5 * 512 * MIB <= host.budget
        assert 5 * 256 * MIB <= host.budget
    assert 10 * 384 * MIB <= prod.budget
    # A HyperFrames render holds two containers asking 4g each: both clamp to
    # the per-container maximum and both fit.
    for host in (staging, prod):
        clamped = min(4 * GIB, host.max_container)
        assert 2 * clamped <= host.budget
    # The per-container maximum is never above the budget.
    tiny = compute_limits(HostResources(GIB, 1.0, "t"), overcommit=0.5, reserve="0")
    assert tiny.max_container <= tiny.budget


@pytest.mark.parametrize(
    "text,expected",
    [("512m", 512 * MIB), ("2g", 2 * GIB), ("1.5GiB", int(1.5 * GIB)), ("64MB", 64 * MIB), ("1048576", MIB)],
)
def test_sizes_parse_like_docker(text, expected):
    assert parse_size(text) == expected
    assert parse_size(format_size(expected)) == expected


@pytest.mark.parametrize("text", ["", "lots", "-1m", "5x"])
def test_bad_sizes_are_rejected(text):
    with pytest.raises(ValueError):
        parse_size(text)


@pytest.mark.asyncio
async def test_admission_is_by_memory_and_releases_on_destroy(budget, stub_sandboxes, skill_dir):
    runner = SkillRunner()
    # 2g host: budget 3840m, per container at most 1792m.
    assert runner.memory_status()["memory_budget_mb"] == 3840
    big = [await _create(runner, skill_dir, memory="1792m") for _ in range(2)]
    status = runner.memory_status()
    assert status["memory_reserved_mb"] == 3584
    assert status["memory_available_mb"] == 256
    assert status["largest_allocatable_mb"] == 256

    # Two slots are used of five, yet a 512m container does not fit.
    with pytest.raises(RunnerFull) as exc_info:
        await _create(runner, skill_dir)
    error = exc_info.value
    assert error.status_code == 429 and error.code == "runner_full"
    assert error.extra["reason"] == "memory"
    assert error.extra["required_memory_mb"] == 512
    assert error.extra["available_memory_mb"] == 256
    assert error.extra["memory_budget_mb"] == 3840
    # Nothing was created for the rejected request.
    assert len(stub_sandboxes.instances) == 2
    assert runner._local_active_capacity.reserved == 2

    # A 256m container still fits exactly.
    small = await _create(runner, skill_dir, memory="256m")
    assert runner.memory_status()["memory_available_mb"] == 0

    await runner.destroy_sandbox(big[0].sandbox_id)
    assert runner.memory_status()["memory_reserved_mb"] == 1792 + 256
    await runner.destroy_sandbox(big[1].sandbox_id)
    await runner.destroy_sandbox(small.sandbox_id)
    assert runner.memory_status()["memory_reserved_mb"] == 0
    assert runner._memory_reservations == {}
    assert runner._local_active_capacity.reserved == 0


@pytest.mark.asyncio
async def test_max_sandboxes_stays_a_hard_cap(budget, stub_sandboxes, skill_dir, monkeypatch):
    monkeypatch.setattr(budget, "MAX_SANDBOXES", 2)
    runner = SkillRunner()
    await _create(runner, skill_dir, memory="128m")
    await _create(runner, skill_dir, memory="128m")
    assert runner.memory_status()["largest_allocatable_mb"] == 0, "no slot left"
    with pytest.raises(RunnerFull, match="Max active sandbox instance limit reached") as exc_info:
        await _create(runner, skill_dir, memory="128m")
    assert exc_info.value.extra["reason"] == "max_sandboxes"
    assert runner.memory_status()["memory_reserved_mb"] == 256


@pytest.mark.asyncio
async def test_a_full_runner_reaps_idle_sandboxes_before_refusing(budget, stub_sandboxes, skill_dir, monkeypatch):
    monkeypatch.setattr(budget, "IDLE_TIMEOUT_SECONDS", 0)
    runner = SkillRunner()
    await _create(runner, skill_dir, memory="1792m")
    await _create(runner, skill_dir, memory="1792m")
    for sandbox in runner._sandboxes.values():
        sandbox.last_used_at = 0.0

    created = await _create(runner, skill_dir, memory="1g")

    assert created.sandbox_id in runner._sandboxes
    assert runner.memory_status()["memory_reserved_mb"] == 1024


@pytest.mark.asyncio
async def test_failed_create_returns_its_memory(budget, stub_sandboxes, skill_dir):
    runner = SkillRunner()
    stub_sandboxes.setup_error = RuntimeError("docker create failed")
    with pytest.raises(RuntimeError, match="docker create failed"):
        await _create(runner, skill_dir, memory="1g")
    assert runner.memory_status()["memory_reserved_mb"] == 0
    assert runner._local_active_capacity.reserved == 0
    assert stub_sandboxes.instances[0].destroyed is True


@pytest.mark.asyncio
async def test_failed_create_whose_cleanup_fails_keeps_memory_until_the_reaper_destroys_it(
    budget, stub_sandboxes, skill_dir
):
    runner = SkillRunner()
    stub_sandboxes.setup_error = RuntimeError("docker create failed")
    stub_sandboxes.destroy_error = RuntimeError("device or resource busy")
    with pytest.raises(RuntimeError, match="docker create failed"):
        await _create(runner, skill_dir, memory="1g")
    # The container may exist: it keeps its slot and its memory.
    sandbox_id = stub_sandboxes.instances[0].sandbox_id
    assert runner._sandboxes[sandbox_id].status == SandboxStatus.ERROR
    assert runner.memory_status()["memory_reserved_mb"] == 1024

    stub_sandboxes.destroy_error = None
    await runner._prune_idle()
    assert sandbox_id not in runner._sandboxes
    assert runner.memory_status()["memory_reserved_mb"] == 0
    assert runner._local_active_capacity.reserved == 0


@pytest.mark.asyncio
async def test_rejected_override_takes_no_capacity(budget, stub_sandboxes, skill_dir):
    runner = SkillRunner()
    with pytest.raises(ConfigOverrideRejected):
        await runner.create_sandbox(
            skill_dir, env={}, config_overrides=ContainerConfig(image="evil:latest"), auto_install=False
        )
    assert stub_sandboxes.instances == []
    assert runner.memory_status()["memory_reserved_mb"] == 0
    assert runner._local_active_capacity.reserved == 0


@pytest.mark.asyncio
async def test_concurrent_creates_never_overcommit_the_budget(budget, stub_sandboxes, skill_dir):
    runner = SkillRunner()
    results = await asyncio.gather(
        *(_create(runner, skill_dir, memory="1g") for _ in range(5)), return_exceptions=True
    )
    admitted = [item for item in results if isinstance(item, CreateSandboxResponse)]
    refused = [item for item in results if isinstance(item, RunnerFull)]
    # 3840m budget: three 1g containers fit, the fourth would need 4096m.
    assert len(admitted) == 3 and len(refused) == 2
    assert runner.memory_status()["memory_reserved_mb"] == 3072


# ── 2. reclaim paths (loss / reap / quarantine) hand memory back ──


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_backend, "_run_docker", fake)
    monkeypatch.setattr(docker_backend, "_CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(docker_backend, "_CANCEL_POLL_INTERVAL_SECONDS", 0.005)
    return fake


@pytest.mark.asyncio
def _default_container_mb() -> int:
    from sandbox.capacity import parse_size, to_mib

    return to_mib(parse_size(ContainerConfig().memory))


@pytest.mark.asyncio
async def test_lost_container_returns_its_memory(budget, docker, monkeypatch):
    runner = SkillRunner()
    await register_sandbox(runner)
    assert runner.memory_status()["memory_reserved_mb"] == _default_container_mb()
    monkeypatch.setattr(skill_runner_module, "list_running_managed_container_names", lambda _scope: set())
    docker.exists = False
    docker.running = False

    await runner._sweep_lost_containers()

    assert runner.memory_status()["memory_reserved_mb"] == 0
    assert runner._memory_reservations == {}


@pytest.mark.asyncio
async def test_idle_reap_returns_its_memory(budget, docker, monkeypatch):
    monkeypatch.setattr(budget, "IDLE_TIMEOUT_SECONDS", 1)
    runner = SkillRunner()
    await register_sandbox(runner, idle_for=120.0)
    await runner._prune_idle()
    assert runner.memory_status()["memory_reserved_mb"] == 0


@pytest.mark.asyncio
async def test_a_failed_load_keeps_the_shared_sandbox_and_its_memory_until_it_is_destroyed(budget, docker):
    """One sandbox serves all of a user's work (sandbox-user-conversation): a
    Skill whose load fails is discarded from its own /skills/<slot>, the
    sandbox keeps serving the others -- holding its memory -- and the memory
    returns when the sandbox is finally destroyed."""

    runner = SkillRunner()
    await register_sandbox(runner)
    docker.inject_fails = True
    with pytest.raises(Exception):
        await runner.load_skill_into_sandbox(
            "sbx-1", "other", {"SKILL.md": "# Other"}, auto_install=False, idle_threshold=0
        )
    assert "sbx-1" in runner._sandboxes
    assert runner.memory_status()["memory_reserved_mb"] == _default_container_mb()

    await runner.destroy_sandbox("sbx-1")
    assert runner.memory_status()["memory_reserved_mb"] == 0
    assert runner._memory_reservations == {}


# ── 4. oversize requests are clamped, never unschedulable ──


@pytest.mark.asyncio
async def test_oversize_memory_and_cpus_are_clamped_with_a_warning(budget, stub_sandboxes, skill_dir):
    runner = SkillRunner()
    # The HyperFrames video request on a 2g / 1-CPU runner.
    created = await _create(runner, skill_dir, memory="4g", cpus=4.0, pids_limit=512)
    assert created.config.memory == "1792m"
    assert created.config.cpus == 1.0
    assert stub_sandboxes.instances[0].config.memory == "1792m"
    assert any("clamped to 1792m" in warning for warning in created.warnings)
    assert any("cpus 4 exceeds" in warning for warning in created.warnings)
    assert runner.memory_status()["memory_reserved_mb"] == 1792
    # The render's second container (same request) still fits beside it.
    second = await _create(runner, skill_dir, memory="4g", cpus=4.0)
    assert second.config.memory == "1792m"
    assert runner.memory_status()["memory_reserved_mb"] == 3584


@pytest.mark.asyncio
async def test_a_request_within_bounds_is_not_adjusted(budget, stub_sandboxes, skill_dir):
    runner = SkillRunner()
    created = await _create(runner, skill_dir, memory="1g", cpus=0.5)
    assert created.config.memory == "1g"
    assert created.config.cpus == 0.5
    assert created.warnings == []


# ── 5. override allowlist and bounds (derived from the model) ──


def test_overridable_and_runner_owned_fields_partition_the_model():
    fields = set(ContainerConfig.model_fields)
    assert set(OVERRIDABLE_FIELDS) | RUNNER_OWNED_FIELDS == fields
    assert not set(OVERRIDABLE_FIELDS) & RUNNER_OWNED_FIELDS
    # The security- and isolation-relevant fields are never the caller's.
    assert {"image", "cap_drop", "read_only_root", "volumes", "tmpfs", "service_scope"} <= RUNNER_OWNED_FIELDS


@pytest.mark.parametrize("field", sorted(RUNNER_OWNED_FIELDS))
def test_every_runner_owned_field_is_rejected_even_at_its_default(budget, field):
    runner = SkillRunner()
    value = ContainerConfig.model_fields[field].get_default(call_default_factory=True)
    with pytest.raises(ConfigOverrideRejected) as exc_info:
        runner._resolve_config(ContainerConfig(**{field: value}))
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "config_override_rejected"
    assert exc_info.value.extra["field"] == field
    assert exc_info.value.extra["reason"] == "not_overridable"


# field -> (a value above its bound, a value within it, the expected normalised value)
_BOUNDED = {
    "memory": ("9g", "2g", "2g"),
    "cpus": (8.5, 2.0, 2.0),
    "pids_limit": (1025, 512, 512),
    "workdir_tmpfs_size": ("5g", "2048m", "2g"),
    "exec_timeout": (1801, 1800, 1800),
    "install_timeout": (901, 600, 600),
}


def test_every_overridable_field_has_a_bound_test():
    # network is bounded by SANDBOX_ALLOWED_NETWORKS (security check), not a number.
    assert set(_BOUNDED) == set(OVERRIDABLE_FIELDS) - {"network"}


@pytest.mark.parametrize("field", sorted(_BOUNDED))
def test_every_overridable_field_is_bounded(budget, field):
    runner = SkillRunner()
    above, within, normalised = _BOUNDED[field]
    with pytest.raises(ConfigOverrideRejected) as exc_info:
        runner._resolve_config(ContainerConfig(**{field: above}))
    assert exc_info.value.extra == {
        "field": field,
        "reason": "above_maximum",
        "maximum": exc_info.value.extra["maximum"],
    }
    assert getattr(runner._resolve_config(ContainerConfig(**{field: within})), field) == normalised


@pytest.mark.parametrize("field", sorted(_BOUNDED))
@pytest.mark.parametrize("bad", [0, -1])
def test_a_zero_or_negative_value_never_drops_a_limit(budget, field, bad):
    runner = SkillRunner()
    value = f"{bad}m" if field in {"memory", "workdir_tmpfs_size"} else bad
    with pytest.raises(ConfigOverrideRejected) as exc_info:
        runner._resolve_config(ContainerConfig(**{field: value}))
    assert exc_info.value.extra["reason"] == "invalid_value"


def test_network_override_stays_within_the_allowlist(budget, monkeypatch):
    monkeypatch.setattr(budget, "SANDBOX_NETWORK", "manor-sandbox")
    monkeypatch.setattr(budget, "SANDBOX_ALLOWED_NETWORKS", ["manor-sandbox"])
    runner = SkillRunner()
    assert runner._resolve_config(ContainerConfig(network="none")).network == "none"
    with pytest.raises(SecurityError):
        runner._resolve_config(ContainerConfig(network="host"))


def test_omitted_fields_keep_the_runner_defaults(budget):
    runner = SkillRunner()
    cfg = runner._resolve_config(ContainerConfig(memory="1g"))
    assert cfg.memory == "1g"
    assert cfg.image == budget.SANDBOX_IMAGE
    assert cfg.cpus == budget.SANDBOX_CPUS
    # An empty override object changes nothing (it used to apply every model default).
    assert runner._resolve_config(ContainerConfig()) == runner._default_config()


@pytest.mark.parametrize(
    "config",
    [
        # Every config the control plane sends today (bash, workflow code,
        # video, Remotion plugin, generic): all must stay admissible.
        {"network": "none", "memory": "512m", "cpus": 1.0, "pids_limit": 128},
        {"memory": "512m", "cpus": 1.0, "pids_limit": 128},
        {"network": "none", "memory": "4g", "cpus": 4.0, "pids_limit": 512,
         "workdir_tmpfs_size": "2g", "exec_timeout": 1800},
        {"memory": "2g", "workdir_tmpfs_size": "1g", "pids_limit": 512},
    ],
)
def test_every_caller_config_in_the_codebase_is_accepted(budget, config):
    runner = SkillRunner()
    runner._resolve_config(ContainerConfig(**config))



def test_the_sdk_refuses_runner_owned_settings_before_sending():
    from packages.core.services.sandbox_sdk import client as sdk_client
    from packages.core.services.sandbox_sdk.models import (
        OVERRIDABLE_CONTAINER_FIELDS,
        RUNNER_OWNED_CONTAINER_FIELDS,
    )

    assert set(OVERRIDABLE_CONTAINER_FIELDS) == set(OVERRIDABLE_FIELDS)
    assert set(RUNNER_OWNED_CONTAINER_FIELDS) <= RUNNER_OWNED_FIELDS
    assert {"image", "workdir"} <= set(RUNNER_OWNED_CONTAINER_FIELDS)
    for field in RUNNER_OWNED_CONTAINER_FIELDS:
        with pytest.raises(ValueError, match=field):
            sdk_client._config_payload({"memory": "1g", field: "x"})
    assert sdk_client._config_payload({"memory": "1g", "cpus": None}) == {"memory": "1g"}

# ── 3. the HTTP contract ──


@pytest.fixture
def client(budget, monkeypatch, stub_sandboxes):
    service = load_service_main(monkeypatch, "_sandbox_service_main_memory_admission_test")
    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", lambda _image, **_kw: True)
    runner = SkillRunner()

    async def no_op():
        return None

    monkeypatch.setattr(runner, "startup", no_op)
    monkeypatch.setattr(runner, "shutdown", no_op)
    monkeypatch.setattr(service, "runner", runner)
    with TestClient(
        service.app,
        raise_server_exceptions=False,
        headers={"X-Manor-Sandbox-Token": API_TOKEN},
    ) as test_client:
        yield test_client, runner


def _create_body(**config):
    body = {"skill_name": "demo", "files": {"SKILL.md": "# Demo"}, "auto_install": False}
    if config:
        body["config_overrides"] = config
    return body


@pytest.mark.parametrize(
    "override,field",
    [({"image": "attacker/image:latest"}, "image"), ({"cap_drop": []}, "cap_drop"),
     ({"read_only_root": False}, "read_only_root"), ({"volumes": ["/:/host:rw"]}, "volumes")],
)
def test_forbidden_override_is_a_typed_400_over_http(client, stub_sandboxes, override, field):
    test_client, _runner = client
    response = test_client.post("/api/v1/sandbox/create-from-files", json=_create_body(**override))
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "config_override_rejected"
    assert body["field"] == field
    assert body["reason"] == "not_overridable"
    assert stub_sandboxes.instances == []


def test_runner_full_is_a_typed_429_with_memory_numbers_over_http(client):
    test_client, _runner = client
    for _ in range(2):
        ok = test_client.post("/api/v1/sandbox/create-from-files", json=_create_body(memory="1792m"))
        assert ok.status_code == 200, ok.text
        assert ok.json()["config"]["memory"] == "1792m"
    full = test_client.post("/api/v1/sandbox/create-from-files", json=_create_body(memory="1g"))
    assert full.status_code == 429, full.text
    body = full.json()
    assert body["code"] == "runner_full"
    assert body["reason"] == "memory"
    assert body["required_memory_mb"] == 1024
    assert body["available_memory_mb"] == 256
    assert body["largest_allocatable_mb"] == 256
    assert body["max_container_memory_mb"] == 1792


def test_health_reports_the_memory_budget(client):
    test_client, _runner = client
    test_client.post("/api/v1/sandbox/create-from-files", json=_create_body(memory="1g"))
    health = test_client.get("/health").json()
    assert health["host_memory_mb"] == 2048
    assert health["host_cpus"] == 1.0
    assert health["memory_budget_mb"] == 3840
    assert health["memory_reserved_mb"] == 1024
    assert health["memory_available_mb"] == 2816
    assert health["max_container_memory_mb"] == 1792
    assert health["largest_allocatable_mb"] == 1792
    assert health["container_defaults"]["memory_mb"] == 512
    assert health["container_defaults"]["network"] == budget_default_network()


def budget_default_network() -> str:
    return skill_runner_module.app_config.SANDBOX_NETWORK
