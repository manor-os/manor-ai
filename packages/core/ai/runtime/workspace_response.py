"""Workspace Chat candidate replies and server-rendered execution facts.

The typed execution section is verified. Ordinary answer text is still model
prose: a schema cannot prove its semantic truth or classify every false claim.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from packages.core.ai.runtime.final_response import (
    FinalResponseAction, FinalResponseDecision, PROGRESS_RESPONSE_SCHEMA,
    STRUCTURED_RESPONSE_INSTRUCTIONS, parse_response_object, response_schema_error, structured_response_format,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.services.workspace_execution_evidence import (
    ExecutionEvidenceScope, read_workspace_task_evidence,
)


WORKSPACE_RESPONSE_INSTRUCTIONS = """<workspace-response-contract>
Your final reply is one JSON object, not Markdown fences. Tool calls remain
ordinary tool calls; do not put calls inside this object.
For discussion, explanations, questions or an honest blocker use:
{"kind":"answer","text":"your full user-facing answer"}.
Zero-tool answers are valid. An answer is not an execution receipt. Do not put
claims of dispatch, execution, saved delivery or background progress in answer
text. Do not copy progress from older messages or unrelated scheduled work.
For Task execution/progress use only:
{"kind":"execution_summary","references":[{"kind":"task","id":"exact task id","scope":"current_turn","revision":"host supplied revision"}]}.
Use scope=existing_record when reporting an earlier Task, not a new dispatch.
This section has no model-authored status or summary: the server renders real
state. It supports Task records; other tool results may be explained accurately
as an answer but must not be described as verified Task delivery.
For direct tool or child Agent work, check the original request against the
returned execution receipts before answering. A completed tool, provider job
or child loop proves only that operation finished, not that the full request
was satisfied. Preserve warnings, omitted features and unfinished work. If
authorized work remains, continue using tools in this loop; otherwise explain
the specific limitation without claiming delivery. For edits, preserve the
requested source and changes when delegating; a new generated asset is not
evidence that the original was edited. Separate input assets are not evidence
of a combined output. Inspect the final artifact with available tools before
claiming requested properties; clearly identify anything not verified.
If no revision has been observed, use an empty revision string: the final-response validator
returns current authorized facts through the ordinary error contract, then
use those exact references/revisions. If work has not been requested through a
tool, continue executing within current permissions instead of inventing a
Task id. A revision/format error never authorizes repeating a previous write.
Missing, expired or unknown effects retain normal approval/reconciliation.
</workspace-response-contract>""" + "\n" + STRUCTURED_RESPONSE_INSTRUCTIONS

_SCHEMA = {
    "oneOf": [
        {"type": "object", "required": ["kind", "text"], "additionalProperties": False,
         "properties": {"kind": {"const": "answer"}, "text": {"type": "string", "minLength": 1}}},
        {"type": "object", "required": ["kind", "references"], "additionalProperties": False,
         "properties": {"kind": {"const": "execution_summary"}, "references": {
             "type": "array", "maxItems": 8,
             "items": {"type": "object", "required": ["kind", "id", "scope"],
                       "additionalProperties": False, "properties": {
                           "kind": {"const": "task"},
                           "id": {"type": "string", "minLength": 1, "maxLength": 26},
                           "scope": {"enum": ["current_turn", "existing_record"]},
                           "revision": {"type": "string", "maxLength": 64},
                       }},
         }}},
    ],
}
WORKSPACE_EXECUTION_SUMMARY_SCHEMA = _SCHEMA["oneOf"][1]

_LABELS = {
    "not_started": ("已创建，尚无执行记录", "Created; no execution recorded"),
    "accepted": ("已接收，等待派发", "Accepted; waiting for dispatch"),
    "assigned": ("已分配，等待执行", "Assigned; waiting for execution"),
    "planning": ("协调器正在处理", "Coordinator is processing"),
    "awaiting_coordinator": ("已有步骤完成，等待继续处理", "Steps completed; awaiting continuation"),
    "running": ("正在执行", "Executing"),
    "waiting": ("已暂停，等待中", "Paused; waiting"),
    "waiting_input": ("等待输入或审批", "Waiting for input or approval"),
    "blocked": ("当前受阻", "Currently blocked"),
    "delivered": ("已验收并完成交付", "Accepted and delivered"),
    "completed_unverified": ("标记为完成；未找到可验证的交付验收", "Marked complete; verified delivery acceptance unavailable"),
    "failed": ("执行失败", "Execution failed"),
    "cancelled": ("已取消", "Cancelled"),
}


def _safe_label(text: str) -> str:
    label = escape(" ".join(str(text).split()), quote=True)
    return "".join(f"\\{char}" if char in "\\`*_{}[]()#+-.!|>" else char for char in label)


class WorkspaceChatResponseContract:
    instructions = WORKSPACE_RESPONSE_INSTRUCTIONS
    response_format = structured_response_format("workspace_reply", _SCHEMA)
    tool_response_format = structured_response_format("workspace_reply", {
        "anyOf": [*_SCHEMA["oneOf"], PROGRESS_RESPONSE_SCHEMA],
    })

    def __init__(
        self, scope: ExecutionEvidenceScope, *, locale: str | None = None,
        reader: Callable[[ExecutionEvidenceScope, list[dict[str, str]]], Awaitable[list[dict[str, Any]]]] = read_workspace_task_evidence,
    ) -> None:
        self.scope = scope
        self.locale = locale or "en"
        self._reader = reader

    def _reject(
        self, code: str, message: str, *, facts: list[dict[str, Any]] | None = None,
        continue_execution: bool = False, validation_error: dict[str, Any] | None = None,
    ) -> FinalResponseDecision:
        action = (
            FinalResponseAction.CONTINUE_EXECUTION if continue_execution
            else FinalResponseAction.REVISE_REPLY
        )
        return FinalResponseDecision(action, diagnostic=tool_error_payload(
            tool_name="final_response", code=code, message=message,
            details={"response_action": action.value, "facts": facts or [],
                     **({"validation_error": validation_error} if validation_error else {}),
                     "response_contract": self.instructions,
                     "repair_instruction": (
                         "Continue within the current tool and approval boundaries, or return a final reply matching this contract."
                         if continue_execution else
                         "This next round only revises the final reply. Return one complete JSON object matching this contract; do not call tools, emit a stream marker, or repeat any action."
                     ),
                     "constraint": "Do not repeat a settled write or bypass approval. Existing tools and effect controls remain authoritative."},
        ))

    async def validate(self, content: str) -> FinalResponseDecision:
        candidate, validation_error = parse_response_object(content)
        if validation_error:
            return self._reject("response_contract_invalid", "Return one JSON object matching the Workspace response contract: answer with text, or execution_summary with references.", validation_error=validation_error)
        validation_error = response_schema_error(candidate, _SCHEMA)
        if validation_error:
            return self._reject("response_contract_invalid", "Correct the field described by validation_error. Use kind=answer with text, or kind=execution_summary with references[{kind:task,id,scope,revision?}]; no extra fields or model-authored status.", validation_error=validation_error)
        if candidate["kind"] == "answer":
            if not candidate["text"].strip():
                return self._reject("response_contract_invalid", "Answer text must not be blank.", validation_error={"reason": "blank_text", "path": ["text"], "rule": "nonblank"})
            return FinalResponseDecision(FinalResponseAction.ACCEPT, content=candidate["text"])
        references = candidate["references"]
        if not references:
            return self._reject(
                "execution_evidence_required",
                "No execution reference was supplied. Continue the requested work with available tools, or give an honest answer without an execution claim.",
                continue_execution=True,
            )
        if len({ref["id"] for ref in references}) != len(references):
            return self._reject("execution_reference_invalid", "Task references must be unique.")
        try:
            facts = await self._reader(self.scope, references)
        except Exception:
            # Lookup failures are not evidence of absent work or permission to
            # repeat it. No database/provider details enter the model response.
            return self._reject("execution_evidence_unavailable", "Execution records could not be verified. Do not repeat the work; report that its status is currently unavailable.")
        by_id = {fact["id"]: fact for fact in facts}
        if any(ref["id"] not in by_id for ref in references):
            return self._reject("execution_reference_unavailable", "An execution reference is unavailable in this authorized Workspace. Do not infer that no effect happened or repeat it.")
        if any(ref["scope"] == "current_turn" and by_id[ref["id"]]["origin"] != "current_turn" for ref in references):
            return self._reject("execution_origin_mismatch", "An earlier or unrelated Task is not proof of dispatch from this turn. Use existing_record only to report that earlier work, or continue the requested work within current permissions.", facts=facts, continue_execution=True)
        if any(ref.get("revision") != by_id[ref["id"]]["revision"] for ref in references):
            return self._reject("execution_revision_required", "Read these current host facts and use their exact revisions. Changed execution state requires a refreshed summary, not replaying the work.", facts=facts)
        ordered = [by_id[ref["id"]] for ref in references]
        return FinalResponseDecision(
            FinalResponseAction.ACCEPT,
            content=render_verified_task_status(references, ordered, locale=self.locale),
            evidence=tuple(ordered),
        )


def render_verified_task_status(
    references: list[dict[str, Any]], facts: list[dict[str, Any]], *, locale: str,
) -> str:
    """Server-written Task status lines; no model-authored state or summary."""
    chinese = locale.lower().startswith("zh")
    header = "已核实的任务状态：" if chinese else "Verified task status:"
    lines = [header, ""]
    for ref, fact in zip(references, facts, strict=True):
        state = _LABELS[fact["state"]][0 if chinese else 1]
        waiting = fact.get("waiting") if isinstance(fact.get("waiting"), dict) else None
        if waiting:
            if waiting.get("wake_at"):
                detail = (f"至 {waiting['wake_at']}" if chinese else f"until {waiting['wake_at']}")
            else:
                count = len(waiting.get("job_ids") or [])
                detail = (f"{count} 个媒体任务" if chinese else f"for {count} media job(s)")
            state = f"{state} {_safe_label(detail)}"
        prefix = "已有任务 · " if chinese else "Existing task · "
        if ref["scope"] == "current_turn":
            prefix = "本轮创建 · " if chinese else "Created this turn · "
        lines.append(f"- [{_safe_label(fact['title'])}]({fact['url']}) — {prefix}{state}")
    return "\n".join(lines)


def workspace_chat_response_contract(ctx: Any) -> WorkspaceChatResponseContract | None:
    """Only the explicit Workspace Chat surface opts into this wire contract."""
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.i18n import get_locale

    envelope = getattr(ctx, "runtime_envelope", None)
    if envelope is None or getattr(envelope, "surface", None) != ChatSurface.WORKSPACE_CHAT:
        return None
    fields = [getattr(envelope, name, None) for name in ("entity_id", "workspace_id", "user_id", "conversation_id")]
    if not all(isinstance(value, str) and value for value in fields):
        return None
    metadata = getattr(envelope, "metadata", None) or {}
    return WorkspaceChatResponseContract(ExecutionEvidenceScope(
        *fields, origin_user_message_id=metadata.get("origin_user_message_id"),
    ), locale=get_locale())
