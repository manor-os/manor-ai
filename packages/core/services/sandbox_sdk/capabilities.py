"""What a sandbox runner can do, read once from its ``/health`` report.

The control plane is released together with the runner it is built for, but a
runner host upgrades on its own schedule: every release has a window in which
the control plane is newer than its runners, and a host that does not follow
a release channel can lag indefinitely. Every runner behaviour or API newer
than the oldest runner still in service is therefore gated on a capability
the runner reports, and the caller falls back to the behaviour that runner
had -- or, when a feature cannot work there at all, fails early with a
message naming the runner as outdated.

This module is the single table of those capabilities and of the request
fields and routes they gate. ``tests/test_sandbox_runner_compat.py`` holds
it against both sides: every request field or route the runner gained since
``BASELINE_RUNNER`` and every one the SDK sends must be listed here, and the
current runner must report every capability.

An old runner silently ignores an unknown request field (pydantic's default),
so an ungated field does not fail -- it changes what the command does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# ── capabilities ────────────────────────────────────────────────────────────

#: POST /exec/start, GET /executions/{id}, POST /executions/{id}/responses
#: (a command started without waiting for it).
EXEC_START = "exec_start"
#: Skill env handed to each exec (and replaced by load_skill's ``env``)
#: instead of baked into the container at create.
ENV_PER_EXEC = "env_per_exec"
#: POST /files/tail.
FILES_TAIL = "files_tail"
#: Exec timeouts up to 3600 s (older runners reject more than 1800 with 422).
LONG_EXEC_TIMEOUT = "long_exec_timeout"
# (Host memory admission needs no flag: its consumers read the numbers
# themselves -- ``max_container_memory_mb`` etc. -- and place by slots alone
# when a runner does not report them.)
#: Skills side by side in /skills/<slot>; each exec and file operation picks
#: its /skill view (``skill``) and whether it gets that Skill's env
#: (``skill_env``).
SKILL_SLOTS = "skill_slots"
#: The shared /workspace tmpfs with per-work-area directories
#: (``/workspace/c/<area>/agents/<agent>``) created on demand.
WORK_AREAS = "work_areas"
#: A fair per-sandbox FIFO: ``fair_key`` / ``queue_timeout`` on an exec.
EXEC_QUEUE = "exec_queue"
#: ``env`` on a synchronous exec (this command only).
EXEC_ENV = "exec_env"
#: ``env`` on a started exec (this command only).
EXEC_START_ENV = "exec_start_env"
#: ``side_channel`` execs: the platform's own bookkeeping next to the slots,
#: never with the Skill env.
EXEC_SIDE_CHANNEL = "exec_side_channel"
#: Built-in Skills baked into the image (``baked_skill`` on create/load).
BAKED_SKILLS = "baked_skills"


def _flag(health: Mapping[str, Any], name: str) -> bool:
    return health.get(name) is True


def _slots(health: Mapping[str, Any]) -> bool:
    return str(health.get("skill_layout") or "") == "slots"


#: capability -> (what it is, in the outdated-runner message; predicate over
#: the runner's /health report). A predicate accepts the capability's own flag
#: (``capability_flags`` -- what the current runner reports) and, for a runner
#: built before that flag existed, the oldest report that implies it: such a
#: runner is treated as lacking it, never as having one it lacks.
CAPABILITIES: dict[str, tuple[str, Callable[[Mapping[str, Any]], bool]]] = {
    EXEC_START: (
        "starting a command without waiting for it",
        # Added 2026-09-01; env_injection is the first report that implies it.
        lambda h: _flag(h, EXEC_START) or "env_injection" in h,
    ),
    ENV_PER_EXEC: (
        "per-command Skill credentials",
        lambda h: _flag(h, ENV_PER_EXEC) or str(h.get("env_injection") or "") == "exec",
    ),
    FILES_TAIL: (
        "reading a growing file by offset",
        lambda h: _flag(h, FILES_TAIL) or _slots(h),
    ),
    LONG_EXEC_TIMEOUT: (
        "commands longer than 30 minutes",
        lambda h: _flag(h, LONG_EXEC_TIMEOUT) or _slots(h),
    ),
    SKILL_SLOTS: (
        "Skills side by side, each command with its own Skill view and credentials",
        _slots,
    ),
    WORK_AREAS: (
        "per-conversation work areas in /workspace",
        lambda h: _flag(h, WORK_AREAS) or _slots(h),
    ),
    EXEC_QUEUE: ("a fair command queue", lambda h: _flag(h, EXEC_QUEUE)),
    EXEC_ENV: ("per-command env", lambda h: _flag(h, EXEC_ENV)),
    EXEC_START_ENV: ("per-command env on a started command", lambda h: _flag(h, EXEC_START_ENV)),
    EXEC_SIDE_CHANNEL: ("side-channel bookkeeping commands", lambda h: _flag(h, EXEC_SIDE_CHANNEL)),
    BAKED_SKILLS: (
        "built-in Skills baked into the image",
        lambda h: isinstance(h.get(BAKED_SKILLS), dict),
    ),
}

#: The /health keys a current runner reports so that it is recognised as
#: having every capability (sandbox-service/main.py ``health``).
capability_flags: dict[str, Any] = {
    EXEC_START: True,
    ENV_PER_EXEC: True,
    FILES_TAIL: True,
    LONG_EXEC_TIMEOUT: True,
    WORK_AREAS: True,
    EXEC_QUEUE: True,
    EXEC_ENV: True,
    EXEC_START_ENV: True,
    EXEC_SIDE_CHANNEL: True,
}

#: An exec timeout above this needs LONG_EXEC_TIMEOUT.
LEGACY_MAX_EXEC_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class RunnerCapabilities:
    """One runner's capabilities. ``reported`` False: its /health could not be read."""

    names: frozenset[str] = frozenset()
    reported: bool = False
    build_version: str = ""
    health: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def has(self, capability: str) -> bool:
        return capability in self.names

    def lacks(self, capability: str) -> bool:
        """Known NOT to have it: the runner answered and did not report it.

        An unreachable runner is not called outdated: the request itself
        fails with the runner's real (transport) error.
        """

        return self.reported and capability not in self.names

    def missing(self, *capabilities: str) -> list[str]:
        return [name for name in capabilities if self.lacks(name)]


