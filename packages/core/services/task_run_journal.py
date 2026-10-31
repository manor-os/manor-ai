"""Write the Task run journal: one bounded, model-visible row per run transition.

Every writer calls ``append_run_journal`` inside the transaction that CASes or
bumps the run row, after that write, so ``seq`` is the row's ``state_version``
at that point. A rolled-back transition leaves no row. ``(run_kind, run_id,
seq)`` is the idempotency key; a collision means a journal write without its
own bump and is logged (raised under ``MANOR_TASK_RUN_JOURNAL_STRICT``).

Reading contract (W4c):

* Order within one run is ``seq``. ``created_at`` is the insert's wall clock
  (``clock_timestamp()``) and only interleaves different runs of a Task.
* Readers address one run by ``(run_id, seq)`` (``ix_task_run_journal_run``);
  a Task's history by ``(task_id, created_at, id)``.
* ``round`` is the loop's own counter. It restarts with a fresh-transcript
  retry and when a parked call resumes, so group a run's rounds by
  ``(lifecycle_generation, round)`` and order them by ``seq``.
* A ``round`` row's ``usage_delta`` is that run's own model usage only. A
  parent that resumed on settled delegated children also shows their usage
  as ``child_usage_delta``: it is already in those child runs' own rows, so a
  reader summing a Task's runs adds ``usage_delta`` and never
  ``child_usage_delta`` (that is the parent's attribution view).
* A ``wake`` row carries the lifecycle the run continues in (after the
  rebind its answer caused); a ``retry`` row the new lifecycle.
* ``terminal`` names the summary comment the loop posted (``comment_log_id``)
  and carries the bounded ``summary``; a stop decided outside the loop (the
  Task was cancelled or failed) has ``source = "task_status"``.

Payloads are what the model saw or produced, never the host's bookkeeping:
tool arguments go through the chat stream's redactor
(``runtime_tool_arguments_for_chat``), lose secret-looking keys and pass the
log redactor; a call's status comes from ``runtime_tool_status_for_chat``
(an approval stop is ``awaiting_approval``, a suspension ``waiting``);
receipts keep only their public reference fields; a wait keeps its kind, card
and times (no checkpoint digests, no claim or wake identities). Every builder
emits only the keys declared below and every payload fits
``PAYLOAD_MAX_BYTES``; an oversized one is shrunk by kind and keeps its
required keys.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Iterable, Mapping

from sqlalchemy.dialects.postgresql import insert as pg_insert

from packages.core.ai.runtime.tool_gateway import CALL_TOOL_NAME, runtime_gateway_target
from packages.core.models.task_agent_child import TaskAgentChildRun
from packages.core.models.task_coordinator import TaskCoordinatorEngine, TaskCoordinatorRun
from packages.core.models.task_run_journal import (
    TaskRunJournal,
    TaskRunJournalKind,
    TaskRunKind,
)


logger = logging.getLogger(__name__)

PAYLOAD_MAX_BYTES = 16 * 1024
ASSISTANT_TEXT_MAX_CHARS = 4000
TOOL_CALLS_MAX = 30
ARGS_SUMMARY_MAX_CHARS = 500
RECEIPTS_PER_CALL_MAX = 10
DELIVERABLES_MAX = 10
SUMMARY_MAX_CHARS = 1000
_TEXT_FIELD_MAX_CHARS = 500
_ID_MAX_CHARS = 128
STRICT_ENV = "MANOR_TASK_RUN_JOURNAL_STRICT"

# The only keys a payload may carry, per level.
ROUND_PAYLOAD_KEYS = frozenset({
    "assistant_text", "assistant_text_truncated", "tool_calls", "tool_calls_omitted",
    "round", "usage_delta", "child_usage_delta",
})
TOOL_CALL_KEYS = frozenset({
    "call_id", "name", "args_summary", "status", "effect_state", "receipts", "receipts_omitted",
})
RECEIPT_KEYS = ("handle", "kind", "document_id", "fs_path", "url", "name")
WAIT_KEYS = frozenset({
    "kind", "tool_call_id", "tool_name", "hitl_request_id", "summary", "reason",
    "wake_at", "deadline_at", "job_count", "child_count", "reparked",
})
PLAN_REVISION_KEYS = frozenset({"revision", "item_count", "acceptance_criteria_count"})
PARK_KEYS = frozenset({"wait"})
WAKE_KEYS = frozenset({"wait", "resolution", "task_resumed", "decided_by"})
RETRY_KEYS = frozenset({"prior_phase", "reason"})
TERMINAL_KEYS = frozenset({
    "status", "phase", "error_code", "source", "summary", "comment_log_id",
    "deliverables", "deliverables_omitted",
    # A completed delivery whose acceptance report left criteria unmet or
    # unverified: how many (the criteria themselves are in the comment).
    "acceptance_exceptions",
    # How many tool calls of that delivery stayed unconfirmed (W1a).
    "unconfirmed_effects",
})
DELIVERABLE_KEYS = ("document_id", "fs_path", "url", "name", "origin")
# A delegation: the parked call and each child run it made (or re-parked on).
DELEGATE_KEYS = frozenset({"tool_call_id", "children", "reparked"})
DELEGATED_CHILD_KEYS = ("run_id", "item_index", "agent", "agent_id", "prompt_summary")
PROMPT_SUMMARY_MAX_CHARS = 300
# One child run's terminal row: its verdict and the receipts its parent adopts.
CHILD_SETTLED_KEYS = frozenset({
    "status", "phase", "outcome_status", "interrupted", "error_code", "source",
    "receipts", "receipts_omitted",
})
# A delegation batch settled into its parent: each child's verdict.
BATCH_SETTLED_KEYS = frozenset({"tool_call_id", "resolution", "children", "completed", "total"})
SETTLED_CHILD_KEYS = ("run_id", "agent", "status", "interrupted")
# Set by the shrink backstop on any kind.
TRUNCATED_KEY = "truncated"

# What a row of each kind keeps even when its payload had to be shrunk.
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    TaskRunJournalKind.ROUND.value: ("round",),
    TaskRunJournalKind.PLAN_REVISION.value: ("revision", "item_count", "acceptance_criteria_count"),
    TaskRunJournalKind.PARK.value: ("wait",),
    TaskRunJournalKind.WAKE.value: ("wait", "resolution", "task_resumed", "decided_by"),
    TaskRunJournalKind.RETRY.value: ("prior_phase", "reason"),
    TaskRunJournalKind.TERMINAL.value: ("status", "phase", "error_code", "source", "comment_log_id"),
    TaskRunJournalKind.DELEGATE.value: ("tool_call_id", "children"),
    TaskRunJournalKind.CHILD_SETTLED.value: ("status", "phase", "outcome_status", "interrupted", "error_code"),
    TaskRunJournalKind.BATCH_SETTLED.value: ("tool_call_id", "resolution", "completed", "total"),
}

# Argument keys whose values never reach a journal row, at any depth.
_SECRET_KEY_RE = re.compile(
    r"(^|_)(password|passwd|passphrase|secret|token|api_?key|apikey|authorization|auth|"
    r"headers?|cookies?|credentials?|private_?key|access_?key|signature|session_?id)($|_)",
)


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:_ID_MAX_CHARS] or None


def _size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


# ── tool calls of one round ────────────────────────────────────────────


def _secret_key(key: Any) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key).strip().lower().replace("-", "_")))


def _without_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_secrets(item) for key, item in value.items() if not _secret_key(key)
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def _args_summary(name: str, arguments: Any) -> str | None:
    from packages.core.ai.runtime.streams import runtime_tool_arguments_for_chat
    from packages.core.observability.log_redaction import redact_sensitive_log_text

    public = _without_secrets(arguments) if isinstance(arguments, Mapping) else None
    try:
        compact = runtime_tool_arguments_for_chat(name, dict(public) if public is not None else None)
    except Exception:
        compact = None
    compact = _without_secrets(compact) if compact else None
    if not compact:
        return None
    text = json.dumps(compact, ensure_ascii=False, default=str, sort_keys=True)
    return redact_sensitive_log_text(text)[:ARGS_SUMMARY_MAX_CHARS]


def _call_status(result: Any) -> str:
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.runtime.streams import (
        _runtime_tool_control_status,
        runtime_tool_status_for_chat,
    )

    try:
        if isinstance(result, RuntimeToolSuspension):
            return "waiting"
        if _runtime_tool_control_status(result) == "hitl_required":
            # The chat stream shows this as "success" (its card follows); in
            # the journal the call has not run yet.
            return "awaiting_approval"
        return runtime_tool_status_for_chat(result)
    except Exception:
        return "unknown"


def _args_digest(arguments: Any) -> str:
    """A match key for model-visible arguments (never the arguments themselves)."""

    if not isinstance(arguments, Mapping):
        return ""
    public = {k: v for k, v in arguments.items() if not str(k).startswith("_")}
    canonical = json.dumps(public, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def observed_tool_call(name: str, result: Any, arguments: Any) -> dict[str, Any]:
    """What a round's journal keeps of one finished call, taken at ``on_tool_end``.

    Redacted on the spot: the buffer holds the redacted summary, the status
    and a digest of the arguments (to find the call's transcript id at
    checkpoint time), never raw arguments or results. ``name`` is kept whole
    for matching and truncated only in the payload.
    """

    return {
        "name": str(name or ""),
        "args_summary": _args_summary(name, arguments),
        "status": _call_status(result),
        # Match key only (dropped from the payload).
        "_match": _args_digest(arguments),
    }


def _message_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One assistant message's tool calls: id, name, parsed model-visible args."""

    calls: list[dict[str, Any]] = []
    for call in message.get("tool_calls") or ():
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "")
        raw = function.get("arguments", call.get("arguments"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        entry = {
            "id": str(call.get("id") or ""), "name": name,
            "arguments": raw if isinstance(raw, dict) else None, "match": _args_digest(raw),
        }
        target = runtime_gateway_target(raw) if name == CALL_TOOL_NAME else None
        if target is not None:
            # The loop runs (and observes) a gateway call as the tool it names,
            # under this same call id: one call, the inner tool's.
            entry["target"], entry["target_match"] = target[0], _args_digest(target[1])
        calls.append(entry)
    return calls


def _transcript_calls(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Tool calls of these messages, newest assistant message first."""

    calls: list[dict[str, Any]] = []
    for message in reversed(list(messages or [])):
        if isinstance(message, dict) and message.get("role") == "assistant":
            calls.extend(_message_calls(message))
    return calls


def _pick(candidates: list[dict[str, Any]], used: set[int], entry: Mapping[str, Any]) -> int | None:
    for exact in (True, False):
        for index, call in enumerate(candidates):
            if index in used:
                continue
            if call["name"] == entry["name"]:
                match = call["match"]
            elif call.get("target") == entry["name"]:
                match = call["target_match"]
            else:
                continue
            if exact and match != entry.get("_match"):
                continue
            return index
    return None


def _match_call_ids(
    observed: list[dict[str, Any]], messages: list[Any], start: int | None,
) -> list[str | None]:
    """Each observed call's transcript id.

    The round's own calls (messages after ``start``) are tried first, by
    name and arguments then by name; only a call they cannot explain (a
    parked call resumed at the start of the wake) is looked up in the
    earlier transcript.
    """

    split = start if start is not None else 0
    tiers = [_transcript_calls(messages[split:]), _transcript_calls(messages[:split])]
    used: list[set[int]] = [set(), set()]
    ids: list[str | None] = []
    for entry in observed:
        found = None
        for tier, candidates in enumerate(tiers):
            index = _pick(candidates, used[tier], entry)
            if index is not None:
                used[tier].add(index)
                found = candidates[index]["id"] or None
                break
        ids.append(found)
    return ids


def _receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _text(receipt[key], _TEXT_FIELD_MAX_CHARS)
        for key in RECEIPT_KEYS
        if receipt.get(key) not in (None, "")
    }


def round_start(messages: list[Any], previous_messages: list[Any] | None) -> int | None:
    """Where this round's messages begin, or None when it cannot be told.

    Normally the transcript only grows, so the round starts after the
    previous checkpoint's transcript. A compacted transcript no longer
    contains that prefix: the previous checkpoint's last message is looked up
    instead, and None means it is gone too (the round's own messages are then
    unknown).
    """

    if not previous_messages:
        return 0
    count = len(previous_messages)
    if count <= len(messages) and messages[count - 1] == previous_messages[-1]:
        return count
    last = previous_messages[-1]
    for index in range(len(messages) - 1, -1, -1):
        if messages[index] == last:
            return index + 1
    return None


def _round_assistant(messages: list[Any], start: int | None) -> Mapping[str, Any] | None:
    """The assistant message this round added; after an unknown compaction, the last one."""

    for message in reversed(messages[start or 0:]):
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message
    return None


def _assistant_text(message: Mapping[str, Any] | None) -> str:
    if message is None:
        return ""
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    return str(content or "")


# A run's usage key holding what its settled delegated children used (rolled
# up into the run's usage for attribution; services.task_agent_child_settlement).
ROLLED_UP_USAGE_KEY = "child_usage"


def _numbers(usage: Any) -> dict[str, int | float]:
    return {
        str(key): value for key, value in (usage or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    } if isinstance(usage, Mapping) else {}


def _own_usage(usage: Any) -> dict[str, int | float]:
    """A run's usage without what its children used (rolled up into it)."""

    rolled = _numbers(usage.get(ROLLED_UP_USAGE_KEY)) if isinstance(usage, Mapping) else {}
    return {key: value - rolled.get(key, 0) for key, value in _numbers(usage).items()}


def _rolled_up_usage(usage: Any) -> dict[str, int | float]:
    return _numbers(usage.get(ROLLED_UP_USAGE_KEY)) if isinstance(usage, Mapping) else {}


def _usage_delta(usage: Any, previous: Any) -> dict[str, int | float]:
    current = {
        str(key): value for key, value in (usage or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    } if isinstance(usage, Mapping) else {}
    before = {
        str(key): value for key, value in (previous or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    } if isinstance(previous, Mapping) else {}
    if any(current.get(key, 0) < value for key, value in before.items()):
        # A fresh-transcript retry restarted the run's usage.
        before = {}
    delta = {key: value - before.get(key, 0) for key, value in current.items()}
    return {
        key: (round(value, 6) if isinstance(value, float) else value)
        for key, value in sorted(delta.items()) if value
    }


def round_payload(
    *,
    rounds: int,
    messages: Iterable[Any],
    observed: list[dict[str, Any]],
    run_state: Mapping[str, Any] | None,
    previous_usage: Any,
    previous_messages: list[Any] | None = None,
) -> dict[str, Any]:
    """The ``round`` row: the model's text, its calls and the usage spent.

    ``observed`` are the calls that finished in this round (``on_tool_end``),
    in order, including a parked call resumed at the start of a wake. A call
    the round's model message requested that did not finish (it parked the
    run, waits on a person, or was stopped) is listed after them as
    ``pending`` (not when a compaction hid which message is the round's).
    """

    messages = list(messages or [])
    start = round_start(messages, previous_messages)
    assistant = _round_assistant(messages, start)
    evidence = (run_state or {}).get("tool_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    effects = {
        item.get("call_id"): item.get("effect_state")
        for item in evidence.get("observations") or () if isinstance(item, Mapping)
    }
    receipts: dict[str, list[dict[str, Any]]] = {}
    for receipt in evidence.get("receipts") or ():
        if isinstance(receipt, Mapping) and receipt.get("call_id"):
            receipts.setdefault(str(receipt["call_id"]), []).append(_receipt(receipt))
    calls = []
    for entry, call_id in zip(observed, _match_call_ids(observed, messages, start)):
        call_receipts = receipts.get(call_id or "", [])
        call = {
            "call_id": call_id,
            "name": _text(entry["name"], _ID_MAX_CHARS),
            "args_summary": entry.get("args_summary"),
            "status": entry.get("status"),
            "effect_state": effects.get(call_id) if call_id else None,
            "receipts": call_receipts[:RECEIPTS_PER_CALL_MAX],
        }
        if len(call_receipts) > RECEIPTS_PER_CALL_MAX:
            call["receipts_omitted"] = len(call_receipts) - RECEIPTS_PER_CALL_MAX
        calls.append(call)
    finished = {call["call_id"] for call in calls if call["call_id"]}
    requested = _message_calls(assistant) if assistant is not None and start is not None else []
    for call in requested:
        if call["id"] and call["id"] in finished:
            continue
        calls.append({
            "call_id": call["id"] or None,
            "name": _text(call["name"], _ID_MAX_CHARS),
            "args_summary": _args_summary(call["name"], call["arguments"]),
            "status": "pending",
            "effect_state": None,
            "receipts": [],
        })
    text = _assistant_text(assistant)
    payload: dict[str, Any] = {
        "round": int(rounds or 0),
        "assistant_text": text[:ASSISTANT_TEXT_MAX_CHARS],
        "tool_calls": calls[:TOOL_CALLS_MAX],
        # The run's own model usage; children's rolled-up usage apart.
        "usage_delta": _usage_delta(
            _own_usage((run_state or {}).get("usage")), _own_usage(previous_usage),
        ),
    }
    child_delta = _usage_delta(
        _rolled_up_usage((run_state or {}).get("usage")), _rolled_up_usage(previous_usage),
    )
    if child_delta:
        payload["child_usage_delta"] = child_delta
    if len(text) > ASSISTANT_TEXT_MAX_CHARS:
        payload["assistant_text_truncated"] = True
    if len(calls) > TOOL_CALLS_MAX:
        payload["tool_calls_omitted"] = len(calls) - TOOL_CALLS_MAX
    return bound_round_payload(payload)


def bound_round_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink a round payload until it fits: receipts, then text, then calls."""

    if _size(payload) <= PAYLOAD_MAX_BYTES:
        return payload
    for call in payload["tool_calls"]:
        if call.get("receipts"):
            call["receipts_omitted"] = call.get("receipts_omitted", 0) + len(call["receipts"])
            call["receipts"] = []
    if _size(payload) <= PAYLOAD_MAX_BYTES:
        return payload
    for limit in (1000, 200):
        if len(payload["assistant_text"]) > limit:
            payload["assistant_text"] = payload["assistant_text"][:limit]
            payload["assistant_text_truncated"] = True
        for call in payload["tool_calls"]:
            if call.get("args_summary") and len(call["args_summary"]) > limit // 4:
                call["args_summary"] = call["args_summary"][: limit // 4]
        if _size(payload) <= PAYLOAD_MAX_BYTES:
            return payload
    while payload["tool_calls"] and _size(payload) > PAYLOAD_MAX_BYTES:
        payload["tool_calls"].pop()
        payload["tool_calls_omitted"] = payload.get("tool_calls_omitted", 0) + 1
    return payload


# ── parks, wakes, retries, plan revisions, terminal ────────────────────


def wait_summary(wait: Mapping[str, Any] | None, **extra: Any) -> dict[str, Any]:
    """A ``task_loop_wait`` (or a host re-stage) as the journal shows it."""

    wait = wait if isinstance(wait, Mapping) else {}
    summary: dict[str, Any] = {
        "kind": _text(wait.get("kind"), 40) or None,
        "tool_call_id": _id(wait.get("tool_call_id")),
        "tool_name": _id(wait.get("tool_name")),
        "hitl_request_id": _id(wait.get("hitl_request_id")),
        "summary": _text(wait.get("summary"), _TEXT_FIELD_MAX_CHARS) or None,
        "reason": _text(wait.get("reason"), _TEXT_FIELD_MAX_CHARS) or None,
        "wake_at": _text(wait.get("wake_at"), 40) or None,
        "deadline_at": _text(wait.get("deadline_at"), 40) or None,
    }
    if isinstance(wait.get("job_ids"), list):
        summary["job_count"] = len(wait["job_ids"])
    if isinstance(wait.get("child_ids"), list):
        summary["child_count"] = len(wait["child_ids"])
    summary.update(extra)
    return {key: value for key, value in summary.items() if key in WAIT_KEYS and value is not None}


def park_payload(wait: Mapping[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {"wait": wait_summary(wait, **extra)}


def wake_payload(
    wait: Mapping[str, Any] | None,
    *,
    resolution: str,
    task_resumed: bool,
    decided_by: str | None = None,
) -> dict[str, Any]:
    """A parked run woken: by a person's decision (``decided_by``) or by the host."""

    return {
        "wait": wait_summary(wait),
        "resolution": _text(resolution, 40),
        "task_resumed": bool(task_resumed),
        "decided_by": _id(decided_by),
    }


def retry_payload(*, prior_phase: str | None, reason: str) -> dict[str, Any]:
    return {"prior_phase": _text(prior_phase, 40) or None, "reason": _text(reason, 80)}


def plan_revision_payload(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision": int(plan.get("revision") or 0),
        "item_count": len(plan.get("items") or ()),
        "acceptance_criteria_count": len(plan.get("acceptance_criteria") or ()),
    }


def terminal_payload(
    *,
    status: str,
    phase: str,
    error_code: str | None,
    files: Iterable[Any] | None,
    source: str = "loop",
    summary: str | None = None,
    comment_log_id: str | None = None,
    acceptance_exceptions: int = 0,
    unconfirmed_effects: int = 0,
) -> dict[str, Any]:
    deliverables = [
        {key: _text(item[key], _TEXT_FIELD_MAX_CHARS) for key in DELIVERABLE_KEYS if item.get(key)}
        for item in files or () if isinstance(item, Mapping)
    ]
    payload: dict[str, Any] = {
        "status": _text(status, 40),
        "phase": _text(phase, 40),
        "error_code": _text(error_code, _ID_MAX_CHARS) or None,
        "source": _text(source, 40),
        "summary": _text(summary, SUMMARY_MAX_CHARS) or None,
        "comment_log_id": _id(comment_log_id),
        "deliverables": deliverables[:DELIVERABLES_MAX],
    }
    if len(deliverables) > DELIVERABLES_MAX:
        payload["deliverables_omitted"] = len(deliverables) - DELIVERABLES_MAX
    if acceptance_exceptions > 0:
        payload["acceptance_exceptions"] = int(acceptance_exceptions)
    if unconfirmed_effects > 0:
        payload["unconfirmed_effects"] = int(unconfirmed_effects)
    return payload


def delegate_payload(
    *, tool_call_id: str, children: Iterable[Any], reparked: bool = False,
) -> dict[str, Any]:
    """A delegation parked its call on child runs (``children`` are the child rows)."""

    payload: dict[str, Any] = {
        "tool_call_id": _id(tool_call_id),
        "children": [
            {
                "run_id": _id(getattr(child, "id", None)),
                "item_index": int(getattr(child, "item_index", 0) or 0),
                "agent": _text(getattr(child, "agent_label", None), _TEXT_FIELD_MAX_CHARS) or None,
                "agent_id": _id(getattr(child, "agent_id", None)),
                "prompt_summary": _text(getattr(child, "prompt", None), PROMPT_SUMMARY_MAX_CHARS) or None,
            }
            for child in children
        ],
    }
    if reparked:
        payload["reparked"] = True
    return payload


def child_settled_payload(
    *,
    status: str,
    phase: str,
    outcome_status: str | None,
    interrupted: bool,
    error_code: str | None,
    receipts: Iterable[Any] | None = None,
    source: str = "loop",
) -> dict[str, Any]:
    """A child run's terminal row; ``receipts`` are its ledger's receipt records."""

    records = [_receipt(item) for item in receipts or () if isinstance(item, Mapping)]
    payload: dict[str, Any] = {
        "status": _text(status, 40),
        "phase": _text(phase, 40),
        "outcome_status": _text(outcome_status, 40) or None,
        "interrupted": bool(interrupted),
        "error_code": _text(error_code, _ID_MAX_CHARS) or None,
        "source": _text(source, 40),
        "receipts": records[:RECEIPTS_PER_CALL_MAX],
    }
    if len(records) > RECEIPTS_PER_CALL_MAX:
        payload["receipts_omitted"] = len(records) - RECEIPTS_PER_CALL_MAX
    return payload


def batch_settled_payload(
    *, tool_call_id: str, resolution: str, children: Iterable[Any],
) -> dict[str, Any]:
    """A delegation batch settled into its parent (``children`` are the child rows)."""

    entries = [
        {
            "run_id": _id(getattr(child, "id", None)),
            "agent": _text(getattr(child, "agent_label", None), _TEXT_FIELD_MAX_CHARS) or None,
            "status": _text(getattr(child, "outcome_status", None) or getattr(child, "phase", None), 40),
            "interrupted": bool(getattr(child, "interrupted", False)),
        }
        for child in children
    ]
    return {
        "tool_call_id": _id(tool_call_id),
        "resolution": _text(resolution, 40),
        "children": entries,
        "completed": sum(1 for entry in entries if entry["status"] == "completed"),
        "total": len(entries),
    }


_KIND_KEYS: dict[str, frozenset[str]] = {
    TaskRunJournalKind.ROUND.value: ROUND_PAYLOAD_KEYS,
    TaskRunJournalKind.PLAN_REVISION.value: PLAN_REVISION_KEYS,
    TaskRunJournalKind.PARK.value: PARK_KEYS,
    TaskRunJournalKind.WAKE.value: WAKE_KEYS,
    TaskRunJournalKind.RETRY.value: RETRY_KEYS,
    TaskRunJournalKind.TERMINAL.value: TERMINAL_KEYS,
    TaskRunJournalKind.DELEGATE.value: DELEGATE_KEYS,
    TaskRunJournalKind.CHILD_SETTLED.value: CHILD_SETTLED_KEYS,
    TaskRunJournalKind.BATCH_SETTLED.value: BATCH_SETTLED_KEYS,
}


def _shrink_terminal(payload: dict[str, Any]) -> dict[str, Any]:
    """Deliverable references go first, then the summary; the verdict stays."""

    for limit in (200, 80):
        payload["deliverables"] = [
            {key: _text(value, limit) for key, value in item.items()} for item in payload.get("deliverables") or []
        ]
        if _size(payload) <= PAYLOAD_MAX_BYTES:
            return payload
    while payload.get("deliverables") and _size(payload) > PAYLOAD_MAX_BYTES:
        payload["deliverables"].pop()
        payload["deliverables_omitted"] = payload.get("deliverables_omitted", 0) + 1
    if _size(payload) > PAYLOAD_MAX_BYTES and payload.get("summary"):
        payload["summary"] = _text(payload["summary"], 200)
    return payload


def _required_only(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The backstop: the kind's required keys, each value cut to a short form."""

    def cut(value: Any) -> Any:
        if isinstance(value, str):
            return value[:200]
        if isinstance(value, Mapping):
            return {key: cut(item) for key, item in list(value.items())[:12]}
        if isinstance(value, list):
            return [cut(item) for item in value[:5]]
        return value

    kept = {key: cut(payload[key]) for key in _REQUIRED_KEYS.get(kind, ()) if key in payload}
    kept[TRUNCATED_KEY] = True
    return kept


def _bounded(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed = _KIND_KEYS.get(kind)
    if allowed is None:
        raise ValueError(f"No journal payload is defined yet for kind {kind!r}")
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Journal {kind} payload carries undeclared keys: {sorted(unknown)}")
    bounded = json.loads(json.dumps(dict(payload), ensure_ascii=False, default=str))
    if _size(bounded) <= PAYLOAD_MAX_BYTES:
        return bounded
    if kind == TaskRunJournalKind.TERMINAL.value:
        bounded = _shrink_terminal(bounded)
    elif kind == TaskRunJournalKind.ROUND.value:
        bounded = bound_round_payload(bounded)
    if _size(bounded) > PAYLOAD_MAX_BYTES:
        bounded = _required_only(kind, bounded)
    return bounded


# ── identity and the write ─────────────────────────────────────────────


def run_journal_identity(run: Any) -> dict[str, Any]:
    """Who a run row is, as every journal row of it records."""

    if isinstance(run, TaskCoordinatorRun):
        return {
            "task_id": run.task_id,
            "entity_id": run.entity_id,
            "workspace_id": run.workspace_id,
            "run_kind": TaskRunKind.ROOT.value,
            "run_id": run.id,
            "root_run_id": run.id,
            "parent_run_id": None,
            "parent_tool_call_id": None,
            "agent_id": run.root_agent_id,
            "agent_subscription_id": run.root_subscription_id,
            "depth": 0,
            "lifecycle_generation": run.lifecycle_generation,
        }
    if isinstance(run, TaskAgentChildRun):
        return {
            "task_id": run.task_id,
            "entity_id": run.entity_id,
            "workspace_id": run.workspace_id,
            "run_kind": TaskRunKind.CHILD.value,
            "run_id": run.id,
            "root_run_id": run.root_run_id,
            "parent_run_id": run.parent_ref,
            "parent_tool_call_id": run.parent_tool_call_id,
            "agent_id": run.agent_id,
            "agent_subscription_id": run.agent_subscription_id,
            "depth": run.depth,
            "lifecycle_generation": run.lifecycle_generation,
        }
    raise TypeError(f"No journal identity for run rows of {type(run).__name__}")


def _strict() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() in {"1", "true", "yes"}


async def append_run_journal(
    db: Any,
    *,
    run: Any,
    kind: TaskRunJournalKind | str,
    payload: Mapping[str, Any],
    round: int | None = None,
) -> None:
    """Journal the transition the caller just wrote to ``run`` (same transaction).

    ``seq`` is read from the row: its ``state_version`` after that write.
    """

    kind_value = kind.value if isinstance(kind, TaskRunJournalKind) else str(kind)
    seq = int(run.state_version)
    statement = pg_insert(TaskRunJournal).values(
        **run_journal_identity(run),
        seq=seq,
        kind=kind_value,
        round=round,
        payload=_bounded(kind_value, payload),
    ).on_conflict_do_nothing(constraint="uq_task_run_journal_run_seq")
    result = await db.execute(statement)
    if result.rowcount == 0:
        # Two journal writes on one version: a writer journaled without
        # bumping the run row. Never fatal in production.
        message = f"Task run journal seq collision: {kind_value} on run {run.id} at seq {seq}"
        if _strict():
            raise RuntimeError(message)
        logger.warning(message)


async def append_agent_loop_run_journal(
    db: Any,
    *,
    run: Any,
    kind: TaskRunJournalKind | str,
    payload: Mapping[str, Any],
) -> bool:
    """``append_run_journal`` for a Task run outside the loop, when an agent loop drives it.

    Runs of the retired Plan engine (``plan_dag``) write no journal. A
    delegated child run is always an agent loop. Returns whether a row was
    written.
    """

    if (
        not isinstance(run, TaskAgentChildRun)
        and getattr(run, "engine", None) != TaskCoordinatorEngine.AGENT_LOOP.value
    ):
        return False
    await append_run_journal(db, run=run, kind=kind, payload=payload)
    return True


__all__ = [
    "PAYLOAD_MAX_BYTES",
    "append_agent_loop_run_journal",
    "append_run_journal",
    "batch_settled_payload",
    "child_settled_payload",
    "delegate_payload",
    "observed_tool_call",
    "park_payload",
    "plan_revision_payload",
    "retry_payload",
    "round_payload",
    "round_start",
    "run_journal_identity",
    "terminal_payload",
    "wait_summary",
    "wake_payload",
]
