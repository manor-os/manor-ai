"""The control plane against OLDER sandbox runners.

Incident (staging, 2026-10-18): staging's control plane ran the per-user
sandbox stack against a runner built 2026-08-17. Every ``sandbox`` exec of an
agent without a Skill was given its work-area workdir
(``/workspace/c/<work>/agents/<agent>``), which only a current runner
creates; the old one passed it to ``docker exec -w`` and every command failed
with ``chdir to cwd ... no such file or directory``.

Every release has a window in which the control plane is newer than its
runners, so this is a class: every new-runner behaviour is gated on a
capability the runner reports in /health (``sandbox_sdk.capabilities``),
falling back to what an old runner did -- or refusing, naming the runner as
outdated, where it cannot work there (or would silently do something unsafe).

Pinned here:

* the class guard: every route and request field the runner gained since the
  oldest runner in service, and every one the SDK sends, is listed against a
  capability; an old runner never receives a field or route it does not know;
  the current runner reports every capability;
* the request gate's policies (omit vs refuse);
* the sandbox tool on an old runner (the incident), including the credential
  boundary it cannot keep and background commands it cannot run.

Bash, the session service and external agents keep their old-runner cases in
their own suites (``test_bash_in_sandbox``, ``test_sandbox_session_service``,
``test_external_agent_delegation``).
"""

from __future__ import annotations

import json

import httpx
import pytest

from packages.core.services.sandbox_sdk import SandboxClient, SandboxRunnerOutdatedError
from packages.core.services.sandbox_sdk import capabilities as caps
from packages.core.services.sandbox_sdk.models import OVERRIDABLE_CONTAINER_FIELDS
from tests.sandbox_exec_fakes import AUGUST_RUNNER_HEALTH, CURRENT_RUNNER_HEALTH, SEPTEMBER_RUNNER_HEALTH
from tests.test_sandbox_binding_scope import (  # noqa: F401 - fixtures
    _area,
    _create_generic,
    _exec,
    _placements,
    exec_client,
    fake_cache,
    local_sandbox_settings,
)

AUGUST = caps.capabilities_from_health(AUGUST_RUNNER_HEALTH)
SEPTEMBER = caps.capabilities_from_health(SEPTEMBER_RUNNER_HEALTH)
CURRENT = caps.capabilities_from_health(CURRENT_RUNNER_HEALTH)
WORK_AREA = "/workspace/c/01M58GJDSJBYKBQ06TDAG91971/agents/main"


# ── the class guard ─────────────────────────────────────────────────────────


def _runner_routes(app) -> set[tuple[str, str]]:
    from fastapi.routing import APIRoute

    return {(method, route.path) for route in app.routes if isinstance(route, APIRoute) for method in route.methods}


def test_every_runner_route_and_request_field_newer_than_the_oldest_runner_is_gated(monkeypatch) -> None:
    from tests.sandbox_runner_fakes import load_service_main

    import sandbox.models as runner_models

    main = load_service_main(monkeypatch, "sandbox_main_runner_compat")
    new_routes = _runner_routes(main.app) - caps.BASELINE_RUNNER["routes"]
    ungated = sorted(r for r in new_routes if r not in caps.ROUTE_GATES and r not in caps.RUNNER_ONLY_ROUTES)
    assert not ungated, f"runner routes without a capability gate: {ungated}"

    ungated_fields = []
    for model, baseline in caps.BASELINE_RUNNER["fields"].items():
        if model == "ContainerConfig":
            continue
        new_fields = set(getattr(runner_models, model).model_fields) - baseline
        for route in caps.REQUEST_MODEL_ROUTES[model]:
            gates = caps.REQUEST_GATES.get(route, {})
            ungated_fields += [(model, route[1], name) for name in sorted(new_fields) if name not in gates]
    assert not ungated_fields, f"runner request fields without a capability gate: {ungated_fields}"
    # A caller may only override container settings every runner knows.
    assert set(OVERRIDABLE_CONTAINER_FIELDS) <= caps.BASELINE_RUNNER["fields"]["ContainerConfig"]


_RESPONSE = {
    "sandbox_id": "SBX", "container_name": "c", "status": "completed", "workdir": "/skill",
    "skill": {"name": "s", "skill_dir": "/skill"}, "skill_name": "s", "created_at": 1.0, "last_used_at": 1.0,
    "stdout": "", "stderr": "", "exit_code": 0, "execution_id": "EXE", "cancelled": True,
    "event_id": "ev", "accepted": True, "path": "/tmp/f", "content": "", "size": 0, "truncated": False,
    "content_base64": "", "data_base64": "", "next_offset": 0, "eof": True, "written": 0,
}


def _recording_client(capabilities: caps.RunnerCapabilities | None):
    sent: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.replace("/SBX", "/{sandbox_id}").replace("/EXE", "/{execution_id}")
        sent.append((request.method, path, body))
        if (request.method, path) == ("GET", "/api/v1/sandbox"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_RESPONSE)

    client = SandboxClient("http://runner", transport=httpx.MockTransport(handler))
    client.capabilities = capabilities
    return client, sent