UNKNOWN = RunnerCapabilities()


def capabilities_from_health(health: Mapping[str, Any] | None) -> RunnerCapabilities:
    if not isinstance(health, Mapping):
        return UNKNOWN
    names = frozenset(name for name, (_, probe) in CAPABILITIES.items() if probe(health))
    return RunnerCapabilities(
        names=names,
        reported=True,
        build_version=str(health.get("build_version") or ""),
        health=dict(health),
    )


def describe(capability: str) -> str:
    return CAPABILITIES[capability][0] if capability in CAPABILITIES else capability


def outdated_message(missing: list[str] | tuple[str, ...], *, build_version: str = "", what: str = "") -> str:
    """The one actionable text every outdated-runner failure carries."""

    needs = ", ".join(describe(name) for name in missing)
    build = f" (build {build_version})" if build_version else ""
    subject = f"{what} needs" if what else "This needs"
    return (
        f"The sandbox runner{build} is outdated and must be upgraded: {subject} "
        f"{needs}, which it does not support. Nothing was run. Ask an operator to "
        "upgrade the sandbox runner to the current release."
    )


# ── what each capability gates ──────────────────────────────────────────────

#: Gate policies.
#: ``omit``: an old runner's default IS the pre-capability behaviour, so the
#:   field is simply not sent to it.
#: ``refuse``: sending it to an old runner would silently do something
#:   different (and possibly unsafe) -- the request is refused instead with
#:   the outdated-runner message. Callers that have a fallback decide it
#:   before they build the request.
OMIT = "omit"
REFUSE = "refuse"


@dataclass(frozen=True)
class Gate:
    capability: str
    policy: str
    #: The field's value needs the capability (None: any value that is sent).
    applies: Callable[[Any], bool] | None = None
    #: Refused on a runner whose capabilities are unknown too: a runner that
    #: turned out to lack it would do something unsafe, so it needs proof.
    requires_proof: bool = False

    def needed_for(self, value: Any) -> bool:
        return True if self.applies is None else bool(self.applies(value))


def _beyond_legacy_timeout(value: Any) -> bool:
    try:
        return int(value) > LEGACY_MAX_EXEC_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return False


def _work_area_path(value: Any) -> bool:
    return str(value or "").startswith("/workspace/")


