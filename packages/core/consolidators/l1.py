"""L1 semantic consolidation — the LLM-assisted summarization layer (M15).

L0 (everything else in this package) is deterministic SQL/Python. L1 is
the **whitelist** where one LLM call per domain per review turns
unstructured text into a short neutral label:

* M4.2 — cluster unstructured execution failure reasons (only when the
  same class of failure occurs ≥3 times);
* M4.5 — induce the common shape of repeated human edits.

Three properties hold by construction:

**Opt-in.** ``MANOR_CONSOLIDATOR_L1`` defaults to OFF. Until L1 has been
validated in staging, every deployment runs the pure-L0 pipeline; the
flag is the only way to turn the LLM path on, and flipping it changes
``compute_input_hash`` so cached reports never leak across the toggle.

**Budgeted.** At most ONE LLM call per domain per review. This is
enforced *structurally*: each consolidator has exactly one call site for
its L1 helper, guarded by a single threshold check — there is no loop or
retry around it, and a helper that returns ``None`` is never re-tried.

**Observation-only (裁定 1).** These helpers may only summarize. The
system prompt (``packages/core/ai/runtime/consolidation.py``) forbids
recommendations outright, the returned label is validated against a
strict JSON schema, and anything the model returns still has to survive
``FORBIDDEN_KEYS`` in ``contract.py`` before it can be persisted. Any
failure at all — disabled, no model, transport error, malformed JSON,
schema mismatch — returns ``None``; nothing ever raises into a review.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

L1_ENABLED_ENV = "MANOR_CONSOLIDATOR_L1"
L1_TRUTHY_VALUES = frozenset({"1", "true"})

# Budget: one LLM call per domain per review (M15 L1 白名单). Kept as a
# named constant so the invariant is greppable; enforcement is structural
# (single call site per consolidator, no retry).
L1_CALLS_PER_DOMAIN_PER_REVIEW = 1

# Input caps — L1 sees a compact, bounded sample, never the full window.
MAX_L1_INPUT_RECORDS = 20
MAX_ERROR_CHARS = 200
MAX_L1_OUTPUT_GROUPS = 10
MAX_LABEL_CHARS = 120

# Coverage markers written by consolidators into ``coverage.sources["l1"]``.
L1_DISABLED = "disabled"      # flag off — pure L0 run
L1_SKIPPED = "skipped"        # flag on, but the threshold was not met
L1_UNAVAILABLE = "unavailable"  # call attempted, no usable result
L1_USED = "used"              # call attempted, clusters returned

_FAILURE_INSTRUCTION = (
    "Each record is a group of failed automated executions: 'service_key' "
    "is the owning service, 'sample_error' is a truncated error string, "
    "'count' is how many executions the record covers. Group records whose "
    "errors describe the same kind of failure and give each group a short "
    "factual label naming the observed failure mode. Every record index "
    "must appear in at most one group; drop records that fit no group."
)
_FAILURE_SCHEMA_HINT = (
    '[{"cluster": "<short factual label>", "count": <integer>, '
    '"member_indexes": [<record index>, ...]}]'
)

_EDIT_INSTRUCTION = (
    "Each record is an aggregate of human edits: 'field' is the name of an "
    "edited field, 'count' is how many contributions changed it. Group "
    "field names that describe the same kind of change and give each group "
    "a short factual label naming what was edited."
)
_EDIT_SCHEMA_HINT = '[{"pattern": "<short factual label>", "count": <integer>}]'


def l1_enabled() -> bool:
    """Whether the opt-in L1 LLM layer is enabled for this process.

    Default OFF: L1 stays opt-in until it has been validated in staging.
    Truthy values are ``"1"`` and ``"true"`` (case-insensitive); anything
    else — including unset — keeps the pipeline pure-L0.
    """
    raw = str(os.getenv(L1_ENABLED_ENV, "") or "").strip().lower()
    return raw in L1_TRUTHY_VALUES


def truncate_error(text: Any, limit: int = MAX_ERROR_CHARS) -> str:
    """Collapse an error value to a single bounded line."""
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit]


def _model_available() -> bool:
    """Whether a text-completion backend is configured at all.

    Checked before the call so a workspace without credentials degrades to
    ``unavailable`` instantly instead of paying a transport timeout on
    every review.
    """
    try:
        from packages.core.ai.runtime import runtime_text_completion_platform_configured

        return bool(runtime_text_completion_platform_configured())
    except Exception:  # noqa: BLE001 — availability probe never raises
        return False


def _parse_json_array(raw: str | None) -> list | None:
    """Parse a model response into a JSON array, or ``None``.

    Tolerates a markdown fence around the payload (some models add one
    despite the prompt) but nothing else: anything that is not a JSON
    array is rejected outright.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — malformed output is a None, not a raise
        return None
    return parsed if isinstance(parsed, list) else None


