"""Bounded host observations, not semantic success or replay authority."""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from packages.core.ai.runtime.receipts import EffectState, runtime_aggregate_effect_state
from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult

# The receipt ledger outlives the bounded observation window: a long run must
# never lose the receipt of the file it produced early on.
_MAX_RECEIPTS = 512
_MAX_UNCONFIRMED_CALLS = 256
# The bounded, model-citable observation window.
_OBSERVATION_WINDOW = 128
# Merge keys ("{prefixed call ID}#{identity digest}") of child observations
# already adopted: a child reaching the parent twice (carrier and channel, or a
# retried merge) registers its receipts once. Oldest keys are dropped first.
_MAX_MERGED_CHILD_CALL_IDS = 2048
# Confirmed non-file side effects of the run (a created Task, a sent message,
# a published site ...), aggregated per action_key in first-seen order. Like
# receipts they outlive the observation window. Purely informational: the
# effects ledger never moves effect_state or the effect floor.
_MAX_EFFECT_KEYS = 32
_MAX_EFFECT_TARGETS = 5
# Call IDs already counted ({call_id: action_key}), oldest dropped first. A
# per-call seen-set, not a per-key digest: a delegated child republishes its
# whole cumulative ledger, and only call identity tells an already-counted
# call from a new one.
_MAX_EFFECT_CALL_IDS = 256
# A call still in the observation window can be observed (or merged) again;
# it must still be in the seen-set then, or it would be counted twice.
assert _MAX_EFFECT_CALL_IDS > _OBSERVATION_WINDOW, "effect seen-set must outlive the observation window"
# Child effect calls already accounted for (counted or omitted), per child
# prefix: a cumulative republish adds only calls not accounted for yet.
_MAX_CHILD_ACCOUNTED_PREFIXES = 256
# Identity of a non-file receipt that names what the effect touched.
_EFFECT_TARGET_KEYS = ("name", "url", "external_id")
# Delegation runs work inside this run: the child's own effects are merged
# into this ledger and listed themselves.
_IN_RUN_ACTION_KEYS = frozenset({"workspace.service.delegate"})
# A child's effects ledger travels as this ledger-only marker, never as an
# observation: it carries no effect_state, so it cannot move the floor.
_EFFECTS_MARKER = {"call_id": "~effects", "tool": "ledger", "operation": "effects"}


def _classify(tool_name: str, arguments: dict | None) -> Any:
    """The runtime classification of one call, or None when it cannot say."""
    from packages.core.ai.runtime import approval_classifier

    try:
        return approval_classifier.classify_runtime_tool(
            tool_name, arguments if isinstance(arguments, dict) else {},
        )
    except Exception:
        return None


def _classifier_facts(classification: Any) -> dict[str, str]:
    """Compact, host-only classifier facts for an ACTION call; {} otherwise."""
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect

    action = getattr(classification, "action", None)
    if classification is None or classification.effect is not RuntimeToolEffect.ACTION or action is None:
        return {}
    facts = {
        "effect": classification.effect.value,
        "action_key": str(action.action_key or ""),
        "resource_kind": str(action.resource_kind or ""),
        "action_operation": str(action.operation or ""),
    }
    return {key: value for key, value in facts.items() if value}


# Bookkeeping for the effects ledger, never part of model-visible evidence.
_CLASSIFIER_FACT_KEYS = frozenset({"effect", "action_key", "resource_kind", "action_operation"})


def _is_file_receipt(receipt: dict) -> bool:
    return bool(receipt.get("document_id") or receipt.get("fs_path"))


def _listed_effect(observation: dict, receipts: list[dict]) -> dict | None:
    """The effect one observed call adds to the effects ledger, or None.

    Only a confirmed ACTION is a side effect. Not listed: calls whose effect
    is already visible as a file receipt, file create/modify actions (their
    files arrive as receipts, possibly from a later call such as
    wait_media_jobs), read-operation actions (asking a question) and
    delegation (its child's effects are listed instead).
    """
    if observation.get("effect") != "action" or not observation.get("action_key"):
        return None
    if _known_effect_state(observation.get("effect_state")) != EffectState.CONFIRMED.value:
        return None
    operation = observation.get("action_operation")
    if (
        any(_is_file_receipt(receipt) for receipt in receipts)
        or (observation.get("resource_kind") == "file" and operation in {"create", "modify"})
        or operation == "read"
        or observation["action_key"] in _IN_RUN_ACTION_KEYS
    ):
        return None
    targets = []
    for receipt in receipts:
        target = next((str(receipt[k])[:300] for k in _EFFECT_TARGET_KEYS if receipt.get(k)), None)
        if target:
            targets.append(target)
    return {
        "call_id": observation["call_id"],
        "action_key": observation["action_key"],
        "resource_kind": observation.get("resource_kind"),
        "operation": operation,
        "targets": targets,
    }