def _nonempty(value: Any) -> bool:
    return bool(value)


_EXEC_FIELDS: dict[str, Gate] = {
    # Present on BASELINE_RUNNER, but only some values work there.
    "timeout": Gate(LONG_EXEC_TIMEOUT, REFUSE, _beyond_legacy_timeout),
    "workdir": Gate(WORK_AREAS, REFUSE, _work_area_path),
    # skill_env=False keeps a Skill's credentials from an agent that did not
    # invoke it; an old runner would hand them over.
    "skill_env": Gate(SKILL_SLOTS, REFUSE, lambda value: value is False),
    # Without slots there is one /skill: the most recent Skill's, which is
    # what an omitted view means.
    "skill": Gate(SKILL_SLOTS, OMIT),
    # No queue: a full sandbox answers sandbox_busy at once, as before.
    "fair_key": Gate(EXEC_QUEUE, OMIT),
    "queue_timeout": Gate(EXEC_QUEUE, OMIT),
    # The side channel promises no Skill env and no slot; an old runner would
    # run it as an ordinary command with the Skill env.
    "side_channel": Gate(EXEC_SIDE_CHANNEL, REFUSE, _nonempty),
}

_START_FIELDS: dict[str, Gate] = {
    **_EXEC_FIELDS,
    "env": Gate(EXEC_START_ENV, REFUSE, _nonempty),
}

_FILE_FIELDS: dict[str, Gate] = {
    "skill": Gate(SKILL_SLOTS, OMIT),
    "path": Gate(WORK_AREAS, REFUSE, _work_area_path),
}


_CREATE_FIELDS: dict[str, Gate] = {
    "baked_skill": Gate(BAKED_SKILLS, REFUSE, _nonempty),
}

_LOAD_FIELDS: dict[str, Gate] = {
    "baked_skill": Gate(BAKED_SKILLS, REFUSE, _nonempty),
    # A runner that bakes env at create cannot replace it: a credentialed
    # load is refused (the session service never asks for one there); an
    # empty env is what that runner did anyway.
    "env": Gate(ENV_PER_EXEC, REFUSE, _nonempty),
    "allowed_sensitive_keys": Gate(ENV_PER_EXEC, OMIT),
}

#: (method, SDK route template) -> {request field: Gate}. A route listed with
#: a ``None`` field entry is itself gated (``ROUTE_GATES``).
REQUEST_GATES: dict[tuple[str, str], dict[str, Gate]] = {
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec"): {
        **_EXEC_FIELDS,
        "env": Gate(EXEC_ENV, REFUSE, _nonempty),
    },
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec/start"): _START_FIELDS,
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/read"): _FILE_FIELDS,
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/read-base64"): _FILE_FIELDS,
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/write"): _FILE_FIELDS,
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/write-base64"): _FILE_FIELDS,
    ("POST", "/api/v1/sandbox/create-from-builtin"): _CREATE_FIELDS,
    ("POST", "/api/v1/sandbox/{sandbox_id}/load-skill"): _LOAD_FIELDS,
}

#: Routes BASELINE_RUNNER does not have -> the capability that adds them.
ROUTE_GATES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/sandbox/{sandbox_id}/exec/start"): EXEC_START,
    ("GET", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}"): EXEC_START,
    ("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/responses"): EXEC_START,
    ("POST", "/api/v1/sandbox/{sandbox_id}/files/tail"): FILES_TAIL,
}

#: Runner routes the control plane never calls, so nothing gates them.
RUNNER_ONLY_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/ready"): "the orchestrator readiness probe",
}

#: Fields the SDK sends that no runner reads (kept for older SDK callers).
IGNORED_BY_EVERY_RUNNER: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/api/v1/sandbox/create-from-builtin"): frozenset({"source"}),
}

