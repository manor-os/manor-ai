"""The Sandbox runner's error contract, health probe and structural guards.

4. Errors are typed and mapped in one place: every route answers the same
   status and machine-readable ``code`` for the same runner error, and a bare
   bug never masquerades as "sandbox gone".
5. /health never blocks the event loop on Docker.
Guards: every ``docker exec`` goes through the one liveness-checked path, and
no blocking Docker helper is called directly from service code.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.sandbox_runner_fakes import (
    API_TOKEN,
    SANDBOX_SERVICE_ROOT,
    FakeDocker,
    describe_app_routes,
    load_service_main,
    register_sandbox,
)

import sandbox.docker_backend as docker_backend  # noqa: E402
import sandbox.skill_runner as skill_runner_module  # noqa: E402
from api import routes  # noqa: E402
from sandbox.concurrency import SandboxConcurrencyExceeded  # noqa: E402
from sandbox.docker_backend import DockerCommandTimeout  # noqa: E402
from sandbox.errors import (  # noqa: E402
    CommandTimeout,
    ConfigOverrideRejected,
    DependencyInstallFailed,
    DockerUnavailable,
    ExecutionConflict,
    ExecutionNotFound,
    FileTooLarge,
    InvalidPath,
    InvalidRequest,
    RunnerFull,
    SandboxBusy,
    SandboxFileNotFound,
    SandboxGone,
    SandboxLost,
    SandboxUnavailable,
)
from sandbox.security import SecurityError  # noqa: E402
from sandbox.skill_runner import SkillRunner  # noqa: E402

_FILES = {"SKILL.md": "# Demo"}

# Every API route -> a valid request body. Guarded below against the app's
# routes, so a new route without an entry fails instead of going unchecked.
_ROUTE_BODIES: dict[tuple[str, str], dict | None] = {
    ("POST", "/api/v1/skill/scan"): {"skill_dir": "/tmp/skill"},
    ("POST", "/api/v1/sandbox/create"): {"skill_dir": "/tmp/skill"},
    ("POST", "/api/v1/sandbox/create-from-files"): {"skill_name": "demo", "files": _FILES},
    ("POST", "/api/v1/sandbox/create-from-builtin"): {"skill_name": "demo", "files": _FILES},
    ("POST", "/api/v1/sandbox/{sandbox_id}/load-skill"): {"skill_name": "demo", "files": _FILES},
    ("GET", "/api/v1/sandbox"): None,
    ("GET", "/api/v1/sandbox/{sandbox_id}"): None,
    ("POST", "/api/v1/sandbox/{sandbox_id}/touch"): None,
    ("DELETE", "/api/v1/sandbox/{sandbox_id}"): None,
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec"): {"command": "echo hi"},
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec/start"): {"command": "echo hi"},
    ("GET", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}"): None,
    ("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/cancel"): None,
    ("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/responses"): {
        "event_id": "ev-1",
        "payload": {},
    },
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/read"): {"path": "/skill/a.txt"},
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/read-base64"): {"path": "/skill/a.bin"},
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/tail"): {"path": "/work/.agent/run.log"},
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/write"): {"path": "/skill/a.txt", "content": "x"},
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/write-base64"): {
        "path": "/skill/a.bin",
        "content_base64": "eA==",
    },
    ("GET", "/api/v1/sandbox/{sandbox_id}/context"): None,
    ("POST", "/api/v1/skill/run"): {"skill_dir": "/tmp/skill", "commands": ["echo hi"]},
}

# Runner error -> (status, code). A bare KeyError/RuntimeError is a bug: it
# must surface as 500, never as a 404 the control plane reads as "reclaimed".
_ERROR_MATRIX = [
    (SandboxGone("Sandbox not found: sbx-1"), 404, "sandbox_not_found"),
    (SandboxLost("Sandbox sbx-1 is gone: container is not running"), 404, "sandbox_lost"),
    (ExecutionNotFound("Execution not found: e-1"), 404, "execution_not_found"),
    (SandboxFileNotFound("File not found in sandbox: a.txt"), 404, "file_not_found"),
    (SandboxBusy("Sandbox is already executing a command"), 409, "sandbox_busy"),
    (SandboxUnavailable("Sandbox unavailable (status=error)"), 409, "sandbox_unavailable"),
    (ExecutionConflict("execution_id already exists with a different request"), 409, "execution_conflict"),
    (SecurityError("Docker network 'host' is not allowed"), 403, "security_denied"),
    (SandboxConcurrencyExceeded("Max active sandbox limit reached (5)"), 429, "capacity_exceeded"),
    (RunnerFull("Sandbox runner memory is full", extra={"reason": "memory"}), 429, "runner_full"),
    (
        ConfigOverrideRejected("image is set by the runner", extra={"field": "image"}),
        400,
        "config_override_rejected",
    ),
    (InvalidRequest("Invalid skill_name '../x'"), 400, "invalid_request"),
    (InvalidPath("Cannot write /x: Read-only file system"), 400, "invalid_path"),
    (FileTooLarge("File too large: 9 bytes (max 1)"), 413, "file_too_large"),
    (DependencyInstallFailed("dependency install failed (exit 1)"), 422, "dependency_install_failed"),
    (
        CommandTimeout(
            "Command timed out after 5s",
            extra={"stdout": "partial out", "stderr": "partial err", "execution_id": "e-1"},
        ),
        504,
        "command_timeout",
    ),
    (DockerUnavailable("Cannot connect to the Docker daemon"), 503, "docker_unavailable"),
    (RuntimeError("boom"), 500, "internal_error"),
    (KeyError("a bug, not a missing sandbox"), 500, "internal_error"),
]


class _RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __getattr__(self, name):
        def _raise(*_args, **_kwargs):
            raise self._exc

        return _raise


def _api_routes(app) -> set[tuple[str, str]]:
    found = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1/"):
            continue
        for method in getattr(route, "methods", set()) or set():
            if method not in {"HEAD", "OPTIONS"}:
                found.add((method, path))
    return found


@pytest.fixture
def service(monkeypatch):
    """The runner app, wired to a freshly imported ``api`` package.

    Earlier tests in a regression shard can leave a different or emptied
    ``api.routes`` module behind (CI saw an app with no /api/v1 routes), so
    this contract never reads routes through whatever happens to be cached:
    the ``api`` modules are dropped for this test (and restored after it), and
    ``service.routes`` is the exact routes module the app was built from.
    """
    for name in [name for name in sys.modules if name == "api" or name.startswith("api.")]:
        monkeypatch.delitem(sys.modules, name)
    main = load_service_main(monkeypatch, "_sandbox_service_main_error_contract_test")
    main.routes = sys.modules["api.routes"]
    assert main.routes.__file__.startswith(str(SANDBOX_SERVICE_ROOT)), main.routes.__file__
    return main


def test_route_body_table_covers_every_api_route(service):
    assert _api_routes(service.app) == set(_ROUTE_BODIES), describe_app_routes(service.app)


@pytest.mark.parametrize(
    "exc,status,code", _ERROR_MATRIX, ids=[type(item[0]).__name__ for item in _ERROR_MATRIX]
)
def test_every_route_maps_every_runner_error_the_same_way(service, monkeypatch, exc, status, code):
    """One central mapping: no route may translate (or lose) a runner error itself."""
    monkeypatch.setattr(service.routes, "_runner", _RaisingRunner(exc))
    client = TestClient(
        service.app,
        raise_server_exceptions=False,
        headers={"X-Manor-Sandbox-Token": API_TOKEN},
    )
    for (method, template), body in sorted(_ROUTE_BODIES.items(), key=lambda item: item[0]):
        path = template.format(sandbox_id="sbx-1", execution_id="e-1")
        response = client.request(method, path, json=body)
        assert response.status_code == status, (method, template, response.text)
        payload = response.json()
        assert payload["code"] == code, (method, template, payload)
        assert isinstance(payload["detail"], str) and payload["detail"]
        if status == 504:
            assert payload["stdout"] == "partial out"
            assert payload["stderr"] == "partial err"


def test_request_validation_and_unknown_routes_carry_a_code(service):
    client = TestClient(service.app, headers={"X-Manor-Sandbox-Token": API_TOKEN})
    invalid = client.post("/api/v1/sandbox/sbx-1/exec", json={"timeout": 5})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_request"
    unknown = client.get("/api/v1/no-such-route")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "not_found"




# ── the real runner behind the API ──


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(docker_backend, "_run_docker", fake)
    monkeypatch.setattr(docker_backend, "_CANCEL_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(docker_backend, "_CANCEL_POLL_INTERVAL_SECONDS", 0.005)
    return fake


@pytest.fixture
def live_client(service, monkeypatch, docker):
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
    ) as client:
        sandbox, _lease = client.portal.call(register_sandbox, runner)
        yield client, runner, sandbox


@pytest.mark.parametrize("skill_name", ["../escape", "a/b", "a\\b", "..", ""])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/sandbox/create-from-files",
        "/api/v1/sandbox/create-from-builtin",
        "/api/v1/sandbox/sbx-1/load-skill",
    ],
)
def test_skill_names_that_address_paths_are_rejected_as_400(live_client, docker, path, skill_name):
    client, runner, sandbox = live_client
    response = client.post(path, json={"skill_name": skill_name, "files": _FILES})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"
    assert sandbox.status.value == "ready"
    assert not any(call[1] in {"create", "exec"} for call in docker.calls)


def test_synchronous_exec_timeout_is_504_with_partial_output_and_kills_the_process(live_client, docker):
    client, runner, sandbox = live_client
    docker.launcher_timeout = DockerCommandTimeout(5, b"partial out", b"partial err")

    response = client.post(
        "/api/v1/sandbox/sbx-1/exec",
        json={"command": "python slow.py", "timeout": 5, "execution_id": "sync-t"},
    )

    assert response.status_code == 504, response.text
    body = response.json()
    assert body["code"] == "command_timeout"
    assert body["detail"] == "Command timed out after 5s"
    assert body["stdout"] == "partial out"
    assert body["stderr"] == "partial err"
    assert body["execution_id"] == "sync-t"
    assert "python" not in body["detail"] and "launcher" not in body["detail"]
    assert docker.signals == ["kill -TERM -321"], "the timed-out process group is killed"
    assert sandbox.status.value == "ready"


def test_oversized_binary_read_is_413(live_client):
    client, _runner, _sandbox = live_client
    response = client.post(
        "/api/v1/sandbox/sbx-1/files/read-base64", json={"path": "/skill/a.bin", "max_size": 1}
    )
    assert response.status_code == 413, response.text
    assert response.json()["code"] == "file_too_large"


def test_busy_sandbox_answers_409_over_http(live_client):
    client, _runner, sandbox = live_client
    # Agents of a conversation share the sandbox: busy means every command
    # slot is taken.
    for index in range(sandbox.max_concurrent_executions):
        sandbox.claim_command(f"other-{index}", "python long.py", 60)
    response = client.post("/api/v1/sandbox/sbx-1/exec", json={"command": "echo hi"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "sandbox_busy"
    # The side channel still works next to the running command.
    read = client.post("/api/v1/sandbox/sbx-1/files/read", json={"path": "/skill/a.txt"})
    assert read.status_code == 200, read.text


def test_cancel_of_an_unknown_execution_is_404_execution_not_found(live_client):
    client, _runner, _sandbox = live_client
    response = client.post("/api/v1/sandbox/sbx-1/executions/never-ran/cancel")
    assert response.status_code == 404
    assert response.json()["code"] == "execution_not_found"


# ── 5. /health never blocks the event loop ──


@pytest.mark.asyncio
async def test_image_probe_runs_off_the_event_loop_with_a_short_timeout_and_is_cached(monkeypatch):
    probes: list[dict] = []
    main_thread = threading.get_ident()

    def slow_probe(image, **kwargs):
        probes.append({"thread": threading.get_ident(), **kwargs})
        time.sleep(0.3)
        return True

    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", slow_probe)
    monkeypatch.setattr(skill_runner_module, "_load_baked_skill_index", lambda *_a, **_kw: None)
    runner = SkillRunner()
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticking = asyncio.create_task(ticker())
    await runner.refresh_image_status()
    ticking.cancel()

    assert ticks >= 10, "the event loop kept running while Docker was probed"
    assert probes[0]["thread"] != main_thread
    assert 0 < probes[0]["timeout"] <= 10
    assert runner.runtime_status()["sandbox_image_available"] is True

    await runner.refresh_image_status()
    assert len(probes) == 1, "a fresh answer is served from the cache"

    # A stale answer is served at once while one shared refresh runs.
    runner._image_status_at -= 3600
    started = time.monotonic()
    await asyncio.gather(*(runner.refresh_image_status() for _ in range(5)))
    assert time.monotonic() - started < 0.2
    await runner._image_probe
    assert len(probes) == 2


def test_health_endpoint_uses_the_cached_probe(service, monkeypatch):
    probes = []

    def probe(image, **kwargs):
        probes.append(image)
        return False

    monkeypatch.setattr(skill_runner_module, "_image_exists_locally", probe)
    monkeypatch.setattr(service, "runner", SkillRunner())
    # No lifespan: /health must answer without the runner's Docker startup.
    client = TestClient(service.app)
    first = client.get("/health")
    second = client.get("/health")
    assert first.status_code == second.status_code == 200
    assert first.json()["sandbox_image_available"] is False
    assert len(probes) == 1


# ── structural guards ──


_BLOCKING_DOCKER_HELPERS = {
    "_run_docker",
    "_container_state",
    "_image_exists_locally",
    "_tar_directory",
    "docker_host_resources",
    "list_managed_sandbox_containers",
    "list_running_managed_container_names",
    "remove_managed_sandbox_container",
}


def _direct_calls(tree: ast.AST, names: set[str]):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in names:
                yield node


def test_service_code_never_calls_a_blocking_docker_helper_on_the_event_loop():
    """Guard the class behind the /health incident: outside docker_backend's
    own thread-side helpers, a blocking Docker helper may only be handed to
    ``asyncio.to_thread``, never called."""
    offenders = []
    for path in sorted(SANDBOX_SERVICE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path.name == "docker_backend.py":
            # Module-level helpers run in threads by contract; inside the
            # DockerSandbox class only nested (thread-body) functions may call them.
            sandbox_class = next(
                node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DockerSandbox"
            )
            for method in sandbox_class.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                nested = {
                    id(call)
                    for inner in ast.walk(method)
                    if isinstance(inner, ast.FunctionDef) and inner is not method
                    for call in _direct_calls(inner, _BLOCKING_DOCKER_HELPERS)
                }
                for call in _direct_calls(method, _BLOCKING_DOCKER_HELPERS):
                    if id(call) not in nested:
                        offenders.append(f"{path.name}:{call.lineno} DockerSandbox.{method.name}")
            continue
        for call in _direct_calls(tree, _BLOCKING_DOCKER_HELPERS | {"run", "Popen"}):
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr in {"run", "Popen"}:
                if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    continue
            offenders.append(f"{path.relative_to(SANDBOX_SERVICE_ROOT)}:{call.lineno}")
    assert offenders == []


# The only DockerSandbox methods allowed to reach a raw docker helper, and
# which helpers: none of them runs ``docker exec`` except ``_docker`` itself.
_RAW_DOCKER_METHODS = {
    "_docker": {"_run_docker"},  # the one exec path
    "_raise_if_container_gone": {"_container_state"},
    "verify_alive": {"_container_state"},
    "destroy": {"_run_docker", "_container_state"},  # rm -f, inspect
    "_ensure_container": {"_run_docker", "_container_state", "_image_exists_locally"},
    "_inject_files": {"_tar_directory"},  # host-side archive of the skill files
}


def test_every_docker_exec_goes_through_the_liveness_checked_path():
    """A ``docker exec`` that bypasses ``DockerSandbox._docker`` would report a
    dead container as a command exit code again. Only the allow-listed
    methods may reference the raw docker helpers, and the file bridge none."""
    source = (SANDBOX_SERVICE_ROOT / "sandbox" / "docker_backend.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sandbox_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DockerSandbox"
    )
    violations = []
    for method in sandbox_class.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        allowed = _RAW_DOCKER_METHODS.get(method.name, set())
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Name)
                and node.id in _BLOCKING_DOCKER_HELPERS
                and node.id not in allowed
            ):
                violations.append(f"DockerSandbox.{method.name} uses {node.id} (line {node.lineno})")
    assert violations == []

    bridge = (SANDBOX_SERVICE_ROOT / "sandbox" / "fs_bridge.py").read_text(encoding="utf-8")
    bridge_tree = ast.parse(bridge)
    referenced = {
        node.id for node in ast.walk(bridge_tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(bridge_tree) if isinstance(node, ast.Attribute)}
    assert not referenced & (_BLOCKING_DOCKER_HELPERS | {"subprocess", "exec"}), (
        "the file bridge must use side_exec only"
    )


def test_guards_scan_real_files():
    assert (SANDBOX_SERVICE_ROOT / "sandbox" / "docker_backend.py").is_file()
    assert Path(skill_runner_module.__file__).resolve().is_relative_to(SANDBOX_SERVICE_ROOT.resolve())