def _known_effect_state(value: Any) -> str:
    """An observation's effect_state; missing or invalid proves nothing (unconfirmed)."""
    try:
        return EffectState(str(value)).value if value is not None else EffectState.UNCONFIRMED.value
    except ValueError:
        return EffectState.UNCONFIRMED.value


# A child's sticky effect floor travels as this ledger-only marker, never as an
# observation the model sees.
_EFFECT_FLOOR_MARKER = {"call_id": "~effect_floor", "tool": "ledger", "operation": "effect_floor"}


def _is_ledger_marker(item: dict, marker: dict) -> bool:
    """Whether a child item is this ledger-only marker.

    Matched by tool + operation and the last call-ID segment, never the exact
    call ID: a delegation prefixes every child call ID with its run ID
    ("run-1/~effects"), and a nested child adds its own prefix.
    """
    call_id = item.get("call_id")
    return (
        item.get("tool") == marker["tool"]
        and item.get("operation") == marker["operation"]
        and isinstance(call_id, str)
        and call_id.rsplit("/", 1)[-1] == marker["call_id"]
    )


def _child_identity_digest(item: dict) -> str:
    """Stable identity of one child observation, independent of when it was seen.

    The same evidence merged twice digests equally; a later child run that
    reuses local call IDs but produced different results does not.
    """
    identity = {
        "tool": item.get("tool"),
        "operation": item.get("operation"),
        "effect_state": _known_effect_state(item.get("effect_state")),
        # Handles and call IDs are positional, not what the child produced.
        "receipt_records": [
            {k: v for k, v in record.items() if k not in {"handle", "call_id"}}
            for record in item.get("receipt_records") or () if isinstance(record, dict)
        ],
    }
    if _is_ledger_marker(item, _EFFECTS_MARKER):
        # Only the effects marker: other digests stay as checkpointed merge
        # keys recorded them.
        identity["effects"] = [item.get("effects"), item.get("effect_calls"), item.get("effects_omitted")]
    blob = json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# A sandbox skill handoff stays open until its deliverable is saved; a run
# that keeps opening sandboxes cannot grow this list without bound.
_MAX_SANDBOX_HANDOFFS = 16
_MAX_HANDOFF_FAILURES = 8


# Ledger bookkeeping stays host-only; handles become model-visible only in W2.
_HOST_ONLY_OBSERVATION_KEYS = frozenset({"receipt_records", "receipt_handles", *_CLASSIFIER_FACT_KEYS})


def runtime_model_view_of_observation(item: dict) -> dict:
    """A copy of one host observation that is safe to show the model."""
    return {k: copy.deepcopy(v) for k, v in item.items() if k not in _HOST_ONLY_OBSERVATION_KEYS}


# Host-only side channel from a tool that runs a child loop (delegation) to the
# tool call that invoked it. Model or provider JSON never reaches this list.
_child_evidence_sink: ContextVar[list[dict] | None] = ContextVar(
    "runtime_child_evidence_sink", default=None,
)


@contextmanager
def runtime_child_evidence_scope() -> Iterator[list[dict]]:
    sink: list[dict] = []
    token = _child_evidence_sink.set(sink)
    try:
        yield sink
    finally:
        _child_evidence_sink.reset(token)


def runtime_publish_child_evidence(observations: Iterable[dict] | None) -> bool:
    """Hand child observations to the calling tool call; False when no call listens."""
    sink = _child_evidence_sink.get()
    if sink is None:
        return False
    sink.extend(copy.deepcopy(item) for item in observations or () if isinstance(item, dict))
    return True


