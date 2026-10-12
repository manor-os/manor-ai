"""The run state a parked or yielded agent loop resumes from.

Hosts used to rebuild their checkpoint from a hand-written key list each
round; state left off the list was silently lost at the next park/resume (the
loaded gateway tools of R2-6, earlier the posted-work digest). Here the
runtime owns the shape instead:

* ``RuntimeRunState`` holds what every agent loop carries. Each dataclass
  field declares whether it is persisted, derived from persisted fields, or
  ephemeral (``metadata={"run_state": ...}``), and what a retry does with it
  (``metadata={"on_retry": ...}``); a field without both declarations is a
  bug that the round-trip guard reports.
* A host registers its own state as named ``RuntimeRunStateSlot``s over its
  live state mapping, each with its own retry disposition. The snapshot
  captures every registered slot; nothing is listed by hand.

The snapshot is flat: core fields and slots are top-level keys. Checkpoints
written before this module (and their readers) use the same keys, so
``restore`` accepts them unchanged.

What is never serialized: authority. Tools loaded through discovery are
persisted as names only (``loaded_gateway_tools``, recorded from real
discovery loads); the harness re-authorizes them through exact discovery on
restore, which rebuilds the run-local discovered grants.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, fields, replace
from typing import Any, ClassVar


RUN_STATE_METADATA_KEY = "run_state"
RETRY_METADATA_KEY = "on_retry"
PERSIST = "persist"
DERIVED = "derived"
EPHEMERAL = "ephemeral"
RUN_STATE_KINDS = frozenset({PERSIST, DERIVED, EPHEMERAL})
# What a retry of a stopped run does with a persisted field or slot.
RETRY_CARRY = "carry"
RETRY_RESET = "reset"
RETRY_DISPOSITIONS = frozenset({RETRY_CARRY, RETRY_RESET})
# host_slots is the container of the slots; each slot declares its own.
RETRY_PER_SLOT = "per_slot"
RUN_STATE_VERSION_KEY = "run_state_version"
# Most recently loaded discovery tools a run keeps for its next resume. Kept
# apart from chat's capability hints, whose ranking puts used tools first.
LOADED_GATEWAY_TOOLS_LIMIT = 32
# Signals a loop hands its round observer next to the run state. Declared so
# an observer can read them; never persisted.
LOOP_SIGNAL_KEYS = frozenset({"terminal_failure"})


RUN_STATE_NEWER_RUNTIME_DETAIL = (
    "This Task was saved by a newer version of Manor. Try again once the update "
    "has finished rolling out."
)


class RuntimeRunStateVersionError(ValueError):
    """A checkpoint written by a newer runtime than this one.

    A rolling deploy's older worker can meet one. Its message is safe to show:
    the run is intact and a current runtime continues it.
    """

    def __init__(self, version: Any = None) -> None:
        super().__init__(RUN_STATE_NEWER_RUNTIME_DETAIL)
        self.version = version


def runtime_run_state_is_newer(data: Mapping[str, Any] | None) -> bool:
    """Whether ``data`` was written by a newer runtime than this one."""
    try:
        RuntimeRunState.restore({RUN_STATE_VERSION_KEY: (data or {}).get(RUN_STATE_VERSION_KEY)})
    except RuntimeRunStateVersionError:
        return True
    return False


def _persist(on_retry: str) -> dict[str, str]:
    return {RUN_STATE_METADATA_KEY: PERSIST, RETRY_METADATA_KEY: on_retry}


def _ephemeral() -> dict[str, str]:
    return {RUN_STATE_METADATA_KEY: EPHEMERAL}


def _copy_or_none(value: Any) -> Any:
    return None if value is None else copy.deepcopy(value)


def runtime_bounded_tool_names(names: Iterable[Any], *, limit: int = LOADED_GATEWAY_TOOLS_LIMIT) -> list[str]:
    """Distinct names, oldest first, the most recent ``limit`` kept.

    A name seen again moves to the end (it was loaded again).
    """
    ordered: dict[str, None] = {}
    for raw in names or ():
        name = str(raw or "").strip() if isinstance(raw, str) else ""
        if not name:
            continue
        ordered.pop(name, None)
        ordered[name] = None
    return list(ordered)[-max(0, limit):] if limit else []


@dataclass(frozen=True)
class RuntimeRunStateSlot:
    """One named piece of host state that survives park/resume.

    ``on_retry`` says whether a retry of the stopped run carries the value or
    resets it. ``encode`` turns the live value into JSON (``None`` leaves the
    slot out of the snapshot); ``decode`` turns the stored value (``None`` when
    absent) back into the live value. ``host_key`` is the key in the host's
    live state mapping when it differs from the stored ``name``.
    """

    name: str
    on_retry: str
    encode: Callable[[Any], Any] = _copy_or_none
    decode: Callable[[Any], Any] = _copy_or_none
    default: Callable[[], Any] = lambda: None
    host_key: str = ""

    @property
    def live_key(self) -> str:
        return self.host_key or self.name


class RuntimeRunStateSlots:
    """A host's registered slots, bound to its live state mapping."""

    def __init__(
        self,
        slots: Iterable[RuntimeRunStateSlot],
        *,
        live: MutableMapping[str, Any],
    ) -> None:
        registered: dict[str, RuntimeRunStateSlot] = {}
        live_keys: set[str] = set()
        reserved = runtime_run_state_reserved_keys()
        for slot in slots:
            if not slot.name or slot.name in reserved:
                raise ValueError(f"Run state slot name {slot.name!r} is reserved")
            if slot.name in registered or slot.live_key in live_keys:
                raise ValueError(f"Run state slot {slot.name!r} is registered twice")
            if slot.on_retry not in RETRY_DISPOSITIONS:
                raise ValueError(f"Run state slot {slot.name!r} does not declare what a retry does with it")
            registered[slot.name] = slot
            live_keys.add(slot.live_key)
        self._slots = registered
        self._live = live

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._slots)

    @property
    def live_keys(self) -> frozenset[str]:
        return frozenset(slot.live_key for slot in self._slots.values())

    def __iter__(self):
        return iter(self._slots.values())

    def names_on_retry(self, disposition: str) -> frozenset[str]:
        return frozenset(name for name, slot in self._slots.items() if slot.on_retry == disposition)

    def initialize(self) -> None:
        """Set every slot's default in the live state."""
        for slot in self._slots.values():
            self._live[slot.live_key] = slot.default()

    def capture(self) -> dict[str, Any]:
        """The encoded value of every slot that currently holds one."""
        captured: dict[str, Any] = {}
        for slot in self._slots.values():
            encoded = slot.encode(self._live.get(slot.live_key, slot.default()))
            if encoded is not None:
                captured[slot.name] = encoded
        return captured

    def load(self, encoded: Mapping[str, Any]) -> None:
        """Decode stored slot values into the live state (absent ones too)."""
        for slot in self._slots.values():
            self._live[slot.live_key] = slot.decode(copy.deepcopy(encoded.get(slot.name)))

    def select(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """The stored values of registered slots in a flat payload."""
        return {
            name: copy.deepcopy(data[name])
            for name in self._slots
            if name in data and data[name] is not None
        }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)]


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _legacy_loaded_tools(tool_evidence: Mapping[str, Any]) -> list[str]:
    """Discovery loads a checkpoint from before ``loaded_gateway_tools`` recorded.

    Only ``source: loaded`` hints: those come from a real discovery result.
    A ``used`` hint is recorded for any call the model made, so it is never
    a reason to restore a tool.
    """
    hints = tool_evidence.get("capability_hints")
    items = hints.get("hints") if isinstance(hints, dict) else None
    return runtime_bounded_tool_names(
        item.get("name") for item in items or () if isinstance(item, dict) and item.get("source") == "loaded"
    )


