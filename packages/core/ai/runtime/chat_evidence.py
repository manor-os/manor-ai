"""Current-turn capability observations shared by ordinary and Workspace Chat.

Host observations substantiate attempts, not semantic task success or authority
to repeat effects. Old conversation prose never supplies current evidence.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
import re
from typing import Any

from packages.core.ai.runtime.final_response import (
    FinalResponseAction, FinalResponseContext, FinalResponseDecision, validate_final_response,
    PROGRESS_RESPONSE_SCHEMA, STRUCTURED_RESPONSE_INSTRUCTIONS,
    parse_response_object, response_schema_error, structured_response_format,
)
from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence, runtime_model_view_of_observation
from packages.core.services.file_refs import file_ref, rewrite_reply_file_links, strip_internal_file_links

logger = logging.getLogger(__name__)


_INTERNAL_FORMAT_REPAIR_REPLY = re.compile(
    r"(?:上一(?:条|次)回复.{0,40}(?:系统要求|内部要求).{0,32}(?:JSON|格式))|"
    r"(?:本次将.{0,24}(?:按规定|按要求).{0,16}(?:完整|有效)?\s*JSON)|"
    r"(?:previous (?:reply|response).{0,50}(?:system|required).{0,32}JSON)",
    re.IGNORECASE,
)


_ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "text", "outcome"],
    "properties": {
        "kind": {"const": "answer"}, "text": {"type": "string", "minLength": 1},
        "outcome": {
            "type": "object", "additionalProperties": False,
            "required": ["status", "evidence_refs", "limitations"],
            "properties": {
                "status": {"enum": ["answered", "completed", "partial", "blocked", "not_attempted"]},
                "evidence_refs": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
                "limitations": {"type": "array", "maxItems": 16,
                                "items": {"type": "string", "minLength": 1, "maxLength": 2000}},
                "claims": {
                    "type": "array", "maxItems": 16, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["operation", "status", "evidence_refs"],
                        "properties": {
                            "operation": {"type": "string", "minLength": 1, "maxLength": 200},
                            "status": {"enum": ["completed", "partial", "blocked", "not_attempted"]},
                            "evidence_refs": {"type": "array", "maxItems": 32, "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
}


class ChatEvidenceContract(RuntimeToolEvidence):

    def __init__(self, *, inner=None, requires_execution: bool = False, accept_prose: bool = False):
        super().__init__()
        self.inner = inner
        schema = _ANSWER_SCHEMA
        if inner is not None:
            from packages.core.ai.runtime.workspace_response import WORKSPACE_EXECUTION_SUMMARY_SCHEMA

            schema = {"anyOf": [_ANSWER_SCHEMA, WORKSPACE_EXECUTION_SUMMARY_SCHEMA]}
        self._schema = schema
        self.response_format = structured_response_format("chat_reply", schema)
        self.tool_response_format = structured_response_format("chat_reply", {
            "anyOf": [*(schema.get("anyOf") or [schema]), PROGRESS_RESPONSE_SCHEMA],
        })
        self.requires_execution = requires_execution
        # Follow-ups imitate the visible prose of earlier stored turns; a
        # first reply in prose is a format slip worth repairing.
        self.accept_prose = accept_prose
        # Keep the action loop focused on the request. Detailed attribution
        # errors belong in the existing validator's targeted feedback, rather
        # than a long reply-first instruction competing with task execution.
        self.instructions = ((inner.instructions + "\n") if inner else "") + """<current-turn-evidence>
