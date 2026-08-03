"""Step-result envelopes.

Two related contracts live here:

  * ``Success`` / ``Failure`` — the typed in-process envelope every worker
    action returns.
  * The StepResult envelope — the fixed default *output schema* for
    llm/subagent plan steps. Planner-invented per-step JSON schemas are the
    root cause of the OutputSchemaError failure class: the model guesses
    field names the execution can't honor. The envelope replaces those
    guesses with one permissive contract plus a deterministic constructor
    that maps ANY raw worker output onto it, giving a hard guarantee:
    ``build_step_result_envelope`` never raises and its result always
    validates against ``step_result_envelope_schema()``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Union

from jsonschema import Draft202012Validator


@dataclass(frozen=True)
class Success:
    data: Any
    ok: bool = True

    def to_dict(self) -> dict:
        return {"ok": True, "data": self.data}


@dataclass(frozen=True)
class Failure:
    reason: str
    detail: Optional[dict] = None
    ok: bool = False

    def to_dict(self) -> dict:
        out: dict = {"ok": False, "reason": self.reason}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


StepResult = Union[Success, Failure]


# ── StepResult envelope (default llm/subagent output contract) ────────

STEP_RESULT_ENVELOPE_ID = "manor:step-result-envelope/v1"


class StepResultStatus(str, Enum):
    """The ONE status vocabulary for step outcomes.

    Every layer that reads or writes a step's outcome — the ``submit_result``
    tool an agent calls, the envelope schema, the dispatcher's success gate,
    the executor's blocker check — must use these members. A second
    vocabulary anywhere means a word gets silently dropped in translation:
    ``submit_result`` used to offer agents ``done | partial | blocked`` while
    the envelope only accepted ``succeeded | partial | failed``, so an agent
    reporting ``done`` was downgraded to ``partial`` (the inference fallback)
    and ``blocked`` — an explicit "I could not proceed" — was recorded as
    partial success.
    """

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        return tuple(member.value for member in cls)


#: Words a model may naturally produce, mapped onto the canonical vocabulary.
#: This is normalization of free text, NOT a second vocabulary: the enum above
#: is what every schema advertises and every gate compares against.
STEP_RESULT_STATUS_ALIASES: dict[str, StepResultStatus] = {
    "done": StepResultStatus.SUCCEEDED,
    "complete": StepResultStatus.SUCCEEDED,
    "completed": StepResultStatus.SUCCEEDED,
    "success": StepResultStatus.SUCCEEDED,
    "ok": StepResultStatus.SUCCEEDED,
    "blocked": StepResultStatus.FAILED,
    "error": StepResultStatus.FAILED,
    "failure": StepResultStatus.FAILED,
    "incomplete": StepResultStatus.PARTIAL,
}


def normalize_step_result_status(value: Any) -> Optional[StepResultStatus]:
    """Map a raw status onto the canonical enum, or None when unrecognized.

    None means "the caller said nothing usable" — the envelope builder then
    infers a status from the payload rather than trusting an unknown word.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        return StepResultStatus(text)
    except ValueError:
        return STEP_RESULT_STATUS_ALIASES.get(text)


_VALID_STATUSES = StepResultStatus.values()
_SUMMARY_MAX = 200
# Keys whose value is "the content" of a loose dict result, in preference order.
_TEXTISH_KEYS = ("text", "result", "content")


def step_result_envelope_schema() -> dict:
    return {
        "$id": STEP_RESULT_ENVELOPE_ID,
        "type": "object",
        "required": ["status", "summary"],
        "additionalProperties": True,
        "properties": {
            "status": {"enum": list(_VALID_STATUSES)},
            # One line: what was done.
            "summary": {"type": "string"},
            "outputs": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name"],
                            "properties": {
                                "name": {"type": "string"},
                                "path": {"type": "string"},
                                "url": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    # Free-form structured payload — deliberately unconstrained.
                    "data": {},
                },
            },
            "progress": {
                "type": "object",
                "properties": {
                    "done": {"type": "integer"},
                    "total": {"type": "integer"},
                    "unit": {"type": "string"},
                },
            },
            "failure": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "blockers": {"type": "array", "items": {"type": "string"}},
                    "retryable": {"type": "boolean"},
                },
            },
            "next_steps": {"type": "array", "items": {"type": "string"}},
        },
    }