@dataclass
class RuntimeRunState:
    """Everything an agent loop needs to continue where it stopped."""

    VERSION: ClassVar[int] = 1

    messages: list[dict[str, Any]] = field(default_factory=list, metadata=_persist(RETRY_CARRY))
    usage: dict[str, Any] = field(default_factory=dict, metadata=_persist(RETRY_CARRY))
    # A retry gets a fresh round budget.
    rounds: int = field(default=0, metadata=_persist(RETRY_RESET))
    tool_calls_made: list[str] = field(default_factory=list, metadata=_persist(RETRY_CARRY))
    # RuntimeToolEvidence.checkpoint_state().
    tool_evidence: dict[str, Any] = field(default_factory=dict, metadata=_persist(RETRY_CARRY))
    # Host-observed web text the carried transcript cites.
    web_source_slices: list[dict[str, Any]] = field(default_factory=list, metadata=_persist(RETRY_CARRY))
    final_response_evidence: dict[str, Any] = field(default_factory=dict, metadata=_persist(RETRY_CARRY))
    # Tools discovery loaded in this run, oldest first (names only, never a
    # grant). Recorded from discovery results, not from what the model called.
    loaded_gateway_tools: list[str] = field(default_factory=list, metadata=_persist(RETRY_CARRY))
    # Encoded values of the host's registered slots, by slot name.
    host_slots: dict[str, Any] = field(
        default_factory=dict,
        metadata={RUN_STATE_METADATA_KEY: PERSIST, RETRY_METADATA_KEY: RETRY_PER_SLOT},
    )
    # Declared loop signals for this round's observer (LOOP_SIGNAL_KEYS).
    loop_signals: dict[str, Any] = field(default_factory=dict, metadata=_ephemeral())

    def resume_loop_kwargs(self) -> dict[str, Any]:
        """The agentic loop's ``initial_*`` arguments for this state.

        The stored system message is dropped: the loop adds the current one.
        """
        return {
            "initial_messages": [
                copy.deepcopy(message) for message in self.messages
                if message.get("role") != "system"
            ],
            "initial_usage": copy.deepcopy(self.usage),
            "initial_rounds": self.rounds,
            "initial_tool_calls_made": list(self.tool_calls_made),
        }

    def snapshot(self, slots: RuntimeRunStateSlots | None = None) -> dict[str, Any]:
        """A JSON-ready flat payload of every persisted field and slot.

        With ``slots``, the host's live slot values are captured now and
        replace any carried ``host_slots``. Ephemeral fields never appear.
        """
        payload: dict[str, Any] = {RUN_STATE_VERSION_KEY: self.VERSION}
        for item in fields(self):
            kind = item.metadata.get(RUN_STATE_METADATA_KEY)
            if kind not in RUN_STATE_KINDS:
                raise TypeError(f"RuntimeRunState.{item.name} does not declare how it is checkpointed")
            if kind != PERSIST or item.name == "host_slots":
                continue
            payload[item.name] = copy.deepcopy(getattr(self, item.name))
        host_slots = slots.capture() if slots is not None else self.host_slots
        payload.update(copy.deepcopy(host_slots))
        return payload

    @classmethod
    def restore(
        cls,
        data: Mapping[str, Any] | None,
        slots: RuntimeRunStateSlots | None = None,
    ) -> "RuntimeRunState":
        """Read a snapshot, or a flat checkpoint written before this module.

        Malformed values fall back to empty ones; keys that are neither a
        field nor a registered slot (host park state) are left to the host.
        A snapshot from a newer runtime raises ``RuntimeRunStateVersionError``.
        """
        data = data if isinstance(data, Mapping) else {}
        version = data.get(RUN_STATE_VERSION_KEY)
        if version is not None and (not isinstance(version, int) or version > cls.VERSION):
            raise RuntimeRunStateVersionError(version)
        raw_calls = data.get("tool_calls_made")
        tool_evidence = _dict(data.get("tool_evidence"))
        raw_loaded = data.get("loaded_gateway_tools")
        return cls(
            messages=_list_of_dicts(data.get("messages")),
            usage=_dict(data.get("usage")),
            rounds=_non_negative_int(data.get("rounds")),
            tool_calls_made=[str(item) for item in raw_calls] if isinstance(raw_calls, list) else [],
            tool_evidence=tool_evidence,
            web_source_slices=_list_of_dicts(data.get("web_source_slices")),
            final_response_evidence=_dict(data.get("final_response_evidence")),
            loaded_gateway_tools=(
                runtime_bounded_tool_names(raw_loaded)
                if isinstance(raw_loaded, list)
                else _legacy_loaded_tools(tool_evidence)
            ),
            host_slots=slots.select(data) if slots is not None else {},
            loop_signals={key: copy.deepcopy(data[key]) for key in LOOP_SIGNAL_KEYS if key in data},
        )

    def for_retry(self, slots: RuntimeRunStateSlots, *, fresh_transcript: bool = False) -> "RuntimeRunState":
        """What a retry of this stopped run starts from.

        Each field and slot declares ``on_retry``: carried values stay, reset
        ones return to their default. A fresh-transcript retry (the transcript
        itself may be what failed) resets every core field and keeps only the
        carried slots.
        """
        retried = RuntimeRunState()
        for item in fields(self):
            if item.metadata.get(RETRY_METADATA_KEY) == RETRY_CARRY and not fresh_transcript:
                setattr(retried, item.name, copy.deepcopy(getattr(self, item.name)))
        carried = slots.names_on_retry(RETRY_CARRY)
        retried.host_slots = {
            name: copy.deepcopy(value) for name, value in self.host_slots.items() if name in carried
        }
        return retried

    def with_usage(self, usage: Mapping[str, Any]) -> "RuntimeRunState":
        return replace(self, usage=dict(usage))