Use tools to carry out the current request, interpreting references, supplied
inputs and constraints in conversation context. For the same resource, later
user changes replace earlier requested values; preserve the latest values on
continuation unless the user asks to revert. Use the latest explicit resource
selection to resolve references. Observed values on other resources are not
user instructions. Inspect current state when
necessary before changing it. Discussion, status and stop requests may be
answered directly; do not resume earlier work without a current request.
Ask only when the current request cannot proceed without a user-supplied fact.
Preserve policy/approval boundaries and reconcile unknown effects
before retrying. Do not repeat settled effects merely to produce evidence.
The answer text is for the user, not a runtime diagnostic.
This also applies when revising after validation feedback. Explain useful results plainly; never ask the user to
debug the system or verify internal tool execution.
Capability unavailable claims MUST use blocked with current evidence of the
specific unavailable operation.
In a tool-calling round, include a short progress sentence in assistant content
before the calls as {"kind":"progress","text":"what you will check or do next"}.
During multi-step work, add a brief update when a meaningful finding changes
the next step. Describe intent or observed facts, not private reasoning or
unverified completion. Only the text is shown while tools run; the wrapper is
internal. Never put an answer, outcome, execution summary or evidence claims
in a progress object. The answer contract below applies only to the final
answer without tool calls. Do not repeat progress in the final answer.
When ready to give the final answer, return one JSON object:
{"kind":"answer","text":"user-facing answer","outcome":{"status":"answered|completed|partial|blocked|not_attempted","evidence_refs":["current call id"],"limitations":["specific unresolved requirement"],"claims":[{"operation":"exact operation identifier","status":"completed|partial|blocked|not_attempted","evidence_refs":["current call id"]}]}}
answered is for discussion. For action requests report completed, partial,
blocked or not_attempted according to actual results. A requested new observation
needs current evidence; history can answer a question about last known results.
Missing data is not a capability failure. completed/partial/blocked need
operation-specific claims. Only cite actual current calls, including exact child
call IDs from delegated execution.evidence; discovery or delegation alone is not
execution. Use tool names (or manor actions) as operation IDs, workspace_agent.<action>
for Workspace, acquire_capabilities.<action> for acquisition. Evidence supports
only its operation and resources, never another mutation or the entire user goal.
completed needs all claims completed; blocked needs an observed failure.
Preserve unverified requirements in limitations. Do not invent evidence or
relabel unfinished action requests as answered. Keep this bookkeeping out of
user-facing text. Follow validation feedback if a reference or outcome needs
correction; evidence_aliases in feedback refer only to actual current calls.
</current-turn-evidence>""" + "\n" + STRUCTURED_RESPONSE_INSTRUCTIONS

    def tool_progress_text(self, content: str) -> str | None:
        """Only an explicit tool-round progress object can become live prose."""
        value, error = parse_response_object(content)
        if error or set(value) != {"kind", "text"} or value["kind"] != "progress":
            return None
        text = value["text"]
        return text.strip() if isinstance(text, str) and 0 < len(text.strip()) <= 2000 else None

    def checkpoint_state(self) -> dict:
        return {**super().checkpoint_state(), "requires_execution": self.requires_execution}

    def restore_checkpoint_state(self, state: dict) -> None:
        self.requires_execution = self.requires_execution or state.get("requires_execution") is True
        super().restore_checkpoint_state(state)

    def _feedback(self, code: str, message: str, *, execute: bool = True,
                  validation_error: dict | None = None):
        action = FinalResponseAction.CONTINUE_EXECUTION if execute else FinalResponseAction.REVISE_REPLY
        aliases = [
            {"alias": f"e{index}", "call_id": call_id,
             "operation": item.get("operation"), "stage": item.get("stage"), "result": item.get("result")}
            for index, (call_id, item) in enumerate(self.observations.items(), 1)
        ]
        return FinalResponseDecision(action, diagnostic=tool_error_payload(
            tool_name="final_response", code=code, message=message,
            details={"response_action": action.value, "failure_scope": "current_turn_evidence",
                     "facts": [runtime_model_view_of_observation(item) for item in self.observations.values()],
                     "evidence_aliases": aliases,
                     **({"validation_error": validation_error} if validation_error else {}),
                     "constraint": "Inspect the specific gap and continue this loop within current policy. Never repeat settled or unknown effects."},
        ))

    def _delegated_attempts(self, refs: list[str]) -> list[dict]:
        """Follow host-owned parent links, not a model-chosen call-ID prefix."""
        delegations = {ref for ref in refs if self.observations.get(ref, {}).get("stage") == "delegation"}
        attempts = []
        for item in self.observations.values():
            if item.get("stage") not in {"execution", "authorization", "inventory"}:
                continue
            parent = item.get("parent_call_id")
            seen = set()
            while parent and parent not in seen:
                if parent in delegations:
                    attempts.append(item)
                    break
                seen.add(parent)
                parent = self.observations.get(parent, {}).get("parent_call_id")
        return attempts

    def _repair_delegation_claim(self):
        return self._feedback(
            "operation_evidence_insufficient",
            "A cited claim refers to delegation, not the work performed. "
            "Replace this claim with the specific child operations and their references "
            "from evidence_aliases. Report only what those results establish; "
            "keep any unmet goal as a limitation. Do not repeat the child invocation "
            "to correct this attribution.",
            execute=False,
        )

    def _undelivered_handoff_feedback(self, candidate: dict, context: FinalResponseContext):
        """A Skill handoff is the agent's to finish in this turn, not progress to report.

        A ``sandbox_ready`` handoff stays open until a later call saves the
        deliverable; a staged Skill's declared stage stays open until the Skill
        is invoked again. Until then no reply may end the turn while execution
        is still allowed. An observed failure after the handoff lets the agent
        report that blocker honestly (blocked/partial, checked by the rules
        below); it never lets it claim delivery. Not every skill use asks for
        a file ("what does this PDF say"): once the skill has run in its
        sandbox, a plain ``answered`` reply, which claims no completed
        operation, may end the turn.
        """
        if not context.allow_execution:
            return None
        pending = self.pending_sandbox_handoffs()
        if not pending:
            return None
        outcome = candidate.get("outcome")
        status = outcome.get("status") if isinstance(outcome, dict) else None
        if status in {"blocked", "partial"} and any(handoff["failed_call_ids"] for handoff in pending):
            return None
        if status == "answered" and all(
            handoff.get("kind") == "sandbox_ready" and handoff.get("ran") for handoff in pending
        ):
            return None
        handoff = pending[-1]
        skill = handoff["skill"] or "skill"
        if handoff.get("kind") == "skill_continuation":
            return self._feedback("skill_stage_unfinished", (
                f"The {skill} Skill stopped at stage '{handoff.get('stage')}', which is not the requested "
                "result. Do not reply to the user mid-way. In this same turn, invoke the same Skill again "
                "with the references from its result (for example the canvas link) and continue until it "
                "returns stage 'delivered', or report blocked with the failed call if a step actually fails."
            ))
        sandbox = f" (sandbox_id {handoff['sandbox_id']})" if handoff.get("sandbox_id") else ""
        return self._feedback("sandbox_handoff_undelivered", (
            f"The {skill} sandbox{sandbox} was opened for you to run the skill; that is not progress "
            "to report, and the requested deliverable does not exist yet. Do not reply to the user "
            "mid-way. In this same turn, run the skill workflow with the sandbox tool and save the "
            "final file with sandbox action='save_result' (artifact_role='final'). Answer only after "
            "the file is saved, or report blocked with the failed call if a step actually fails."
        ))

    async def validate(self, content: str) -> FinalResponseDecision:
        return await self.validate_with_context(content, FinalResponseContext())

    async def validate_with_context(
        self, content: str, context: FinalResponseContext,
    ) -> FinalResponseDecision:
        candidate, validation_error = parse_response_object(content)
        if validation_error:
            prose = content.strip() if isinstance(content, str) else ""
            if (self.accept_prose and prose and len(prose) <= 131072
                    and not prose.startswith(("{", "[", "`"))
                    and not self.observations and not self.requires_execution):
                # A plain follow-up reply that used no tools this turn has no
                # evidence or claim to bookkeep: accept it as the answer
                # instead of bouncing it back for re-formatting.
                return await self.validate_with_context(json.dumps({
                    "kind": "answer", "text": prose,
                    "outcome": {"status": "answered", "evidence_refs": [], "limitations": []},
                }, ensure_ascii=False), context)
            return self._feedback(
                "response_contract_invalid",
                'Return one raw JSON object matching the response contract: '
                '{"response":{"kind":"answer","text":"user-facing answer","outcome":'
                '{"status":"answered","evidence_refs":[],"limitations":[],"claims":[]}}}. '
                "Use no Markdown fences or surrounding prose. Set the actual outcome status and operation-specific claims from the current evidence aliases. "
                "Correct the validation_error below using the existing results; do not repeat settled effects or any tool call. "
                "The answer text must address the user's request, not explain format repair.",
                execute=False, validation_error=validation_error,
            )
        validation_error = response_schema_error(candidate, self._schema)
        if validation_error:
            path = validation_error["path"]
            code = "response_contract_invalid"
            if "outcome" in validation_error.get("missing_fields", []) or path == ["outcome"]:
                code = "response_outcome_required"
            elif path[:2] == ["outcome", "claims"]:
                code = "outcome_claims_invalid"
            elif path[:1] == ["outcome"]:
                code = "response_outcome_invalid"
            return self._feedback(
                code, "Correct the field described by validation_error using the existing results; "
                "return the complete reply without repeating any tool call.",
                execute=False, validation_error=validation_error,
            )
        answer_text = candidate.get("text")
        if candidate["kind"] == "answer" and not answer_text.strip():
            return self._feedback(
                "response_contract_invalid", "Answer text must not be blank.", execute=False,
                validation_error={"reason": "blank_text", "path": ["text"], "rule": "nonblank"},
            )
        if (
            candidate.get("kind") == "answer"
            and isinstance(answer_text, str)
            and len(answer_text) <= 240
            and _INTERNAL_FORMAT_REPAIR_REPLY.search(answer_text)
        ):
            return self._feedback(
                "answer_text_internal_format_repair",
                "Revise the answer text to address the user's latest request directly. Keep JSON format repair and system instructions out of user-facing text.",
                execute=False,
            )
        handoff_gap = self._undelivered_handoff_feedback(candidate, context)
        if handoff_gap is not None:
            return handoff_gap
        if candidate.get("kind") == "execution_summary" and self.inner:
            if context.allow_execution and self.requires_execution and not any(item.get("external_attempt") or item.get("authorization_blocked") for item in self.observations.values()):
                return self._feedback("current_research_evidence_required", "Task status is not evidence of current external publications. Perform the requested lookup before summarizing research.")
            decision = await validate_final_response(self.inner, json.dumps(candidate, ensure_ascii=False), context)
            if decision.action is FinalResponseAction.ACCEPT and not context.allow_execution:
                return replace(decision, stop_reason=context.stop_reason)
            return decision
        outcome = candidate.pop("outcome", None)
        status, refs, limitations = outcome["status"], outcome["evidence_refs"], outcome["limitations"]
        for index, limitation in enumerate(limitations):
            if not limitation.strip():
                return self._feedback(
                    "response_outcome_invalid", "Describe each limitation with nonblank text.", execute=False,
                    validation_error={"reason": "blank_text", "path": ["outcome", "limitations", index], "rule": "nonblank"},
                )
        claims = outcome.get("claims", [])
        alias_to_call_id = {
            f"e{index}": call_id
            for index, call_id in enumerate(self.observations, 1)
        }
        # Derive the aggregate from the per-operation declarations. Repeating
        # a known reference at two JSON paths does not add proof; each ref and
        # its operation are still checked against the host ledger below.
        refs = list(dict.fromkeys(alias_to_call_id.get(ref, ref) for ref in [
            *refs, *(ref for claim in claims for ref in claim["evidence_refs"]),
        ]))
        if len(refs) > 32:
            return self._feedback("response_outcome_invalid", "Use at most 32 distinct current evidence references.", execute=False)
        outcome["evidence_refs"] = refs
        stale_refs = [ref for ref in refs if ref not in self.observations]
        if stale_refs and status == "answered":
            # History can answer a question about last known results; it just
            # is not current evidence. A discussion answer that cites earlier
            # turns' calls loses those citations instead of being rejected into
            # re-execution (the rejection used to replace the whole answer).
            stale = set(stale_refs)
            refs = [ref for ref in refs if ref not in stale]
            outcome["evidence_refs"] = refs
            current_claims = []
            for claim in claims:
                claim_refs = [alias_to_call_id.get(ref, ref) for ref in claim["evidence_refs"]]
                current_refs = [ref for ref in claim_refs if ref not in stale]
                if claim_refs and not current_refs:
                    continue
                claim["evidence_refs"] = current_refs
                current_claims.append(claim)
            claims = current_claims
            if "claims" in outcome:
                outcome["claims"] = claims
        elif stale_refs:
            claimed_operations = {
                claim.get("operation") for claim in outcome.get("claims", [])
                if isinstance(claim, dict) and isinstance(claim.get("operation"), str)
            }
            observed_operations = {item.get("operation") for item in self.observations.values()}
            repair_only = bool(claimed_operations) and claimed_operations <= observed_operations
            return self._feedback(
                "current_evidence_required",
                f"References {stale_refs[:8]!r} are not calls made in this turn; earlier turns' calls "
                "are not current evidence. If the answer relies only on earlier results already in "
                "the conversation, reply with status answered and do not cite them. If the request "
                "needs a new observation or action, make those tool calls now and cite them.",
                execute=not repair_only,
            )
        attempts = [self.observations[ref] for ref in refs if self.observations[ref].get("stage") in {"execution", "authorization", "inventory"}]
        delegated_attempts = self._delegated_attempts(refs)
        requires_attempt = self.requires_execution and not (
            not context.allow_execution and status in {"partial", "not_attempted"}
        )
        if requires_attempt and not any(item.get("external_attempt") or item.get("authorization_blocked") for item in attempts):
            if any(item.get("external_attempt") or item.get("authorization_blocked") for item in delegated_attempts):
                return self._repair_delegation_claim()
            return self._feedback("current_research_evidence_required", "This request requires current external information. Internal task records and discovery do not prove external retrieval succeeded or failed; attempt an authorized retrieval path.")
        if (requires_attempt or status in {"completed", "blocked"}) and not attempts:
            if delegated_attempts:
                return self._repair_delegation_claim()
            return self._feedback("capability_execution_evidence_required", "Discovery alone does not establish completion or inability. Attempt the requested work with suitable current tools; return a specific observed limitation if it fails.")
        if status == "completed" and attempts and all(item.get("result") == "error" for item in attempts):
            return self._feedback("completion_evidence_missing", "The cited attempts all failed. Continue correction or report the concrete unresolved result without claiming completion.")
        if status in {"blocked", "partial", "not_attempted"} and not limitations:
            return self._feedback("outcome_limitation_required", "Describe the specific remaining requirement or observed blocker.", execute=False)
        if status in {"completed", "partial", "blocked"} and not claims:
            return self._feedback("operation_evidence_required", "Associate the outcome with the exact attempted operation using claims; one unrelated tool call is not proof.", execute=False)
        for claim in claims:
            claim_refs = [alias_to_call_id.get(ref, ref) for ref in claim["evidence_refs"]]
            claim["evidence_refs"] = claim_refs
            evidence = [self.observations[ref] for ref in claim_refs if ref in self.observations]
            if any(item.get("operation") != claim["operation"] for item in evidence):
                cited_operations = sorted({str(item.get("operation")) for item in evidence})
                return self._feedback(
                    "operation_evidence_mismatch",
                    f"Claimed operation {claim['operation']!r} cites {cited_operations!r}. "
                    "Use evidence_aliases for calls of the exact operation, or report it as not attempted. "
                    "Do not infer another capability is unavailable.",
                    execute=False,
                )
            if claim["status"] != "not_attempted" and not evidence:
                return self._feedback("operation_evidence_required", "This operation has no cited current evidence.")
            if claim["status"] in {"completed", "blocked"}:
                expected_result = "error" if claim["status"] == "blocked" else "returned"
                stages = {"execution", "authorization"} if claim["status"] == "blocked" else {"execution", "authorization", "inventory"}
                if not any(item.get("stage") in stages and item.get("result") == expected_result for item in evidence):
                    # Discovery can complete its own lookup, never the action
                    # it discovered. Delegation cannot complete a child goal.
                    discovery_completed = claim["status"] == "completed" and any(
                        item.get("stage") == "discovery" and item.get("result") == "returned" for item in evidence
                    )
                    if not discovery_completed:
                        if self._delegated_attempts(claim_refs):
                            return self._repair_delegation_claim()
                        return self._feedback("operation_evidence_insufficient", "Delegation is not goal completion. Discovery and successful empty reads cannot prove an operation blocked. Use scoped child evidence or report what remains.")
        claim_statuses = {claim["status"] for claim in claims}
        if status == "completed" and claim_statuses != {"completed"}:
            return self._feedback("outcome_claim_mismatch", "Unfinished claims cannot produce overall completed. Report partial or continue the work.", execute=False)
        if status == "blocked" and "blocked" not in claim_statuses:
            return self._feedback("outcome_claim_mismatch", "Overall blocked requires a failed operation. Missing information or an untried operation alone is not a capability blocker.", execute=False)
        if status == "partial" and not any(
            claim["evidence_refs"] and claim["status"] != "not_attempted" for claim in claims
        ):
            return self._feedback("outcome_claim_mismatch", "Partial requires observed progress; otherwise use not_attempted with the remaining requirements.", execute=False)
        if self.inner:
            decision = await validate_final_response(self.inner, json.dumps(candidate, ensure_ascii=False), context)
            if decision.action is not FinalResponseAction.ACCEPT:
                return decision
        elif (set(candidate) != {"kind", "text"} or candidate.get("kind") != "answer"
                or not isinstance(candidate.get("text"), str) or not candidate["text"].strip()):
            return self._feedback("response_contract_invalid", "Return kind=answer with nonempty text and outcome.", execute=False)
        else:
            decision = FinalResponseDecision(FinalResponseAction.ACCEPT, content=candidate["text"])
        if status == "not_attempted" and context.allow_execution:
            return self._feedback(
                "current_request_unresolved",
                "Your proposed answer says the requested work was not attempted. "
                "Re-evaluate the current user request against the observed facts. "
                "If it requests action or a current observation, use suitable authorized tools. "
                "For discussion, a plan, status or stopping, answer that request with status answered "
                "without executing old work. Use request_input only when the current request cannot proceed without a user-supplied fact. "
                "Do not relabel unfinished work as answered merely to finish, or repeat settled or unknown effects.",
            )
        # Shadow comparison for tenants still on the typed contract: how the
        # model-declared status differs from the host-derived one.
        from packages.core.ai.runtime.chat_outcome import derive_chat_outcome_from_ledger

        derived = derive_chat_outcome_from_ledger(self)["status"]
        if derived != status:
            logger.info("[chat_outcome] shadow model_status=%s derived_status=%s", status, derived)
        return replace(
            decision, outcome=outcome,
            stop_reason=context.stop_reason if status in {"partial", "not_attempted"} else None,
        )


_PLAIN_TEXT_REPLY_INSTRUCTIONS = """<reply-guidance>
Use tools to carry out the current request, interpreting references, supplied
inputs and constraints in conversation context. For the same resource, later
user changes replace earlier requested values; preserve the latest values on
continuation unless the user asks to revert. Use the latest explicit resource
selection to resolve references. Observed values on other resources are not
user instructions. Inspect current state when necessary before changing it.
Discussion, status and stop requests may be answered directly; do not resume
earlier work without a current request. Ask only when the current request
cannot proceed without a user-supplied fact. Preserve policy/approval
boundaries and reconcile unknown effects before retrying. Do not repeat
settled effects.
Every time you call tools, first write one short sentence to the user, in
the user's language, saying what you are about to do and why, e.g. "Let me
check your knowledge base for the Q3 report you asked me to summarize." One
sentence covers the calls of that step. When a result changes the plan, say
what you found and what you will do next. The user sees these sentences next
to each step while the tools run; a step with no sentence is unexplained.
Describe intent or observed facts, not private reasoning or unverified
completion. When calling sandbox, also set its description to a few
words in the user's language saying what the call does and why (e.g. "生成第
3 页 SVG：增长渠道"); it is the step label the user sees and is never run.
When you are done, answer the user directly in plain Markdown. Never wrap the
answer in JSON or code fences, and never repeat progress sentences in it.
After work that made or changed something, the answer is a short summary of
the outcome: what was delivered (with its file link), what it contains or the
main choices made, in a few bullets, and anything the user should check or
decide next. A file link or "done" on its own is not an answer.
State only results this turn's tool results show. Say plainly what failed,
was blocked or was not attempted, with the specific reason. Saying a
capability is unavailable requires an attempt of that operation that failed;
discovery alone is not an attempt. A quality gate rejection is a list of
corrections, not a blocker: fix every listed issue and try again until the
deliverable passes. Explain useful results plainly; never ask the user to
debug the system or verify internal tool execution.
</reply-guidance>"""

_WORKSPACE_PLAIN_TEXT_INSTRUCTIONS = """<workspace-reply-guidance>
Do not state Task dispatch, execution, delivery or background progress from
memory. The server appends the verified current status of every Task this
turn created. For earlier Tasks, report only what this turn's tool results
show. Do not copy progress from older messages or unrelated scheduled work.
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
Missing, expired or unknown effects retain normal approval/reconciliation.
</workspace-reply-guidance>"""

# A deliverable its quality gate rejected came with a list of corrections:
# the model is sent back until it passes. Only the run's round and time
# budget bound this gate; it never counts against the other gates.
_QUALITY_GATE_ERROR_CODES = frozenset({"sandbox_quality_gate_blocked"})
# Delivery failures the agent cannot fix by itself; a reply may report these.
# Every other failure after a handoff (a failed command, a wrong file name)
# is the agent's to fix, unless the same failure keeps repeating.
_UNRECOVERABLE_ERROR_CODES = frozenset({
    "sandbox_access_denied", "sandbox_capacity_full", "sandbox_creation_failed",
    "entity_filesystem_unavailable", "workspace_artifact_scope_unavailable",
})
_NO_PROGRESS_REPEATS = 3
# Reply-quality retries: separate from the ledger-gate budget.
_REPLY_GATES = frozenset({"response_empty", "file_link_unopenable"})
_UNCAPPED_GATES = frozenset({"delivery_quality_unmet", "delivery_failure_fixable"})

# Per turn, each ledger gate may send the model back once and all gates
# together twice. Past that the reply is accepted with the gap as a limitation:
# a gate is a nudge to finish the work, never a reason to fail the turn.
_GATE_ATTEMPT_LIMIT = 1
_GATE_TOTAL_LIMIT = 2


def _legacy_reply_text(content: str) -> str:
    """Plain text of a reply that imitated the old JSON envelope.

    Earlier turns of a conversation may still hold JSON replies; a model that
    copies one is not sent back for it. Anything that is not exactly an old
    answer/progress object stays as written.
    """
    stripped = content.strip()
    if not stripped.startswith("{"):
        return content
    candidate, error = parse_response_object(stripped)
    if error or not isinstance(candidate, dict):
        return content
    text = candidate.get("text")
    if candidate.get("kind") in {"answer", "progress"} and isinstance(text, str) and text.strip():
        return text
    return content


def plain_text_history(messages: list[dict]) -> list[dict]:
    """History with old JSON reply envelopes reduced to their visible text.

    Sandbox locations an earlier reply linked are reduced to their words, so
    the model never re-delivers them as files.
    """
    result = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(content, str):
            text = strip_internal_file_links(_legacy_reply_text(content))
            if text != content:
                message = {**message, "content": text}
        result.append(message)
    return result


class ChatStopGate(RuntimeToolEvidence):
    """Plain-text chat replies checked only against the host tool ledger.

    The model writes the answer; the host derives the outcome
    (chat_outcome.derive_chat_outcome) and, in Workspace Chat, appends the
    verified status of Tasks this turn created. The only reasons to send the
    model back are ledger gaps: an unfinished Skill/sandbox delivery, a
    required external lookup never attempted, or (when enabled) discovery with
    no attempt. Format is never a reason.
    """

    response_format = None
    tool_response_format = None
    # The loop streams this contract's candidate text live; a rejected
    # candidate is withdrawn with a text_reset.
    streams_candidate_text = True

    def __init__(
        self, *, workspace_scope: Any = None, locale: str = "en",
        requires_execution: bool = False, discovery_gate: bool = False,
        task_reader: Any = None, current_turn_task_reader: Any = None,
        file_resolver: Any = None, entity_id: str | None = None,
        stranded_task_reader: Any = None,
        lone_task_reader: Any = None,
    ):
        super().__init__()
        self.workspace_scope = workspace_scope
        # Resolves a file address to the FileRef of a file this turn's user
        # can open (file_refs.openable_file_resolver); None skips link checks.
        self._file_resolver = file_resolver
        self._entity_id = entity_id
        self.locale = locale or "en"
        self.requires_execution = requires_execution
        self.discovery_gate = discovery_gate
        self._task_reader = task_reader
        self._current_turn_task_reader = current_turn_task_reader
        self._stranded_task_reader = stranded_task_reader
        self._lone_task_reader = lone_task_reader
        self.gate_attempts: dict[str, int] = {}
        self.gate_limitations: list[str] = []
        self.instructions = _PLAIN_TEXT_REPLY_INSTRUCTIONS + (
            "\n" + _WORKSPACE_PLAIN_TEXT_INSTRUCTIONS if workspace_scope is not None else ""
        )

    def checkpoint_state(self) -> dict:
        return {
            **super().checkpoint_state(),
            "requires_execution": self.requires_execution,
            "gate_attempts": dict(self.gate_attempts),
        }

    def restore_checkpoint_state(self, state: dict) -> None:
        self.requires_execution = self.requires_execution or state.get("requires_execution") is True
        attempts = state.get("gate_attempts")
        if isinstance(attempts, dict):
            self.gate_attempts = {
                str(key): int(value) for key, value in attempts.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
        super().restore_checkpoint_state(state)

    def _handoff_failures(self, pending: list[dict]) -> list[dict]:
        """Observed failures after the open delivery handoffs, oldest first."""
        call_ids = list(dict.fromkeys(
            call_id for handoff in pending for call_id in handoff.get("failed_call_ids") or ()
        ))
        return [self.observations[call_id] for call_id in call_ids if call_id in self.observations]

    @staticmethod
    def _failure_blocks_delivery(failures: list[dict]) -> bool:
        """Whether the latest failure is one the reply may report as a blocker."""
        last = failures[-1]
        if last.get("error_code") in _QUALITY_GATE_ERROR_CODES:
            return False
        if last.get("authorization_blocked") or last.get("error_code") in _UNRECOVERABLE_ERROR_CODES:
            return True
        # The same failure several times in a row: fixing is not progressing.
        tail = failures[-_NO_PROGRESS_REPEATS:]
        signatures = {
            (item.get("operation"), str(item.get("message") or item.get("error") or "")[:300])
            for item in tail
        }
        return len(tail) == _NO_PROGRESS_REPEATS and len(signatures) == 1

    def _gaps(self, context: FinalResponseContext) -> list[tuple[str, str, str]]:
        """(code, model feedback, user-facing limitation) of each open ledger gap."""
        if not context.allow_execution:
            return []
        gaps = []
        pending = self.pending_sandbox_handoffs()
        # An observed failure after the handoff lets the reply report that
        # blocker; it never lets the turn claim delivery (the outcome says so).
        # A quality gate rejection is not such a blocker: it lists fixes.
        failures = self._handoff_failures(pending) if pending else []
        if failures and not self._failure_blocks_delivery(failures):
            skill = pending[-1]["skill"] or "skill"
            if failures[-1].get("error_code") in _QUALITY_GATE_ERROR_CODES:
                gaps.append(("delivery_quality_unmet", (
                    f"The {skill} deliverable was rejected by its quality gate. The rejection lists exactly "
                    "what to correct: fix every listed issue, export again and save the final file again. "
                    "Quality findings are corrections, not blockers; do not end the turn or report failure "
                    "until the file passes the quality gate."
                ), f"{skill}: the deliverable did not pass its quality gate"))
            else:
                gaps.append(("delivery_failure_fixable", (
                    f"A step of the {skill} delivery failed, and the failure is yours to fix: read the error, "
                    "check the actual state (for example list the files a step produced and use their exact "
                    "names), correct the call and continue until the final file is saved. Do not end the "
                    "turn or report failure because of a fixable step error."
                ), f"{skill}: a delivery step failed"))
        elif pending and not failures and not any(handoff["failed_call_ids"] for handoff in pending):
            handoff = pending[-1]
            skill = handoff["skill"] or "skill"
            if handoff.get("kind") == "skill_continuation":
                gaps.append(("skill_stage_unfinished", (
                    f"The {skill} Skill stopped at stage '{handoff.get('stage')}', which is not the requested "
                    "result. Do not reply to the user mid-way. In this same turn, invoke the same Skill again "
                    "with the references from its result (for example the canvas link) and continue until it "
                    "returns stage 'delivered', or report the blocker if a step actually fails."
                ), f"{skill}: the requested result was not delivered"))
            else:
                sandbox = f" (sandbox_id {handoff['sandbox_id']})" if handoff.get("sandbox_id") else ""
                gaps.append(("sandbox_handoff_undelivered", (
                    f"The {skill} sandbox{sandbox} was opened for you to run the skill; that is not progress "
                    "to report, and the requested deliverable does not exist yet. Do not reply to the user "
                    "mid-way. In this same turn, run the skill workflow with the sandbox tool and save the "
                    "final file with sandbox action='save_result' (artifact_role='final'). Answer only after "
                    "the file is saved, or report the blocker if a step actually fails."
                ), f"{skill}: the requested deliverable was not saved"))
        observations = list(self.observations.values())
        if self.requires_execution and not any(
            item.get("external_attempt") or item.get("authorization_blocked") for item in observations
        ):
            gaps.append(("current_research_evidence_required", (
                "This request requires current external information. Earlier conversation text and "
                "discovery do not prove retrieval succeeded or failed; attempt an authorized retrieval path."
            ), "current external information was not retrieved"))
        if self.discovery_gate and any(
            item.get("stage") == "discovery" and item.get("result") == "returned" for item in observations
        ) and not any(
            item.get("stage") in {"execution", "inventory", "delegation", "authorization"} for item in observations
        ):
            gaps.append(("capability_execution_evidence_required", (
                "Discovery alone does not establish completion or inability. Attempt the requested work "
                "with suitable current tools; report a specific observed limitation if it fails."
            ), "suitable capabilities were found but not attempted"))
        return gaps

    async def validate(self, content: str) -> FinalResponseDecision:
        return await self.validate_with_context(content, FinalResponseContext())

    async def validate_with_context(
        self, content: str, context: FinalResponseContext,
    ) -> FinalResponseDecision:
        text = _legacy_reply_text(content if isinstance(content, str) else "")
        if not text.strip() and not self.gate_attempts.get("response_empty"):
            # One retry for an empty reply; after that the turn's existing
            # empty-reply fallback speaks for it.
            self.gate_attempts["response_empty"] = 1
            return FinalResponseDecision(
                FinalResponseAction.REVISE_REPLY,
                diagnostic=tool_error_payload(
                    tool_name="final_response", code="response_empty",
                    message="The reply was empty. Answer the user's request in plain text using the results you already have; do not repeat any tool call.",
                    details={"response_action": FinalResponseAction.REVISE_REPLY.value},
                ),
            )
        # Recomputed on every reply from the current ledger: a gap the model
        # closed after an earlier nudge leaves no stale limitation behind.
        open_limitations: list[str] = []
        for code, message, limitation in self._gaps(context):
            # A gate past its budget becomes a limitation; it never hides a
            # later gate that still has one.
            attempts = self.gate_attempts.get(code, 0)
            ledger_nudges = sum(n for key, n in self.gate_attempts.items()
                                if key not in _REPLY_GATES and key not in _UNCAPPED_GATES)
            if code in _UNCAPPED_GATES or (
                attempts < _GATE_ATTEMPT_LIMIT and ledger_nudges < _GATE_TOTAL_LIMIT
            ):
                self.gate_attempts[code] = attempts + 1
                return FinalResponseDecision(
                    FinalResponseAction.CONTINUE_EXECUTION,
                    diagnostic=tool_error_payload(
                        tool_name="final_response", code=code, message=message,
                        details={
                            "response_action": FinalResponseAction.CONTINUE_EXECUTION.value,
                            "failure_scope": "current_turn_evidence",
                            "facts": [runtime_model_view_of_observation(item) for item in self.observations.values()],
                            "constraint": "Continue this loop within current policy. Never repeat settled or unknown effects.",
                        },
                    ),
                )
            open_limitations.append(limitation)
        self.gate_limitations = open_limitations
        if stranded := await self._stranded_tasks():
            return FinalResponseDecision(
                FinalResponseAction.CONTINUE_EXECUTION,
                diagnostic=tool_error_payload(
                    tool_name="final_response", code="tasks_stranded",
                    message=(
                        "These Tasks this turn created will never start: "
                        + "; ".join(f"{item['title']} ({item['id']})" for item in stranded[:8])
                        + ". Link each to the stages it follows with the Workspace "
                        "update_task_runtime action (depends_on_task_ids = the earlier task ids); "
                        "it then starts when they complete. If it follows nothing, start it with "
                        "start_task. Then reply."
                    ),
                    details={"response_action": FinalResponseAction.CONTINUE_EXECUTION.value,
                             "task_ids": [item["id"] for item in stranded[:8]]},
                ),
            )
        if lone := await self._once("tasks_requested_one_created", self._lone_task_reader):
            return FinalResponseDecision(
                FinalResponseAction.CONTINUE_EXECUTION,
                diagnostic=tool_error_payload(
                    tool_name="final_response", code="tasks_requested_one_created",
                    message=(
                        f"The user asked for Tasks and this turn created one: {lone[0]['title']} "
                        f"({lone[0]['id']}). Nothing creates later stages for you: Chat does not run "
                        "again when a Task completes. If the plan has further stages, create each now "
                        "with create_task (owner_service_key, depends_on_task_ids naming the earlier "
                        "task ids, start=true); each starts when its predecessors complete. If this "
                        "one Task is the whole request, reply."
                    ),
                    details={"response_action": FinalResponseAction.CONTINUE_EXECUTION.value,
                             "task_ids": [lone[0]["id"]]},
                ),
            )
        if self._file_resolver is not None:
            links = await rewrite_reply_file_links(text, self._file_resolver)
            if links.unresolved and not self.gate_attempts.get("file_link_unopenable"):
                # One chance to link the saved file (or say it was not saved);
                # after that the unopenable links are reduced to their words.
                self.gate_attempts["file_link_unopenable"] = 1
                return FinalResponseDecision(
                    FinalResponseAction.REVISE_REPLY,
                    diagnostic=tool_error_payload(
                        tool_name="final_response", code="file_link_unopenable",
                        message=self._unopenable_links_feedback(links.unresolved),
                        details={"response_action": FinalResponseAction.REVISE_REPLY.value},
                    ),
                )
            text = links.text
        evidence: tuple[dict, ...] = ()
        if self.workspace_scope is not None and self._current_turn_task_reader is not None:
            section, evidence = await self._current_turn_task_section()
            if section:
                text = f"{text.rstrip()}\n\n{section}" if text.strip() else section
        from packages.core.ai.runtime.chat_outcome import derive_chat_outcome_from_ledger

        outcome = derive_chat_outcome_from_ledger(
            self, stop_reason="max_rounds" if context.budget_exhausted else None,
            gate_limitations=self.gate_limitations,
        )
        return FinalResponseDecision(
            FinalResponseAction.ACCEPT, content=text.strip(), evidence=evidence, outcome=outcome,
            stop_reason=context.stop_reason if outcome["status"] in {"partial", "not_attempted"} else None,
        )

    def _unopenable_links_feedback(self, unresolved: tuple[str, ...]) -> str:
        available = []
        for receipt in self.receipts:
            if not (receipt.get("document_id") or receipt.get("fs_path")):
                continue
            ref = file_ref(
                entity_id=self._entity_id, fs_path=receipt.get("fs_path"),
                document_id=receipt.get("document_id"), name=receipt.get("name"),
            )
            if ref.get("markdown_link") and ref["markdown_link"] not in available:
                available.append(ref["markdown_link"])
        listed = "; ".join(unresolved[:5])
        message = (
            f"These file links in the reply do not open for the user: {listed}. A sandbox path, an "
            "earlier turn's location or a guessed address is not a delivered file. Link a file only by "
            "copying the exact markdown_link a tool returned for a saved file; if the file was not saved, "
            "do not link it and say it was not produced. Rewrite the reply; do not repeat any tool call."
        )
        if available:
            message += " Files saved in this turn: " + "; ".join(available[:10])
        return message

    async def _stranded_tasks(self) -> list[dict[str, str]]:
        """Once per turn: stages this turn created that nothing will start."""
        return await self._once("tasks_stranded", self._stranded_task_reader)

    async def _once(self, code: str, reader: Any) -> list[dict[str, str]]:
        """A Task reader's findings, acted on at most once per turn."""
        if self.workspace_scope is None or reader is None or self.gate_attempts.get(code):
            return []
        try:
            found = await reader(self.workspace_scope)
        except Exception:
            return []  # a lookup failure is not evidence
        if found:
            self.gate_attempts[code] = 1
        return found

    async def _current_turn_task_section(self) -> tuple[str, tuple[dict, ...]]:
        from packages.core.ai.runtime.workspace_response import render_verified_task_status

        try:
            references = await self._current_turn_task_reader(self.workspace_scope)
            if not references:
                return "", ()
            facts = await self._task_reader(self.workspace_scope, references)
        except Exception:
            # A lookup failure is not evidence of absent work; the answer
            # stays as written and the outcome still comes from the ledger.
            return "", ()
        by_id = {fact["id"]: fact for fact in facts if isinstance(fact, dict)}
        pairs = [(ref, by_id[ref["id"]]) for ref in references if ref["id"] in by_id]
        if not pairs:
            return "", ()
        ordered = [fact for _ref, fact in pairs]
        from packages.core.ai.runtime.response_language import current_response_locale

        # Rendered inside the run, where the user's reply language is known;
        # the request locale is not (a worker turn has no request headers).
        return render_verified_task_status(
            [ref for ref, _fact in pairs], ordered, locale=current_response_locale() or self.locale,
        ), tuple(ordered)


