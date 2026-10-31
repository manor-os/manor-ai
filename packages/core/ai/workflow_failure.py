"""Platform-owned failure headlines for Workflow runs.

A failed step's ``error`` is the headline every run surface shows: the run
panel, the Chat run card, mobile, notifications. Model-written text reaches it
too, as an Agent summary or a Stop template that interpolates one, and a model
may paste a tool's raw error JSON there. The runner settles every failed step
result here, so what a user reads is owned by the platform:

* a Manor platform fault the host observed (``internal_error: true`` in a
  tool result) is shown as a localized platform message, whatever the model
  wrote; the model's text and the raw tool payload move to ``details``;
* a failure the host itself coded (``code``) that has a platform message is
  shown as that localized message; its raw text moves to ``error_details``;
* a headline that names an internal record ID means nothing to a user, so it
  becomes a plain "stopped" line and the raw text moves to ``error_details``;
* a long headline (an Agent explaining at length) keeps its first sentence;
  the whole text moves to ``error_details``;
* any other headline keeps its text, with each embedded JSON object replaced
  by its readable message and the original kept in ``error_details``.
"""
from __future__ import annotations

import json
import re
from typing import Any

from packages.core.i18n import t

PLATFORM_FAILURE_KIND = "platform_error"
PLATFORM_FAILURE_LOCALES = ("en", "zh")
_HEADLINE_MAX_CHARS = 600
# Past this a headline is an explanation: its first sentence is the headline.
_HEADLINE_SHORT_CHARS = 160
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")
_BLOB_MESSAGE_MAX_CHARS = 200
# The start of a JSON object, plain or string-escaped ({\"error\": ...}).
_JSON_OBJECT_START = re.compile(r'\{\s*\\?"[^"\n]{1,80}\\?"\s*:')
_FENCE = re.compile(r"```[A-Za-z]*")
_TRAILING_SEPARATORS = " \t\r\n:：,，;；-—|"