def _clean_label(value: Any) -> str | None:
    """A label must be a non-empty short string of observed fact."""
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    if not label:
        return None
    return label[:MAX_LABEL_CHARS]


def _clean_count(value: Any) -> int | None:
    # bool is an int subclass — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


async def _run_l1(
    *,
    instruction: str,
    payload: list[dict],
    output_schema_hint: str,
    entity_id: str | None,
    workspace_id: str | None,
) -> list | None:
    """The single LLM round-trip shared by both L1 helpers. Never raises."""
    if not _model_available():
        return None
    try:
        from packages.core.ai.runtime import runtime_execute_consolidator_l1_completion

        result = await runtime_execute_consolidator_l1_completion(
            instruction=instruction,
            payload=payload,
            output_schema_hint=output_schema_hint,
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
    except Exception:  # noqa: BLE001 — L1 is best-effort by contract
        logger.warning("consolidator L1 call failed (ignored)", exc_info=True)
        return None
    return _parse_json_array(getattr(result, "content", None))


async def summarize_failure_clusters(
    items: list[dict],
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict] | None:
    """M4.2 — cluster unstructured failure reasons into neutral labels.

    ``items`` are compact anonymized failure records
    ``{"count": int, "sample_error": str, "service_key": str}`` — no ids,
    no payloads, only the error text the executor already recorded,
    truncated by the caller.

    Returns ``[{"cluster": str, "count": int, "member_indexes": [int]}]``
    with ``member_indexes`` guaranteed to be in range and non-overlapping,
    or ``None`` when L1 is disabled / no model is configured / the call or
    its output failed validation.
    """
    if not l1_enabled():
        return None
    records = [
        {
            "count": _clean_count(item.get("count")) or 1,
            "sample_error": truncate_error(item.get("sample_error")),
            "service_key": str(item.get("service_key") or ""),
        }
        for item in (items or [])[:MAX_L1_INPUT_RECORDS]
        if isinstance(item, dict)
    ]
    if not records:
        return None

    parsed = await _run_l1(
        instruction=_FAILURE_INSTRUCTION,
        payload=records,
        output_schema_hint=_FAILURE_SCHEMA_HINT,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if parsed is None:
        return None

    clusters: list[dict] = []
    seen: set[int] = set()
    for entry in parsed[:MAX_L1_OUTPUT_GROUPS]:
        if not isinstance(entry, dict):
            return None  # strict schema: one bad row rejects the response
        label = _clean_label(entry.get("cluster"))
        count = _clean_count(entry.get("count"))
        raw_indexes = entry.get("member_indexes")
        if label is None or count is None or not isinstance(raw_indexes, list):
            return None
        indexes: list[int] = []
        for index in raw_indexes:
            if isinstance(index, bool) or not isinstance(index, int):
                return None
            if not 0 <= index < len(records) or index in seen:
                continue  # hallucinated / duplicated index — drop the member
            seen.add(index)
            indexes.append(index)
        if not indexes:
            continue
        clusters.append({
            "cluster": label,
            "count": count,
            "member_indexes": indexes,
        })
    return clusters


async def summarize_edit_patterns(
    items: list[dict],
    *,
    entity_id: str | None = None,
    workspace_id: str | None = None,
) -> list[dict] | None:
    """M4.5 — induce the common shape of repeated human edits.

    **M9.6 privacy boundary.** ``items`` are field-name/count aggregates
    ONLY — ``[{"field": str, "count": int}]``. Raw human text (old/new
    values, comments, titles) and per-person identifiers (participant id,
    user id, name, role) must never reach this function, and it strips
    anything else a caller passes: only ``field`` and ``count`` survive
    into the prompt. Human data is a *count* here, never a scorecard and
    never a quotation.

    Returns ``[{"pattern": str, "count": int}]`` or ``None``.
    """
    if not l1_enabled():
        return None
    records: list[dict] = []
    for item in (items or [])[:MAX_L1_INPUT_RECORDS]:
        if not isinstance(item, dict):
            continue
        field = _clean_label(item.get("field"))
        count = _clean_count(item.get("count"))
        if field is None or count is None:
            continue
        # Whitelist projection — never pass through extra caller keys.
        records.append({"field": field, "count": count})
    if not records:
        return None

    parsed = await _run_l1(
        instruction=_EDIT_INSTRUCTION,
        payload=records,
        output_schema_hint=_EDIT_SCHEMA_HINT,
        entity_id=entity_id,
        workspace_id=workspace_id,
    )
    if parsed is None:
        return None

    patterns: list[dict] = []
    for entry in parsed[:MAX_L1_OUTPUT_GROUPS]:
        if not isinstance(entry, dict):
            return None
        label = _clean_label(entry.get("pattern"))
        count = _clean_count(entry.get("count"))
        if label is None or count is None:
            return None
        patterns.append({"pattern": label, "count": count})
    return patterns