def runtime_run_state_field_kinds() -> dict[str, str | None]:
    """Each dataclass field's declared kind (None when undeclared)."""
    return {
        item.name: item.metadata.get(RUN_STATE_METADATA_KEY)
        for item in fields(RuntimeRunState)
    }


def runtime_run_state_retry_dispositions() -> dict[str, str | None]:
    """Each persisted field's declared retry disposition (None when undeclared)."""
    return {
        item.name: item.metadata.get(RETRY_METADATA_KEY)
        for item in fields(RuntimeRunState)
        if item.metadata.get(RUN_STATE_METADATA_KEY) == PERSIST
    }


def runtime_run_state_persisted_keys() -> frozenset[str]:
    """Top-level keys of a snapshot besides the host's slots."""
    return frozenset(
        {RUN_STATE_VERSION_KEY}
        | {name for name, kind in runtime_run_state_field_kinds().items() if kind == PERSIST and name != "host_slots"}
    )


def runtime_run_state_reserved_keys() -> frozenset[str]:
    """Top-level snapshot keys a host slot may not take."""
    return frozenset(
        runtime_run_state_persisted_keys()
        | LOOP_SIGNAL_KEYS
        | {name for name in runtime_run_state_field_kinds() if name not in {"host_slots"}}
    )


def runtime_run_state_persistable(observed: Mapping[str, Any]) -> dict[str, Any]:
    """A round observer's state without its ephemeral loop signals."""
    return {key: value for key, value in observed.items() if key not in LOOP_SIGNAL_KEYS}


__all__ = [
    "DERIVED",
    "EPHEMERAL",
    "LOADED_GATEWAY_TOOLS_LIMIT",
    "LOOP_SIGNAL_KEYS",
    "PERSIST",
    "RETRY_CARRY",
    "RETRY_DISPOSITIONS",
    "RETRY_PER_SLOT",
    "RETRY_RESET",
    "RUN_STATE_KINDS",
    "RUN_STATE_NEWER_RUNTIME_DETAIL",
    "RUN_STATE_METADATA_KEY",
    "RUN_STATE_VERSION_KEY",
    "RuntimeRunState",
    "RuntimeRunStateSlot",
    "RuntimeRunStateSlots",
    "RuntimeRunStateVersionError",
    "runtime_bounded_tool_names",
    "runtime_run_state_field_kinds",
    "runtime_run_state_is_newer",
    "runtime_run_state_persistable",
    "runtime_run_state_persisted_keys",
    "runtime_run_state_reserved_keys",
    "runtime_run_state_retry_dispositions",
]