def runtime_child_evidence_from_state(state: Any) -> list[dict]:
    """A child run's whole ledger as observations, not only its bounded window.

    The checkpoint stores each receipt once, in ``receipts``; this attaches
    them per call as ``receipt_records`` for the parent to adopt. Receipts and
    unconfirmed calls outlive the child's 128-observation window; each evicted
    call comes back as one minimal (stage-less, ledger-only) item.
    """
    if not isinstance(state, dict):
        return []
    receipts_by_call: dict[str, list[dict]] = {}
    for receipt in state.get("receipts") or ():
        if isinstance(receipt, dict) and isinstance(receipt.get("call_id"), str):
            receipts_by_call.setdefault(receipt["call_id"], []).append(copy.deepcopy(receipt))
    observations = []
    for raw in state.get("observations") or ():
        if not isinstance(raw, dict) or not isinstance(raw.get("call_id"), str):
            continue
        item = copy.deepcopy(raw)
        if raw["call_id"] in receipts_by_call:
            item["receipt_records"] = copy.deepcopy(receipts_by_call[raw["call_id"]])
        observations.append(item)
    in_window = {item["call_id"] for item in observations}
    evicted: dict[str, dict] = {}
    for call_id, records in receipts_by_call.items():
        if call_id in in_window:
            continue
        first = records[0]
        evicted[call_id] = {
            "call_id": call_id,
            "tool": first.get("tool"),
            # The receipt's tool is the call's (inner) tool. Its own
            # "operation" names the artifact role (document, render ...),
            # not a call operation, so it is not used here.
            "operation": first.get("tool"),
            "effect_state": runtime_aggregate_effect_state(
                [EffectState.CONFIRMED] + [r.get("effect_state") for r in records]
            ).value,
            "receipt_records": records,
        }
    for call in state.get("unconfirmed_calls") or ():
        if not isinstance(call, dict) or not isinstance(call.get("call_id"), str):
            continue
        if call["call_id"] in in_window:
            continue
        if call["call_id"] in evicted:
            # One item per call: a retried call that also left receipts stays
            # unconfirmed rather than being dropped as a duplicate on merge.
            evicted[call["call_id"]]["effect_state"] = EffectState.UNCONFIRMED.value
            continue
        evicted[call["call_id"]] = {
            "call_id": call["call_id"],
            "tool": call.get("tool"),
            "operation": call.get("operation"),
            "effect_state": EffectState.UNCONFIRMED.value,
        }
    items = observations + list(evicted.values())
    # A call retried under the same ID can leave the child unconfirmed with no
    # unconfirmed item left to say so; carry the sticky floor explicitly.
    if state.get("effect_floor") == EffectState.UNCONFIRMED.value and not any(
        _known_effect_state(item.get("effect_state")) == EffectState.UNCONFIRMED.value for item in items
    ):
        items.append({**_EFFECT_FLOOR_MARKER, "effect_state": EffectState.UNCONFIRMED.value})
    effects = state.get("effects")
    calls = state.get("effect_calls")
    omitted = state.get("effects_omitted")
    if effects or calls or omitted:
        # The child's whole effects ledger, evicted calls included. It has no
        # effect_state: informational only, it never moves the parent's floor.
        items.append({
            **_EFFECTS_MARKER,
            "effects": copy.deepcopy(effects) if isinstance(effects, list) else [],
            "effect_calls": dict(calls) if isinstance(calls, dict) else {},
            "effects_omitted": omitted if isinstance(omitted, int) and omitted > 0 else 0,
        })
    return items