PLATFORM_ERROR_MESSAGE_KEY = "workflow.failure.platform_error"
CONTEXT_OVERFLOW_MESSAGE_KEY = "workflow.failure.context_budget_exceeded"
TECHNICAL_FAILURE_MESSAGE_KEY = "workflow.failure.technical"
REPLACED_MESSAGE_KEY = "workflow.failure.replaced"
# Host failure codes whose raw text is about internal records.
HOST_FAILURE_CODES = frozenset({
    "workflow_project_conflict",
    "workflow_project_failed",
    "workflow_project_state_validation_failed",
})
# A ULID or a UUID: an internal record ID, never something a user can act on.
_RECORD_ID = re.compile(
    r"\b(?:[0-9A-HJKMNP-TV-Z]{26}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)


def names_record_id(text: str) -> bool:
    return bool(_RECORD_ID.search(text or ""))


def workflow_platform_failure(
    fault: dict[str, Any],
    *,
    step: dict[str, Any],
    details: dict[str, Any] | None = None,
    message_key: str = PLATFORM_ERROR_MESSAGE_KEY,
) -> dict[str, Any]:
    """The structured failure a step records for a host-observed runtime fault.

    ``fault`` is ``runtime_unresolved_platform_error`` / ``runtime_platform_error``
    output (a stable code plus the redacted raw tool payload), or a runtime
    error the agent loop itself returned, such as a context overflow.
    """
    step_name = str(step.get("name") or step.get("id") or "").strip()
    return {
        "kind": PLATFORM_FAILURE_KIND,
        "code": str(fault.get("code") or "internal_error"),
        "step_id": step.get("id"),
        "step_name": step_name,
        "messages": {
            locale: t(message_key, locale, step=step_name)
            for locale in PLATFORM_FAILURE_LOCALES
        },
        "next_action": "retry_after_fix",
        "details": {
            **{key: fault[key] for key in ("tool", "operation", "call_id") if fault.get(key)},
            **({"tool_error": fault["details"]} if fault.get("details") else {}),
            **(details or {}),
        },
    }


def platform_failure_message(failure: dict[str, Any], locale: str | None) -> str:
    messages = failure.get("messages") if isinstance(failure.get("messages"), dict) else {}
    return str(messages.get(locale or "en") or messages.get("en") or "")


def _platform_fault_in(value: Any) -> dict[str, Any] | None:
    from packages.core.ai.runtime.tool_error_contract import runtime_platform_error

    return runtime_platform_error("", value) if isinstance(value, dict) else None


def _blob_message(value: Any) -> str:
    """The readable message of one embedded JSON value, or ''."""
    if isinstance(value, list):
        return "; ".join(filter(None, (_blob_message(item) for item in value[:3])))
    if not isinstance(value, dict):
        return ""
    nested = value.get("error") if isinstance(value.get("error"), dict) else {}
    for candidate in (
        value.get("message"), nested.get("message"), value.get("reason"),
        value.get("detail"), value.get("error"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            text, _ = readable_failure_text(candidate)
            text = " ".join(text.split())
            if len(text) > _BLOB_MESSAGE_MAX_CHARS:
                text = text[:_BLOB_MESSAGE_MAX_CHARS - 1].rstrip() + "…"
            return text
    return ""


def readable_failure_text(text: str) -> tuple[str, list[Any]]:
    """``text`` with each embedded JSON object replaced by its readable message.

    Returns the readable text and the raw values it replaced. An object that
    does not parse (a model truncated it) ends the readable text there.
    """
    decoder = json.JSONDecoder()
    parts: list[str] = []
    blobs: list[Any] = []
    position = 0
    while (match := _JSON_OBJECT_START.search(text, position)) is not None:
        parts.append(text[position:match.start()])
        try:
            value, position = decoder.raw_decode(text, match.start())
        except ValueError:
            blobs.append(text[match.start():])
            position = len(text)
            break
        blobs.append(value)
        parts.append(_blob_message(value))
    parts.append(text[position:])
    if not blobs:
        return text, []
    # A headline is one line; a fence that wrapped a blob goes with it.
    readable = " ".join(_FENCE.sub(" ", "".join(parts)).split()).rstrip(_TRAILING_SEPARATORS)
    return readable, blobs


def latest_platform_failure(step_results: Any, *, exclude_step_id: str | None = None) -> dict | None:
    """The platform failure most recently recorded by another step of the run."""
    found = None
    for step_id, result in (step_results or {}).items() if isinstance(step_results, dict) else ():
        if step_id == exclude_step_id or not isinstance(result, dict):
            continue
        failure = result.get("platform_failure")
        if isinstance(failure, dict) and failure.get("kind") == PLATFORM_FAILURE_KIND:
            found = failure
    return found


def settle_workflow_failure(
    step: dict[str, Any],
    result: dict[str, Any],
    *,
    step_results: Any,
    locale: str | None,
) -> dict[str, Any]:
    """The failed step result with a headline the platform owns.

    A Stop node reports the outcome of the steps it routes from, so it adopts
    the run's latest platform failure; its rendered message stays in details.
    """
    if not isinstance(result, dict) or result.get("status") != "failed":
        return result
    original = result.get("error")
    # A structured (dict) error is host-authored and rendered field by field;
    # only text can carry a pasted blob.
    readable, blobs = readable_failure_text(original) if isinstance(original, str) else ("", [])
    failure = result.get("platform_failure")
    if not isinstance(failure, dict) and step.get("type") == "stop":
        failure = latest_platform_failure(step_results, exclude_step_id=step.get("id"))
    if not isinstance(failure, dict):
        fault = next(filter(None, (_platform_fault_in(value) for value in [*blobs, original])), None)
        if fault is not None:
            failure = workflow_platform_failure(fault, step=step)
    if isinstance(failure, dict):
        headline = platform_failure_message(failure, locale)
        details = dict(failure.get("details") or {})
        if original not in (None, "", headline):
            details["step_error"] = original
        return {**result, "error": headline, "platform_failure": {**failure, "details": details}}
    code = str(result.get("code") or "")
    headline = readable if blobs else (original if isinstance(original, str) else "")
    if code in HOST_FAILURE_CODES or names_record_id(headline):
        key = f"workflow.failure.{code}" if code in HOST_FAILURE_CODES else TECHNICAL_FAILURE_MESSAGE_KEY
        return {**result, "error": t(key, locale, step=_step_name(step)), "error_details": original}
    if not blobs:
        short = _first_sentence(original) if isinstance(original, str) else original
        return result if short == original else {**result, "error": short, "error_details": original}
    if len(readable) > _HEADLINE_MAX_CHARS:
        readable = readable[:_HEADLINE_MAX_CHARS - 1].rstrip() + "…"
    return {**result, "error": readable or _step_failed(step), "error_details": original}


def user_facing_step_error(step: dict[str, Any], result: dict[str, Any], locale: str | None = None) -> Any:
    """A failed step's error as a user reads it, for records settled before
    host-coded failures and record IDs were kept out of headlines."""
    error = result.get("error")
    if not isinstance(error, str) or not error:
        return error
    if str(result.get("code") or "") in HOST_FAILURE_CODES or names_record_id(error) \
            or len(error) > _HEADLINE_SHORT_CHARS:
        return settle_workflow_failure(
            step, {**result, "status": "failed"}, step_results={}, locale=locale,
        )["error"]
    return error


def run_failure_step(run: Any) -> dict[str, Any]:
    """The step a run stopped at, with the name its definition gave it."""
    step_id = str(getattr(run, "current_step_id", "") or "")
    snapshot = run.definition_snapshot if isinstance(getattr(run, "definition_snapshot", None), dict) else {}
    for node in snapshot.get("nodes") or []:
        if isinstance(node, dict) and str(node.get("id") or "") == step_id:
            return {"id": step_id, "name": node.get("name")}
    return {"id": step_id}


# Why the platform stopped a run whose worker went away mid-step (see
# ``workflow_claim_recovery``): its raw text is about execution claims.
INTERRUPTED_RUN_OUTCOMES = frozenset({"failed_effects_unknown", "failed_recovery_limit"})
INTERRUPTED_MESSAGE_KEY = "workflow.failure.interrupted"


def user_facing_run_error(run: Any, locale: str | None = None) -> Any:
    """``run.error`` as a user reads it (see ``user_facing_step_error``)."""
    results = run.step_results if isinstance(run.step_results, dict) else {}
    step = run_failure_step(run)
    from packages.core.services.workflow_claim_recovery import WORKFLOW_EXECUTION_RECOVERY_TRIGGER_FIELD

    trigger_data = run.trigger_data if isinstance(getattr(run, "trigger_data", None), dict) else {}
    recovery = trigger_data.get(WORKFLOW_EXECUTION_RECOVERY_TRIGGER_FIELD)
    if run.status == "failed" and isinstance(recovery, dict) \
            and recovery.get("outcome") in INTERRUPTED_RUN_OUTCOMES:
        return t(INTERRUPTED_MESSAGE_KEY, locale, step=_step_name(step))
    result = results.get(step["id"]) if isinstance(results.get(step["id"]), dict) else {}
    # The step's code says what its own error was; a run closed for another
    # reason (replaced by a newer version) keeps the run's message.
    code = result.get("code") if run.status == "failed" else None
    return user_facing_step_error(step, {"error": run.error, "code": code}, locale)


def _first_sentence(text: str) -> str:
    """``text`` itself when short; else its first sentence, cut to one line."""
    if len(text) <= _HEADLINE_SHORT_CHARS:
        return text
    text = " ".join(text.split())
    first = _SENTENCE_END.split(text, maxsplit=1)[0].strip()
    if len(first) > _HEADLINE_SHORT_CHARS:
        first = first[:_HEADLINE_SHORT_CHARS - 1].rstrip(_TRAILING_SEPARATORS) + "…"
    return first


def _step_name(step: dict[str, Any]) -> str:
    return str(step.get("name") or step.get("id") or "").strip()


def _step_failed(step: dict[str, Any]) -> str:
    return f"Step \"{_step_name(step)}\" failed"


def workflow_run_failure_headline(
    step: dict[str, Any], result: dict[str, Any], *, fallback: str | None = None,
) -> str:
    """The run-level headline of a settled failed step result: always prose."""
    from packages.core.services.workflow_run_trace import summarize_trace_text

    error = result.get("error")
    if not isinstance(error, str):
        error = _blob_message(error) if error is not None else ""
    return summarize_trace_text(error, fallback=fallback or _step_failed(step))