# Every SDK request, with every option it can send set to a value that is sent.
_REQUESTS = {
    "create_from_files": lambda c: c.create_from_files(
        "s", {"a": "b"}, env={"K": "v"}, allowed_sensitive_keys=["K"], config={"memory": "1g"},
        auto_install=False, idempotency_key="i",
    ),
    "create_from_builtin": lambda c: c.create_from_builtin(
        "s", {"a": "b"}, env={"K": "v"}, allowed_sensitive_keys=["K"], config={"memory": "1g"},
        auto_install=False, idempotency_key="i", baked_skill={"name": "s", "digest": "0" * 64},
    ),
    "load_skill": lambda c: c.load_skill(
        "SBX", "s", {"a": "b"}, auto_install=False, idle_threshold=0, source="builtin", env={"K": "v"},
        allowed_sensitive_keys=["K"], baked_skill={"name": "s", "digest": "0" * 64},
    ),
    "list": lambda c: c.list(),
    "status": lambda c: c.status("SBX"),
    "touch": lambda c: c.touch("SBX"),
    "destroy": lambda c: c.destroy("SBX"),
    "exec": lambda c: c.exec(
        "SBX", "true", timeout=2400, workdir=WORK_AREA, execution_id="EXE", skill_env=False, skill="s",
        fair_key="conv", queue_timeout=5, env={"T": "x"}, side_channel=True,
    ),
    "start_exec": lambda c: c.start_exec(
        "SBX", "true", timeout=2400, workdir=WORK_AREA, execution_id="EXE", skill_env=False, skill="s",
        fair_key="conv", queue_timeout=5, env={"T": "x"},
    ),
    "execution_status": lambda c: c.execution_status("SBX", "EXE", after_sequence=1, wait_seconds=1),
    "cancel_execution": lambda c: c.cancel_execution("SBX", "EXE"),
    "send_execution_response": lambda c: c.send_execution_response("SBX", "EXE", "ev", payload={}, message="m"),
    "read_file": lambda c: c.read_file("SBX", f"{WORK_AREA}/f", skill="s"),
    "read_file_base64": lambda c: c.read_file_base64("SBX", f"{WORK_AREA}/f", skill="s"),
    "tail_file": lambda c: c.tail_file("SBX", "/tmp/f", offset=1),
    "write_file": lambda c: c.write_file("SBX", f"{WORK_AREA}/f", "x", skill="s"),
    "write_file_base64": lambda c: c.write_file_base64("SBX", f"{WORK_AREA}/f", "eA==", skill="s"),
    "get_context": lambda c: c.get_context("SBX"),
}


def _baseline_fields(route: tuple[str, str]) -> frozenset[str] | None:
    for model, routes in caps.REQUEST_MODEL_ROUTES.items():
        if route in routes:
            return caps.BASELINE_RUNNER["fields"][model]
    return None


@pytest.mark.asyncio
async def test_every_route_and_field_the_sdk_sends_is_known_to_the_oldest_runner_or_gated() -> None:
    # The current runner: every field is sent (an unknown one would be refused).
    client, sent = _recording_client(CURRENT)
    for request in _REQUESTS.values():
        await request(client)
    await client.close()

    unknown = []
    for method, path, body in sent:
        route = (method, path)
        if route not in caps.BASELINE_RUNNER["routes"] and route not in caps.ROUTE_GATES:
            unknown.append((method, path, "route"))
        baseline = _baseline_fields(route)
        if baseline is None or not body:
            continue
        allowed = baseline | set(caps.REQUEST_GATES.get(route, {})) | caps.IGNORED_BY_EVERY_RUNNER.get(route, set())
        unknown += [(method, path, name) for name in sorted(set(body) - allowed)]
    assert not unknown, f"sent to runners without a capability gate: {unknown}"


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities", [AUGUST, SEPTEMBER], ids=["august", "september"])
async def test_an_old_runner_never_receives_a_route_or_field_it_does_not_know(capabilities) -> None:
    client, sent = _recording_client(capabilities)
    refused = []
    for name, request in _REQUESTS.items():
        try:
            await request(client)
        except SandboxRunnerOutdatedError as exc:
            assert "outdated and must be upgraded" in str(exc)
            refused.append(name)
    await client.close()

    leaked = []
    for method, path, body in sent:
        route = (method, path)
        gate = caps.ROUTE_GATES.get(route)
        if route not in caps.BASELINE_RUNNER["routes"] and not (gate and capabilities.has(gate)):
            leaked.append((method, path, "route"))
        baseline = _baseline_fields(route)
        if baseline is not None and body:
            gates = caps.REQUEST_GATES.get(route, {})
            leaked += [
                (method, path, name) for name in body
                if name not in baseline | caps.IGNORED_BY_EVERY_RUNNER.get(route, set())
                and not capabilities.has(gates[name].capability)
            ]
    assert not leaked, leaked
    # These need what the runner lacks; everything else still works.
    expected_refused = {"exec", "start_exec", "load_skill", "create_from_builtin", "read_file", "tail_file"}
    assert expected_refused <= set(refused)
    assert {"list", "status", "destroy", "cancel_execution", "create_from_files"}.isdisjoint(refused)