class RuntimeToolEvidence:
    def __init__(self):
        self.observations: dict[str, dict] = {}
        self._capability_scope: dict[str, str | None] | None = None
        self._capability_hints: list[dict[str, str]] = []
        # Tools discovery loaded in this run, oldest first: the names a parked
        # run re-authorizes on resume (RuntimeRunState.loaded_gateway_tools).
        # Only discovery results add to it, never a call the model made.
        self.loaded_tool_names: list[str] = []
        self.receipts: list[dict] = []
        self.unconfirmed_calls: list[dict] = []
        self._next_receipt = 1
        # Set once a receipt is dropped at the cap: the ledger is then incomplete.
        self.receipts_truncated = False
        # Monotonic run-level effect: unlike observations it is never evicted.
        self._effect_floor = EffectState.NONE
        # Insertion-ordered set of adopted child merge keys ("{prefixed id}#{digest}").
        self._merged_child_call_ids: dict[str, None] = {}
        # ``sandbox_ready`` handoffs of this turn. A handoff only opens the
        # skill's sandbox for the agent; it stays "pending" until a later call
        # saves a deliverable, and records later failed calls as blockers.
        self.sandbox_handoffs: list[dict] = []
        # Confirmed non-file side effects (see _listed_effect), one group per
        # action_key in first-seen order; calls past the key cap are omitted.
        self.effects: list[dict] = []
        self.effects_omitted = 0
        # Bounded seen-set of counted calls: {call_id: action_key}.
        self._effect_calls: dict[str, str] = {}
        # Child effect calls already accounted for (counted or omitted), per prefix.
        self._child_effects_accounted: dict[str, int] = {}
        # Effect call IDs counted during the merge in progress (None outside one).
        self._merge_counted: list[str] | None = None

    def configure_capability_scope(self, scope: dict[str, Any]) -> None:
        from packages.core.ai.runtime.capability_continuation import runtime_capability_scope

        self._capability_scope = runtime_capability_scope(**scope)

    def retain_loaded_tools(self, names: Any) -> None:
        """Keep names a resumed run had loaded but has not re-authorized yet.

        Names only: they become callable again only through exact discovery
        on a later resume. No capability hint is recorded for them.
        """
        from packages.core.ai.runtime.run_state import runtime_bounded_tool_names

        self.loaded_tool_names = runtime_bounded_tool_names([*self.loaded_tool_names, *list(names or ())])

    def observe_loaded_tools(self, names: Any) -> None:
        """Record schemas added after discovery, never the baseline surface."""
        from packages.core.ai.runtime.capability_continuation import runtime_capability_hint_state
        from packages.core.ai.runtime.run_state import runtime_bounded_tool_names

        names = list(names or ())
        self.loaded_tool_names = runtime_bounded_tool_names([*self.loaded_tool_names, *names])
        for raw_name in names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            if any(item["name"] == name for item in self._capability_hints):
                continue
            self._capability_hints.append({
                "name": name,
                "source": "loaded",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            })
        self._capability_hints = runtime_capability_hint_state(
            scope=self._capability_scope, hints=self._capability_hints,
        )["hints"]

    def _record_ledger(self, observation: dict, projected: list[dict]) -> None:
        """Register receipts and unconfirmed calls for one observation."""
        handles = []
        observed = observation.get("effect_state")
        # A receipt proves an artifact exists: at least confirmed, even when the
        # call that surfaced it was read-only (e.g. wait_media_jobs).
        receipt_state = runtime_aggregate_effect_state([observed, EffectState.CONFIRMED]).value
        self._effect_floor = runtime_aggregate_effect_state([self._effect_floor, observed])
        for receipt in projected:
            if len(self.receipts) >= _MAX_RECEIPTS:
                self.receipts_truncated = True
                break
            handle = f"r{self._next_receipt}"
            self._next_receipt += 1
            self.receipts.append({
                "handle": handle,
                "call_id": observation["call_id"],
                # The tool that produced the artifact (a gateway call's inner tool).
                "tool": receipt.get("tool") or observation["tool"],
                "effect_state": receipt_state,
                **{key: value for key, value in receipt.items() if key not in {"handle", "call_id", "tool", "effect_state"}},
            })
            handles.append(handle)
            self._effect_floor = runtime_aggregate_effect_state([self._effect_floor, receipt_state])
        if handles:
            observation["receipt_handles"] = handles
        effect = _listed_effect(observation, projected)
        if effect is not None:
            self._record_effect(effect)
        if observation.get("effect_state") == "unconfirmed" and len(self.unconfirmed_calls) < _MAX_UNCONFIRMED_CALLS:
            self.unconfirmed_calls.append({
                "call_id": observation["call_id"],
                "tool": observation["tool"],
                "operation": observation.get("operation"),
            })

    def _record_effect(self, effect: dict) -> None:
        """Count one effect call once, under its action_key (informational only)."""
        call_id, key = effect["call_id"], effect["action_key"]
        if call_id in self._effect_calls:
            return
        self._effect_calls[call_id] = key
        if self._merge_counted is not None:
            self._merge_counted.append(call_id)
        while len(self._effect_calls) > _MAX_EFFECT_CALL_IDS:
            self._effect_calls.pop(next(iter(self._effect_calls)))
        group = next((g for g in self.effects if g.get("action_key") == key), None)
        if group is None:
            if len(self.effects) >= _MAX_EFFECT_KEYS:
                self.effects_omitted += 1
                return
            group = {
                k: v for k, v in (
                    ("action_key", key),
                    ("resource_kind", effect.get("resource_kind")),
                    ("operation", effect.get("operation")),
                ) if v
            }
            group["count"] = 0
            self.effects.append(group)
        group["count"] += 1
        for target in effect.get("targets") or ():
            targets = group.setdefault("targets", [])
            if target not in targets and len(targets) < _MAX_EFFECT_TARGETS:
                targets.append(target)

    def _merge_child_effects(self, prefix: str, marker: dict) -> None:
        """Adopt a child's effects ledger without touching effect_state.

        Each counted child call is re-counted here under ``{prefix}/{call ID}``
        (the child's observations reach this ledger under the same IDs), so a
        cumulative republish counts only new calls. Calls the child no longer
        tracks (beyond its seen-set) are omitted, once: per prefix, the calls
        already accounted for (counted or omitted) are remembered.
        """
        groups = {
            g["action_key"]: g for g in marker.get("effects") or ()
            if isinstance(g, dict) and isinstance(g.get("action_key"), str)
        }
        calls = marker.get("effect_calls") if isinstance(marker.get("effect_calls"), dict) else {}
        for child_call_id, key in calls.items():
            if not isinstance(key, str):
                continue
            group = groups.get(key) or {}
            self._record_effect({
                "call_id": f"{prefix}/{child_call_id}", "action_key": key,
                "resource_kind": group.get("resource_kind"), "operation": group.get("operation"),
                "targets": group.get("targets") or [],
            })
        child_omitted = marker.get("effects_omitted")
        total = sum(int(g.get("count") or 0) for g in groups.values()) + (
            child_omitted if isinstance(child_omitted, int) and child_omitted > 0 else 0
        )
        # Calls of this child counted during this merge (its observations and
        # the marker's IDs), plus everything accounted for by earlier merges.
        newly = sum(1 for counted in self._merge_counted or () if counted.startswith(f"{prefix}/"))
        accounted = self._child_effects_accounted.pop(prefix, 0) + newly
        missing = max(0, total - accounted)
        self.effects_omitted += missing
        self._child_effects_accounted[prefix] = accounted + missing
        while len(self._child_effects_accounted) > _MAX_CHILD_ACCOUNTED_PREFIXES:
            self._child_effects_accounted.pop(next(iter(self._child_effects_accounted)))

    @property
    def effect_state(self) -> str:
        return runtime_aggregate_effect_state(
            [self._effect_floor]
            + [r.get("effect_state") for r in self.receipts]
            + ["unconfirmed" for _ in self.unconfirmed_calls]
            + [o.get("effect_state") for o in self.observations.values()]
        ).value

    def observe_tool_result(self, tool_name: str, call_id: str, result: Any, arguments: dict | None = None) -> None:
        if not call_id:
            return
        from packages.core.ai.runtime.capabilities import capabilities_for_tool_names
        from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
        from packages.core.ai.runtime.streams import runtime_tool_call_error
        from packages.core.services.sensitive_data import sanitize_sensitive_payload

        payload = result.structured_content if isinstance(result, RuntimeStructuredToolResult) else result
        try:
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except (ValueError, TypeError):
            payload = None
        call = RuntimeCompositeToolCallFactory.create(tool_name, arguments)
        action = str(call.arguments.get("action") or "")
        operation = call.tool_name
        if call.tool_name == "manor":
            operation = action or "manor"
        elif call.tool_name in {"workspace_agent", "acquire_capabilities"}:
            operation = f"{call.tool_name}.{action}"
        discovery = call.tool_name in {"search_tools", "acquire_capabilities"} or (
            call.tool_name == "manor" and action == "search"
        )
        inventory = call.tool_name == "workspace_search" or (
            call.tool_name == "workspace_agent" and action == "search"
        )
        error = runtime_tool_call_error(result)
        authorization_blocked = bool(
            call.tool_name == "acquire_capabilities" and action == "apply"
            and isinstance(payload, dict) and isinstance(payload.get("error"), str)
            and payload["error"] in {
                "blocked_by_permission", "blocked_by_governance", "capability_approval_unavailable",
            }
        )
        observation = {
            "call_id": call_id, "tool": tool_name, "operation": operation,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "stage": ("delegation" if call.tool_name == "invoke_skill" else
                      "authorization" if authorization_blocked else
                      "discovery" if discovery else "inventory" if inventory else "execution"),
            "result": "error" if error else "returned",
            "semantic_success_verified": False,
            "external_attempt": any(
                capability.id == "web.safe_search" for capability in capabilities_for_tool_names({tool_name})
            ) or (tool_name.startswith("mcp__") and not tool_name.startswith("mcp__manor_")),
            "authorization_blocked": authorization_blocked,
        }
        if isinstance(payload, dict):
            details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
            observation["failure_scope"] = details.get("failure_scope")
            observation["error"] = str(sanitize_sensitive_payload(str(payload.get("error") or "")))[:120]
            error_body = payload.get("error")
            if isinstance(error_body, dict) and isinstance(error_body.get("code"), str):
                # The tool's own error code, e.g. a quality gate that lists
                # corrections rather than a failure the turn cannot recover.
                observation["error_code"] = error_body["code"][:80]
            # Provider discovery is scoped evidence, never global incapability.
            if discovery:
                providers = details.get("unavailable_provider_keys")
                observation["unavailable_provider_keys"] = [
                    str(sanitize_sensitive_payload(str(item)))[:120] for item in providers[:16]
                ] if isinstance(providers, list) else []
                observation["capability_availability"] = "not_established"
        if error:
            observation["message"] = str(sanitize_sensitive_payload(str(error)))[:600]
        from packages.core.ai.runtime.receipts import runtime_project_receipts, runtime_tool_effect_state

        # The classifier already looks through call_tool / composite wrappers.
        # One classification serves the effect state and the facts below.
        classification = _classify(tool_name, arguments)
        effect_state = runtime_tool_effect_state(tool_name, arguments, result, classification=classification)
        observation["effect_state"] = effect_state.value
        # What the call does (action/control/...), so a non-file effect such
        # as a created Task is visible in the run's result.
        observation.update(_classifier_facts(classification))
        # Project unless the call may have failed mid-effect. Read-only calls
        # (e.g. wait_media_jobs, whose batch status can be "failed" while some
        # jobs completed) still yield receipts; projections filter per item.
        projected = (
            [
                {**receipt, "tool": call.tool_name}
                for receipt in runtime_project_receipts(call.tool_name, call.arguments, payload)
            ]
            if effect_state is not EffectState.UNCONFIRMED
            else []
        )
        self.observations[call_id] = observation
        existing_hint = next(
            (item for item in self._capability_hints if item["name"] == tool_name),
            None,
        )
        if existing_hint is None:
            self._capability_hints.append({
                "name": tool_name,
                "source": "used",
                "observed_at": observation["observed_at"],
            })
        else:
            existing_hint.update(source="used", observed_at=observation["observed_at"])
        from packages.core.ai.runtime.capability_continuation import runtime_capability_hint_state

        self._capability_hints = runtime_capability_hint_state(
            scope=self._capability_scope, hints=self._capability_hints,
        )["hints"]
        self._record_ledger(observation, projected)
        self._track_sandbox_handoff(call_id, call.tool_name, payload, observation, projected)
        # Serialized provider/model JSON cannot mint child evidence. Only the
        # host carrier, built from an executing child loop, supplies these facts.
        if isinstance(result, RuntimeStructuredToolResult):
            self.merge_child_observations(call_id, result.execution_evidence)
        while len(self.observations) > _OBSERVATION_WINDOW:
            self.observations.pop(next(iter(self.observations)))

    def _track_sandbox_handoff(
        self, call_id: str, tool_name: str, payload: Any, observation: dict, projected: list[dict],
    ) -> None:
        returned = isinstance(payload, dict) and observation["result"] == "returned"
        skill = str(payload.get("skill") or "")[:120] if isinstance(payload, dict) else ""
        pending = self.pending_sandbox_handoffs()
        if returned and tool_name == "invoke_skill" and skill:
            # Invoking a staged Skill again resumes its previous stage; the new
            # result reopens a handoff only if it stops at another stage.
            for handoff in pending:
                if handoff.get("kind") == "skill_continuation" and handoff["skill"] == skill:
                    handoff["state"] = "delivered"
                    handoff["delivered_by"] = call_id
        continuation = payload.get("continuation") if isinstance(payload, dict) else None
        if returned and tool_name == "invoke_skill" and skill and isinstance(continuation, dict):
            observation["handoff"] = "skill_continuation"
            self._open_handoff({
                "kind": "skill_continuation", "call_id": call_id, "skill": skill,
                "stage": str(continuation.get("stage") or "")[:48],
            })
            return
        if returned and payload.get("status") == "sandbox_ready":
            observation["handoff"] = "sandbox_ready"
            self._open_handoff({
                "kind": "sandbox_ready", "call_id": call_id, "skill": skill,
                "sandbox_id": str(payload.get("sandbox_id") or "")[:64],
            })
            return
        pending = self.pending_sandbox_handoffs()
        if not pending:
            return
        if observation["result"] == "error":
            for handoff in pending:
                if len(handoff["failed_call_ids"]) < _MAX_HANDOFF_FAILURES:
                    handoff["failed_call_ids"].append(call_id)
            return
        # Only resuming the Skill closes a stage handoff; a file saved on the way
        # (the canvas itself) is not the requested result.
        pending = [handoff for handoff in pending if handoff.get("kind") != "skill_continuation"]
        # A saved file delivers the handoff. Only a filesystem-only scratch save
        # (role "intermediate" and not registered in Knowledge) is a step on the
        # way: the role defaults to "intermediate" whenever the model omits it,
        # and a file in Knowledge is one the user can open either way.
        intermediate = (
            tool_name == "sandbox_save_result" and isinstance(payload, dict)
            and payload.get("artifact_role") == "intermediate"
            and payload.get("saved_to_knowledge") is not True
        )
        if projected and not intermediate:
            for handoff in pending:
                handoff["state"] = "delivered"
                handoff["delivered_by"] = call_id

    def _open_handoff(self, handoff: dict) -> None:
        self.sandbox_handoffs.append({**handoff, "state": "pending", "failed_call_ids": []})
        del self.sandbox_handoffs[:-_MAX_SANDBOX_HANDOFFS]

    def pending_sandbox_handoffs(self) -> list[dict]:
        return [handoff for handoff in self.sandbox_handoffs if handoff.get("state") == "pending"]

    def merge_child_observations(self, call_id: str, children: Any, *, observe: bool = True) -> None:
        """Adopt a child loop's ledger (host carrier or child channel only).

        Receipts, unconfirmed calls and the effect floor always reach this
        ledger. Only with ``observe`` does a child's full observation (one
        that has a ``stage``) also enter the bounded, model-citable window:
        the skill carrier exposes its child calls for citation, while
        delegated children (the channel) and rebuilt evicted calls stay
        ledger-only so they cannot evict this run's own calls or add facts.
        """
        self._merge_counted = []
        try:
            self._merge_children(call_id, children, observe=observe)
        finally:
            self._merge_counted = None
        while len(self.observations) > _OBSERVATION_WINDOW:
            self.observations.pop(next(iter(self.observations)))

    def _merge_children(self, call_id: str, children: Any, *, observe: bool) -> None:
        for child in children or ():
            if not isinstance(child, dict) or not isinstance(child.get("call_id"), str):
                continue
            merged_id = f"{call_id}/{child['call_id']}"
            # Prefixed call ID + identity digest: a retried merge of the same
            # evidence is skipped; a new run reusing local IDs still merges.
            merge_key = f"{merged_id}#{_child_identity_digest(child)}"
            if merge_key in self._merged_child_call_ids:
                continue
            self._merged_child_call_ids[merge_key] = None
            while len(self._merged_child_call_ids) > _MAX_MERGED_CHILD_CALL_IDS:
                self._merged_child_call_ids.pop(next(iter(self._merged_child_call_ids)))
            item = copy.deepcopy(child)
            item["call_id"] = merged_id
            item["parent_call_id"] = f"{call_id}/{child['parent_call_id']}" if child.get("parent_call_id") else call_id
            item["effect_state"] = _known_effect_state(item.get("effect_state"))
            if _is_ledger_marker(child, _EFFECT_FLOOR_MARKER):
                # Raise the floor and record the unconfirmed call; no observation.
                self._record_ledger(item, [])
                continue
            if _is_ledger_marker(child, _EFFECTS_MARKER):
                # The child's effects ledger: counted, never an observation.
                # Its call IDs hang off the marker's own prefix (a delegation
                # adds "run-1/"), as the child's observations do.
                self._merge_child_effects(merged_id.rsplit("/", 1)[0], item)
                continue
            child_receipts = item.pop("receipt_records", None) or []
            item.pop("receipt_handles", None)
            if observe and "stage" in item:
                self.observations[item["call_id"]] = item
            self._record_ledger(item, [
                {k: v for k, v in r.items() if k not in {"handle", "call_id", "effect_state"}}
                for r in child_receipts if isinstance(r, dict)
            ])

    def model_result_with_child_references(self, call_id: str, result: str) -> str:
        """Publish citable host IDs without altering receipts or their authority."""
        if self.observations.get(call_id, {}).get("stage") != "delegation":
            return result
        children = []
        for item in self.observations.values():
            parent = item.get("parent_call_id")
            seen = set()
            while parent and parent not in seen:
                if parent == call_id:
                    children.append(runtime_model_view_of_observation(item))
                    break
                seen.add(parent)
                parent = self.observations.get(parent, {}).get("parent_call_id")
        if not children:
            return result
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return result
        if not isinstance(payload, dict):
            return result
        key = "skill_execution" if "skill_execution" in payload else "execution"
        metadata = payload.get(key)
        if not isinstance(metadata, dict) or metadata.get("scope") != "skill_invocation":
            return result
        # Read only the host ledger: serialized child JSON cannot introduce a
        # fact or borrow a reference from another invocation.
        payload[key] = {**metadata, "evidence": children, "evidence_ref_format": "absolute"}
        payload[key].pop("evidence_ref_prefix", None)
        return json.dumps(payload, ensure_ascii=False)

    def checkpoint_state(self) -> dict:
        from packages.core.ai.runtime.capability_continuation import runtime_capability_hint_state

        # Receipts are stored once, in "receipts"; runtime_child_evidence_from_state
        # attaches them per call when a child ledger is handed to its parent.
        return {
            "observations": [copy.deepcopy(item) for item in self.observations.values()],
            "capability_hints": runtime_capability_hint_state(
                scope=self._capability_scope,
                hints=self._capability_hints,
            ),
            "receipts": copy.deepcopy(self.receipts),
            "unconfirmed_calls": copy.deepcopy(self.unconfirmed_calls),
            "effect_state": self.effect_state,
            "next_receipt": self._next_receipt,
            "effect_floor": self._effect_floor.value,
            "receipts_truncated": self.receipts_truncated,
            "merged_child_call_ids": list(self._merged_child_call_ids)[-_MAX_MERGED_CHILD_CALL_IDS:],
            "sandbox_handoffs": copy.deepcopy(self.sandbox_handoffs),
            "effects": copy.deepcopy(self.effects),
            "effects_omitted": self.effects_omitted,
            "effect_calls": dict(self._effect_calls),
            "child_effects_accounted": dict(self._child_effects_accounted),
        }

    def restore_checkpoint_state(self, state: dict) -> None:
        # Called only with a server-owned checkpoint, never model JSON/history.
        # A pre-ledger checkpoint (no effect_floor) never recorded effect_state:
        # infer it from the observed result instead of distrusting every call.
        # Over-reporting "confirmed" is safe; it never forbids a replay.
        legacy = isinstance(state, dict) and "effect_floor" not in state
        legacy_errors: list[dict] = []
        self.observations = {}
        for item in (state.get("observations") or [])[-_OBSERVATION_WINDOW:]:
            if not isinstance(item, dict) or not isinstance(item.get("call_id"), str):
                continue
            # Checkpoints written before receipts were stored once carried
            # receipt_records per observation; "receipts" is the only source.
            restored = {k: v for k, v in copy.deepcopy(item).items() if k != "receipt_records"}
            if legacy and item.get("effect_state") is None:
                restored["effect_state"] = (
                    EffectState.UNCONFIRMED.value if item.get("result") == "error" else EffectState.CONFIRMED.value
                )
                if item.get("result") == "error":
                    legacy_errors.append(restored)
            else:
                restored["effect_state"] = _known_effect_state(item.get("effect_state"))
            self.observations[item["call_id"]] = restored
        hints = state.get("capability_hints") if isinstance(state, dict) else None
        if isinstance(hints, dict) and self._capability_scope is None:
            scope = hints.get("scope")
            self._capability_scope = copy.deepcopy(scope) if isinstance(scope, dict) else None
        if (
            isinstance(hints, dict)
            and hints.get("scope") == self._capability_scope
            and isinstance(hints.get("hints"), list)
        ):
            self._capability_hints = copy.deepcopy(hints["hints"])
        receipts = state.get("receipts") if isinstance(state, dict) else None
        restored_receipts = [copy.deepcopy(r) for r in receipts if isinstance(r, dict)] if isinstance(receipts, list) else []
        self.receipts = restored_receipts[:_MAX_RECEIPTS]
        self.receipts_truncated = (
            (isinstance(state, dict) and state.get("receipts_truncated") is True)
            or len(restored_receipts) > _MAX_RECEIPTS
        )
        unconfirmed = state.get("unconfirmed_calls") if isinstance(state, dict) else None
        self.unconfirmed_calls = [copy.deepcopy(c) for c in unconfirmed if isinstance(c, dict)][:_MAX_UNCONFIRMED_CALLS] if isinstance(unconfirmed, list) else []
        for item in legacy_errors:
            if len(self.unconfirmed_calls) >= _MAX_UNCONFIRMED_CALLS:
                break
            self.unconfirmed_calls.append({
                "call_id": item["call_id"], "tool": item.get("tool"), "operation": item.get("operation"),
            })
        next_receipt = state.get("next_receipt") if isinstance(state, dict) else None
        self._next_receipt = max(
            int(next_receipt) if isinstance(next_receipt, int) else 1,
            len(self.receipts) + 1,
        )
        merged = state.get("merged_child_call_ids") if isinstance(state, dict) else None
        self._merged_child_call_ids = dict.fromkeys(
            [item for item in merged if isinstance(item, str)][-_MAX_MERGED_CHILD_CALL_IDS:]
            if isinstance(merged, list) else ()
        )
        handoffs = state.get("sandbox_handoffs") if isinstance(state, dict) else None
        self.sandbox_handoffs = [
            {
                "kind": "skill_continuation" if item.get("kind") == "skill_continuation" else "sandbox_ready",
                "call_id": str(item.get("call_id") or ""),
                "skill": str(item.get("skill") or "")[:120],
                **({"stage": str(item.get("stage") or "")[:48]}
                   if item.get("kind") == "skill_continuation" else
                   {"sandbox_id": str(item.get("sandbox_id") or "")[:64]}),
                "state": "delivered" if item.get("state") == "delivered" else "pending",
                "failed_call_ids": [
                    str(ref) for ref in item.get("failed_call_ids") or [] if isinstance(ref, str)
                ][:_MAX_HANDOFF_FAILURES],
                **({"delivered_by": str(item["delivered_by"])} if item.get("delivered_by") else {}),
            }
            for item in handoffs[-_MAX_SANDBOX_HANDOFFS:] if isinstance(item, dict)
        ] if isinstance(handoffs, list) else []
        effects = state.get("effects") if isinstance(state, dict) else None
        self.effects = [
            copy.deepcopy(item) for item in effects
            if isinstance(item, dict) and isinstance(item.get("action_key"), str)
            and isinstance(item.get("count"), int)
        ][:_MAX_EFFECT_KEYS] if isinstance(effects, list) else []
        omitted = state.get("effects_omitted") if isinstance(state, dict) else None
        self.effects_omitted = omitted if isinstance(omitted, int) and omitted > 0 else 0
        calls = state.get("effect_calls") if isinstance(state, dict) else None
        self._effect_calls = dict(
            [(k, v) for k, v in calls.items() if isinstance(k, str) and isinstance(v, str)][-_MAX_EFFECT_CALL_IDS:]
            if isinstance(calls, dict) else ()
        )
        accounted = state.get("child_effects_accounted") if isinstance(state, dict) else None
        self._child_effects_accounted = dict(
            [(k, v) for k, v in accounted.items() if isinstance(k, str) and isinstance(v, int) and v > 0][
                -_MAX_CHILD_ACCOUNTED_PREFIXES:
            ]
            if isinstance(accounted, dict) else ()
        )
        floor = state.get("effect_floor") if isinstance(state, dict) else None
        self._effect_floor = runtime_aggregate_effect_state(
            ([floor] if floor is not None else [])
            + [r.get("effect_state") for r in self.receipts]
            + ["unconfirmed" for _ in self.unconfirmed_calls]
            + [o["effect_state"] for o in self.observations.values()]
        )