_VALIDATOR = Draft202012Validator(step_result_envelope_schema())


def is_step_result_envelope_schema(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("$id") == STEP_RESULT_ENVELOPE_ID


def envelope_indicates_failure(result: Any) -> bool:
    """True when an envelope's ``status`` says the step did not deliver.

    ``status`` is the envelope's success/failure *control signal*, not
    decoration: ``build_step_result_envelope`` deliberately never infers
    "succeeded", so a step whose output carried no content lands on "failed"
    and must be treated as a failed step. ``partial`` is NOT a failure —
    partial output is usable output, and downstream steps can consume it.

    Callers must gate on ``is_step_result_envelope_schema`` first: this reads
    a `status` key that means something entirely different on a custom/action
    schema (a publish receipt's provider status, say).
    """
    return isinstance(result, dict) and result.get("status") == "failed"


def build_step_result_envelope(raw: Any) -> dict:
    """Deterministically wrap ANY raw step output in a valid envelope.

    Never raises, and the returned dict always validates against
    ``step_result_envelope_schema()`` — this is what makes dispatcher output
    validation a tautology for envelope steps. Never invents success: an
    inferred status is at most "partial"; "succeeded" only passes through
    when the raw output explicitly claimed it.
    """
    try:
        envelope = _build(raw)
        if _VALIDATOR.is_valid(envelope):
            return envelope
    except Exception:  # noqa: BLE001 — the never-raise guarantee IS the contract
        pass
    return {
        "status": "failed",
        "summary": "step produced unrenderable output",
        "failure": {"reason": "unrenderable output"},
    }


def _envelope_from_json_text(raw: str) -> Optional[dict]:
    """Return the envelope a JSON string carries, when it already is one.

    Structural: the text must parse to an object that VALIDATES against the
    envelope schema (so its status is a real enum member). Anything else —
    prose, a JSON list, an object with a different shape — returns None and
    is wrapped as text like any other output.
    """
    text = raw.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and _VALIDATOR.is_valid(parsed):
        return parsed
    return None


def _build(raw: Any) -> dict:
    if raw is None or raw == {} or (isinstance(raw, str) and not raw.strip()):
        return {
            "status": "failed",
            "summary": "step produced no output",
            "failure": {"reason": "empty output"},
        }
    if isinstance(raw, dict):
        if _VALIDATOR.is_valid(raw):
            return raw
        return _build_from_dict(raw)
    if isinstance(raw, str):
        # A model that emits its envelope as text (rather than through a tool
        # call) used to get double-wrapped: the real envelope — status and all
        # — was buried as a string in outputs.text while the OUTER status was
        # inferred as "partial". The UI then rendered the inner status and the
        # supervisor read the outer one, disagreeing about the same step.
        # Structural check via the schema validator, never keyword matching.
        unwrapped = _envelope_from_json_text(raw)
        if unwrapped is not None:
            return unwrapped
        return {
            "status": StepResultStatus.PARTIAL.value,
            "summary": _truncate(raw),
            "outputs": {"text": raw},
        }
    if isinstance(raw, (list, bool, int, float)):
        return {
            "status": "partial",
            "summary": _truncate(json.dumps(raw, ensure_ascii=False, default=str)),
            "outputs": {"data": raw},
        }
    # Anything else (bytes, objects, ...) — stringify and treat as text.
    return _build(str(raw))


def _build_from_dict(raw: dict) -> dict:
    out = dict(raw)

    declared = normalize_step_result_status(out.get("status"))
    if declared is None:
        # Nothing usable was declared — infer, and never infer "succeeded":
        # an error key or content-free dict means failed, anything else is at
        # most partial.
        declared = (
            StepResultStatus.FAILED
            if ("error" in out or not _has_content(out))
            else StepResultStatus.PARTIAL
        )
    out["status"] = declared.value

    outputs_raw = out.pop("outputs", None)
    outputs = dict(outputs_raw) if isinstance(outputs_raw, dict) else {}
    if outputs_raw is not None and not isinstance(outputs_raw, dict):
        outputs.setdefault("data", outputs_raw)

    # Summary is picked from the pre-move dict so text-ish content can seed it.
    out["summary"] = _pick_summary(raw)

    # Move recognizable content onto the envelope's canonical slots.
    text_value = _pop_first_textish(out)
    if text_value is not None and not isinstance(outputs.get("text"), str):
        outputs["text"] = text_value
    if not isinstance(outputs.get("files"), list):
        files = _sanitize_files(out.get("files"))
        if files is not None:
            outputs["files"] = files
            out.pop("files", None)

    _sanitize_outputs(outputs)
    if outputs:
        out["outputs"] = outputs
    _sanitize_progress(out)
    _sanitize_failure(out)
    _sanitize_next_steps(out)
    return out


def _has_content(raw: dict) -> bool:
    for value in raw.values():
        if value is None:
            continue
        if isinstance(value, (str, list, dict)) and not value:
            continue
        return True
    return False


def _pop_first_textish(out: dict) -> str | None:
    """Pop and return the first text-ish content value (full, untruncated)."""
    for key in _TEXTISH_KEYS:
        value = out.get(key)
        if isinstance(value, str) and value.strip():
            out.pop(key)
            return value
    return None


def _pick_summary(raw: dict) -> str:
    summary = raw.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary
    for key in _TEXTISH_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if text.strip():
            return _truncate(text)
    return "no structured summary provided"


def _sanitize_files(value: Any) -> list[dict] | None:
    """Envelope-valid file entries, or None when the value isn't a file list.

    Entries without a usable name are dropped rather than failing the whole
    envelope; non-string values on the typed keys are dropped the same way.
    """
    if not isinstance(value, list) or not value:
        return None
    if not any(isinstance(item, dict) and item.get("name") for item in value):
        return None
    files: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        entry = dict(item)
        for key in ("path", "url", "description"):
            if key in entry and not isinstance(entry[key], str):
                entry.pop(key)
        files.append(entry)
    return files or None


def _sanitize_outputs(outputs: dict) -> None:
    if "text" in outputs and not isinstance(outputs["text"], str):
        outputs.setdefault("data", outputs.pop("text"))
    if "files" in outputs:
        files = _sanitize_files(outputs["files"])
        if files is None:
            outputs.setdefault("data", outputs.pop("files"))
        else:
            outputs["files"] = files


def _sanitize_progress(out: dict) -> None:
    progress = out.get("progress")
    if not isinstance(progress, dict):
        out.pop("progress", None)
        return
    progress = dict(progress)
    for key in ("done", "total"):
        if key in progress and (isinstance(progress[key], bool) or not isinstance(progress[key], int)):
            progress.pop(key)
    if "unit" in progress and not isinstance(progress["unit"], str):
        progress.pop("unit")
    out["progress"] = progress


def _sanitize_failure(out: dict) -> None:
    failure = out.get("failure")
    if not isinstance(failure, dict):
        out.pop("failure", None)
        return
    failure = dict(failure)
    if "reason" in failure and not isinstance(failure["reason"], str):
        failure.pop("reason")
    if "blockers" in failure:
        blockers = failure["blockers"]
        if isinstance(blockers, list):
            failure["blockers"] = [b for b in blockers if isinstance(b, str)]
        else:
            failure.pop("blockers")
    if "retryable" in failure and not isinstance(failure["retryable"], bool):
        failure.pop("retryable")
    out["failure"] = failure


def _sanitize_next_steps(out: dict) -> None:
    steps = out.get("next_steps")
    if not isinstance(steps, list):
        out.pop("next_steps", None)
        return
    out["next_steps"] = [s for s in steps if isinstance(s, str)]


def _truncate(text: str, limit: int = _SUMMARY_MAX) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit]