@pytest.mark.asyncio
async def test_the_request_gate_omits_what_an_old_runner_does_anyway_and_refuses_the_rest() -> None:
    client, sent = _recording_client(AUGUST)
    # No queue and one /skill: omitted, the old default is the same thing.
    await client.exec("SBX", "true", execution_id="EXE", skill="pptx", fair_key="conv", queue_timeout=60)
    assert sent[-1][2] == {"command": "true", "timeout": 60, "execution_id": "EXE"}
    # Things an old runner would silently get wrong: refused, nothing sent.
    for kwargs in (
        {"workdir": WORK_AREA},  # the incident: a work area it never created
        {"skill_env": False},  # it would hand the Skill's credentials over
        {"timeout": 2400},  # it answers 422 above 1800 s
        {"env": {"MANOR_CLI_TOKEN": "t"}},  # it would drop the command's credentials
    ):
        before = len(sent)
        with pytest.raises(SandboxRunnerOutdatedError):
            await client.exec("SBX", "true", **kwargs)
        assert len(sent) == before, kwargs
    with pytest.raises(SandboxRunnerOutdatedError):
        await client.start_exec("SBX", "true")
    # Values every runner knows pass unchanged.
    await client.exec("SBX", "true", workdir="/tmp", skill_env=True, timeout=1800)
    assert sent[-1][2] == {"command": "true", "timeout": 1800, "workdir": "/tmp", "skill_env": True}
    await client.close()

    # A current runner, or one whose /health could not be read: sent as asked.
    for capabilities in (CURRENT, caps.UNKNOWN):
        client, sent = _recording_client(capabilities)
        await client.exec("SBX", "true", workdir=WORK_AREA, skill_env=False, skill="", fair_key="c", queue_timeout=5)
        assert sent[-1][2]["workdir"] == WORK_AREA and sent[-1][2]["skill_env"] is False
        await client.close()


@pytest.mark.asyncio
async def test_the_current_runner_reports_every_capability(monkeypatch) -> None:
    from tests.sandbox_inprocess_runner import install_inprocess_runner

    install_inprocess_runner(monkeypatch, "sandbox_main_runner_compat_health")
    import packages.core.services.sandbox_sdk as sandbox_sdk

    client = sandbox_sdk.SandboxClient()
    try:
        health = await client.health()
    finally:
        await client.close()
    assert {name: health.get(name) for name in caps.capability_flags} == caps.capability_flags
    assert caps.capabilities_from_health(health).names == set(caps.CAPABILITIES)


# ── the sandbox tool on an old runner ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "placement"),
    [
        (AUGUST_RUNNER_HEALTH, {}),
        (SEPTEMBER_RUNNER_HEALTH, {}),
        (CURRENT_RUNNER_HEALTH, _area("main")),
    ],
    ids=["august", "september", "current"],
)
async def test_sandbox_exec_runs_in_the_runners_default_workdir_on_an_old_runner(
    fake_cache, exec_client, local_sandbox_settings, health, placement,
) -> None:
    exec_client.health_payload = dict(health)
    sandbox_id = await _create_generic("main")

    result = await _exec("main", sandbox_id)

    assert isinstance(result, str) and "[exit_code: 0]" in result, result
    assert _placements(exec_client) == [(sandbox_id, placement)]


@pytest.mark.asyncio
async def test_an_old_runner_cannot_keep_a_skills_credentials_from_another_agent(
    fake_cache, exec_client, local_sandbox_settings,
) -> None:
    exec_client.health_payload = dict(SEPTEMBER_RUNNER_HEALTH)
    sandbox_id = await _create_generic("main")
    registry = next(key for key, value in fake_cache.store.items()
                    if isinstance(value, dict) and value.get("sandbox_id") == sandbox_id and "base_url" in value)
    fake_cache.store[registry] = {**fake_cache.store[registry], "holds_credentials": True}

    refused = await _exec("helper", sandbox_id)

    assert refused["error"]["code"] == "sandbox_runner_outdated", refused
    assert _placements(exec_client) == [], "nothing ran"


@pytest.mark.asyncio
async def test_a_background_command_is_refused_on_a_runner_without_started_commands(
    fake_cache, exec_client, local_sandbox_settings,
) -> None:
    from packages.core.ai.tools import sandbox_tools

    exec_client.health_payload = dict(AUGUST_RUNNER_HEALTH)
    sandbox_id = await _create_generic("main")

    result = json.loads(await sandbox_tools._sandbox_exec(
        entity_id="ent_1", user_id="user_1", sandbox_id=sandbox_id, command="sleep 100", background=True,
        conversation_id="01M3RHSDAH4WCT68JGXPE3EJ19", _agent_id_from_context="main",
    ))

    assert result["error"]["code"] == "sandbox_runner_outdated"
    assert "background=true" in result["error"]["message"]
    assert _placements(exec_client) == []