#: The oldest runner still in service (dev b11d88306, 2026-08-18): the routes
#: and request fields it accepts. Everything newer is gated above.
BASELINE_RUNNER: dict[str, Any] = {
    "routes": frozenset(
        {
            ("GET", "/health"),
            ("POST", "/api/v1/skill/scan"),
            ("POST", "/api/v1/sandbox/create"),
            ("POST", "/api/v1/sandbox/create-from-files"),
            ("POST", "/api/v1/sandbox/create-from-builtin"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/load-skill"),
            ("GET", "/api/v1/sandbox"),
            ("GET", "/api/v1/sandbox/{sandbox_id}"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/touch"),
            ("DELETE", "/api/v1/sandbox/{sandbox_id}"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/exec"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/executions/{execution_id}/cancel"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/files/read"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/files/read-base64"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/files/write"),
            ("POST", "/api/v1/sandbox/{sandbox_id}/files/write-base64"),
            ("GET", "/api/v1/sandbox/{sandbox_id}/context"),
            ("POST", "/api/v1/skill/run"),
        }
    ),
    # Request model -> fields (sandbox-service/sandbox/models.py at b11d88306).
    "fields": {
        "ExecRequest": frozenset({"command", "timeout", "workdir", "execution_id"}),
        "FileReadRequest": frozenset({"path", "max_size"}),
        "FileReadBase64Request": frozenset({"path", "max_size"}),
        "FileWriteRequest": frozenset({"path", "content", "mkdir"}),
        "FileWriteBase64Request": frozenset({"path", "content_base64", "mkdir"}),
        "CreateFromFilesRequest": frozenset(
            {
                "skill_name", "files", "env", "allowed_sensitive_keys", "config_overrides",
                "auto_install", "idempotency_key", "source",
            }
        ),
        "CreateFromBuiltinRequest": frozenset(
            {
                "skill_name", "idempotency_key", "files", "env", "allowed_sensitive_keys",
                "config_overrides", "auto_install",
            }
        ),
        "LoadSkillRequest": frozenset({"skill_name", "files", "auto_install", "idle_threshold", "source"}),
        "ContainerConfig": frozenset(
            {
                "image", "network", "dns", "memory", "cpus", "pids_limit", "read_only_root", "tmpfs",
                "workdir_tmpfs_size", "cap_drop", "container_prefix", "workdir", "install_timeout",
                "exec_timeout", "volumes",
            }
        ),
    },
}

#: Runner request model -> the SDK route that sends it (for the guard test).
REQUEST_MODEL_ROUTES: dict[str, tuple[tuple[str, str], ...]] = {
    "ExecRequest": (
        ("POST", "/api/v1/sandbox/{sandbox_id}/exec"),
        ("POST", "/api/v1/sandbox/{sandbox_id}/exec/start"),
    ),
    "FileReadRequest": (("POST", "/api/v1/sandbox/{sandbox_id}/files/read"),),
    "FileReadBase64Request": (("POST", "/api/v1/sandbox/{sandbox_id}/files/read-base64"),),
    "FileWriteRequest": (("POST", "/api/v1/sandbox/{sandbox_id}/files/write"),),
    "FileWriteBase64Request": (("POST", "/api/v1/sandbox/{sandbox_id}/files/write-base64"),),
    "CreateFromFilesRequest": (("POST", "/api/v1/sandbox/create-from-files"),),
    "CreateFromBuiltinRequest": (("POST", "/api/v1/sandbox/create-from-builtin"),),
    "LoadSkillRequest": (("POST", "/api/v1/sandbox/{sandbox_id}/load-skill"),),
}


def gate_request(
    capabilities: RunnerCapabilities,
    method: str,
    route: str,
    body: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """(body to send, capabilities it lacks that make it unsendable).

    A non-empty second element means: refuse. Never removes anything for a
    runner whose capabilities are unknown, and refuses there only what needs
    proof (``Gate.requires_proof``).
    """

    if not capabilities.reported:
        unproven = [
            gate.capability
            for name, gate in REQUEST_GATES.get((method, route), {}).items()
            if gate.requires_proof and body and name in body and gate.needed_for(body[name])
        ]
        return body, list(dict.fromkeys(unproven))
    refused: list[str] = []
    route_capability = ROUTE_GATES.get((method, route))
    if route_capability is not None and capabilities.lacks(route_capability):
        refused.append(route_capability)
    gates = REQUEST_GATES.get((method, route), {})
    if body is None or not gates:
        return body, refused
    sent = dict(body)
    for name, gate in gates.items():
        if name not in sent or not gate.needed_for(sent[name]) or not capabilities.lacks(gate.capability):
            continue
        if gate.policy == OMIT:
            sent.pop(name)
        elif gate.capability not in refused:
            refused.append(gate.capability)
    return sent, refused