def _chat_stop_gate(ctx, plan) -> ChatStopGate:
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.turn_policy import TurnResearchStrategy
    from packages.core.i18n import get_locale
    from packages.core.services.workspace_execution_evidence import (
        ExecutionEvidenceScope, read_current_turn_task_references, read_lone_requested_task,
        read_stranded_turn_tasks, read_workspace_task_evidence,
    )

    envelope = getattr(ctx, "runtime_envelope", None)
    scope = None
    if getattr(envelope, "surface", None) == ChatSurface.WORKSPACE_CHAT:
        fields = [getattr(envelope, name, None) for name in ("entity_id", "workspace_id", "user_id", "conversation_id")]
        if all(isinstance(value, str) and value for value in fields):
            metadata = getattr(envelope, "metadata", None) or {}
            scope = ExecutionEvidenceScope(*fields, origin_user_message_id=metadata.get("origin_user_message_id"))
    from packages.core.services.file_refs import openable_file_resolver

    entity_id = getattr(envelope, "entity_id", None)
    return ChatStopGate(
        workspace_scope=scope, locale=get_locale(),
        entity_id=entity_id,
        file_resolver=openable_file_resolver(
            entity_id=entity_id, user_id=getattr(envelope, "user_id", None),
            workspace_id=getattr(envelope, "workspace_id", None),
        ),
        requires_execution=(
            getattr(plan, "research_strategy", TurnResearchStrategy.NONE) != TurnResearchStrategy.NONE
        ),
        task_reader=read_workspace_task_evidence,
        current_turn_task_reader=read_current_turn_task_references,
        stranded_task_reader=read_stranded_turn_tasks,
        lone_task_reader=read_lone_requested_task,
    )


def chat_evidence_contract(ctx):
    """The reply contract of a chat turn: plain text with a host-derived outcome.

    Only tool-using owner, Agent DM and Workspace turns get one; with tools
    off there is no ledger to read and no Task the turn could have created.
    """
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.turn_policy import TurnExecutionMode

    envelope = getattr(ctx, "runtime_envelope", None)
    plan = getattr(ctx, "turn_execution_plan", None)
    if (envelope is None or getattr(envelope, "surface", None) not in {
        ChatSurface.GLOBAL_OWNER_CHAT, ChatSurface.AGENT_DM, ChatSurface.WORKSPACE_CHAT,
    } or getattr(plan, "disable_tools", False)
            or getattr(plan, "mode", None) == TurnExecutionMode.DIRECT_CHAT):
        return None
    return _chat_stop_gate(ctx, plan)
