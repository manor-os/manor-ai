"""Verified delivery evidence for the V3 Task coordinator's own AgentLoop.

The coordinator inspects this bounded snapshot and submits its assessment.
The host validates exact citations and rechecks the live snapshot before any
Task or Plan can be completed; evidence itself grants no execution authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from typing import Any

from packages.core.contracts.task_delivery_inspection import (
    DeliveryInspectionError,
    DeliveryInspectionRequest,
    parse_delivery_inspection,
)
from packages.core.services.sensitive_data import sanitize_approval_credentials, sanitize_sensitive_payload

# The snapshot is paged into the coordinator's context; it is never sent to a
# separate one-shot reviewer. The bound accommodates all 32 references that
# the artifact verifier allows, each with at most 24k of visible text.
MAX_REVIEW_CHARS = 1_200_000
MAX_ARTIFACT_TEXT_CHARS = 24_000
MAX_REVIEW_ARTIFACTS = 32
MAX_ACCEPTANCE_CHECKS = 512
INSPECTION_CHUNK_CHARS = 2_800
INSPECTION_RESULT_MAX_CHARS = 3_400


class TaskAcceptanceError(ValueError):
    """Evidence cannot be reviewed safely; never fall back to success."""

    # These failures allow a bounded fresh read/corrected assessment, not a
    # repeated Step. Missing access/content remains fail-closed; contention
    # uses the separate read-only backoff path.
    CORRECTABLE_CODES = frozenset({
        "invalid_assessment", "artifact_changed", "artifact_reference_missing",
        "artifact_identity_invalid", "artifact_identity_conflict", "artifact_evidence_invalid",
        "artifact_reference_ambiguous",
    })

    def __init__(self, message: str, *, code: str = "acceptance_unverifiable", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    @property
    def retryable(self) -> bool:
        return self.code == "artifact_busy"


class TaskAcceptanceVerdict(str, Enum):
    ACCEPTED = "accepted"
    CHANGES_REQUIRED = "changes_required"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class TaskAcceptanceSnapshot:
    payload_json: str

    @cached_property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    @cached_property
    def digest(self) -> str:
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()

    @cached_property
    def evidence_digest(self) -> str:
        """Stable across coordinator wakes, but changes with delivered evidence.

        The full digest still binds finalization to the exact live claim. The
        claim-independent digest binds inspection pages to their assessment.
        """
        payload = dict(self.payload)
        payload.pop("claim", None)
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskAcceptanceCheck:
    criterion: str
    pointer: str
    quote: str


@dataclass(frozen=True)
class TaskAcceptanceReview:
    snapshot_digest: str
    verdict: TaskAcceptanceVerdict
    evidence: str
    issues: tuple[str, ...]
    checks: tuple[TaskAcceptanceCheck, ...] = ()


def acceptance_snapshot(payload: dict[str, Any]) -> TaskAcceptanceSnapshot:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    if len(serialized) > MAX_REVIEW_CHARS:
        raise TaskAcceptanceError(
            "Task acceptance evidence exceeds the bounded review window; "
            "produce a focused, verifiable deliverable before submitting."
        )
    return TaskAcceptanceSnapshot(serialized)


def _plan_acceptance_criteria(payload: dict[str, Any]) -> list[dict[str, Any]]:
    dag = payload.get("plan_dag")
    metadata = dag.get("metadata") if isinstance(dag, dict) else None
    contract = metadata.get("acceptance_contract") if isinstance(metadata, dict) else None
    criteria = contract.get("criteria") if isinstance(contract, dict) else None
    return [item for item in criteria if isinstance(item, dict)] if isinstance(criteria, list) else []


def _evidence_value(payload: dict[str, Any], pointer: str) -> Any:
    """Resolve only delivered Step results or verified artifact text."""
    if (not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 1000
            or re.search(r"~(?![01])", pointer)):
        raise ValueError("invalid evidence pointer")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    if not (
        len(parts) >= 3 and re.fullmatch(r"0|[1-9][0-9]*", parts[1])
        and ((parts[0] == "steps" and parts[2] == "result")
             or (parts[0] == "artifacts" and parts[2:] == ["text"]))
    ):
        raise ValueError("citation is not delivered result or artifact content")
    value: Any = payload
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError("cited evidence does not exist")
    if parts[0] == "artifacts" and not isinstance(value, str):
        raise ValueError("artifact text is unavailable")
    return value


def _evidence_type(value: Any) -> str:
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    return "object"


def _step_citations(index: int, result: Any):
    """Expose canonical, bounded JSON Pointers for every citable scalar leaf."""
    def object_children(base: str, children: dict[str, Any]):
        for key, child in children.items():
            yield f"{base}/{str(key).replace('~', '~0').replace('/', '~1')}", child

    def list_children(base: str, children: list[Any]):
        for position, child in enumerate(children):
            yield f"{base}/{position}", child

    stack = [iter(((f"/steps/{index}/result", result),))]
    while stack:
        try:
            pointer, value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if len(pointer) > 1000:
            continue
        if isinstance(value, dict):
            stack.append(object_children(pointer, value))
        elif isinstance(value, list):
            stack.append(list_children(pointer, value))
        elif not isinstance(value, str) or value.strip():
            yield {"citation_pointer": pointer, "value_type": _evidence_type(value)}


def _citation_page(entries, *, offset: int, budget: int = 1_300) -> tuple[list[dict[str, str]], int | None, int]:
    """Bound reference metadata independently of the evidence text page."""
    selected: list[dict[str, str]] = []
    count = 0
    omitted = 0
    for entry in entries:
        if count < offset:
            count += 1
            continue
        entry_size = len(json.dumps([entry], ensure_ascii=False))
        if entry_size > INSPECTION_RESULT_MAX_CHARS - 400:
            count += 1
            omitted += 1
            continue
        candidate = selected + [entry]
        if len(json.dumps(candidate, ensure_ascii=False)) > budget:
            return selected, count, omitted
        selected = candidate
        count += 1
    if offset > count:
        raise DeliveryInspectionError(
            "citation offset exceeds result leaf count", path="$.offset",
            schema_rule="maximum", expected=count,
        )
    return selected, None, omitted


def inspect_task_delivery(
    snapshot: TaskAcceptanceSnapshot, *, kind: str = "manifest",
    index: int = 0, offset: int = 0, pointer: str | None = None,
) -> dict[str, Any]:
    """Page the host-verified evidence into the existing Task AgentLoop.

    Each content page is small enough to survive runtime tool-result limits.
    The digest changes when delivered evidence changes, so an assessment made
    against a stale page cannot be submitted after concurrent file edits.
    """
    parse_delivery_inspection({"kind": kind, "index": index, "offset": offset, "pointer": pointer})
    payload = snapshot.payload
    common = {"status": "ok", "evidence_digest": snapshot.evidence_digest}
    if kind == "evidence":
        try:
            value = _evidence_value(payload, pointer)
        except ValueError as exc:
            raise DeliveryInspectionError(
                str(exc), path="$.pointer", schema_rule="available_evidence_leaf",
                expected={"source": "citation_pointer", "snapshot": snapshot.evidence_digest},
            ) from exc
        if isinstance(value, (dict, list)):
            raise DeliveryInspectionError(
                "inspect a citable evidence leaf, not a result container", path="$.pointer",
                schema_rule="scalar_evidence", expected=["string", "number", "boolean", "null"],
            )
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        artifact_evidence = (
            (payload.get("artifacts") or [])[int(pointer.split("/")[2])]
            if pointer.startswith("/artifacts/") else None
        )
    else:
        artifact_evidence = None
    request = payload.get("task") or {}
    request_content = json.dumps(request, sort_keys=True, ensure_ascii=False, default=str)
    if kind == "manifest":
        # Catalog entries are paged as well: large DAGs cannot silently hide
        # Steps from the coordinator or overflow a single tool observation.
        steps = payload.get("steps") or []
        artifacts = payload.get("artifacts") or []
        criteria = _plan_acceptance_criteria(payload)
        verification = payload.get("verification") or {}
        verification_issues = verification.get("issues") or []

        def step_entry(position: int, step: dict[str, Any]) -> dict[str, Any]:
            result = step.get("result")
            serialized = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
            inline = len(serialized) <= 1_500
            return {
                "kind": "step", "index": position,
                "key": str(step.get("key") or "")[:120],
                "step_kind": str(step.get("kind") or "")[:80],
                "status": step.get("status"),
                "result_pointer": f"/steps/{position}/result",
                **({"contract_index": position,
                    "declaration_basis": (step["delivery_contract"].get("declaration") or {}).get("basis"),
                    "requires_semantic_assessment": True}
                   if step.get("delivery_contract") else {}),
                **({"citation_pointer": f"/steps/{position}/result/text"}
                   if isinstance(result, dict) and isinstance(result.get("text"), str) else {}),
                "result_chars": len(serialized),
                "result_complete": inline,
                **({"result": result} if inline else {}),
            }

        catalog = [
            step_entry(position, step)
            for position, step in enumerate(steps)
        ] + [
            {
                "kind": "artifact", "index": position,
                "document_id": artifact.get("document_id"),
                "fs_path": str(artifact.get("fs_path") or "")[:160],
                "pointer": f"/artifacts/{position}/text",
                **({"citation_pointer": f"/artifacts/{position}/text", "value_type": "string"}
                   if isinstance(artifact.get("text"), str) else {}),
                "text_chars": len(artifact.get("text") or ""),
                "text_truncated": bool(artifact.get("text_truncated")),
                "text_available": isinstance(artifact.get("text"), str),
                "size": artifact.get("size"),
                "source_sha256": artifact.get("source_sha256"),
            }
            for position, artifact in enumerate(artifacts)
        ] + [
            {
                "kind": "criterion", "index": position,
                "key": criterion.get("key"),
                "check_id": f"criterion:{position}:{criterion.get('key')}",
                "deliverable_name": str(criterion.get("deliverable_name") or "")[:120],
                "description": str(criterion.get("description") or "")[:280],
                "description_truncated": len(str(criterion.get("description") or "")) > 280,
                "evidence_step_keys": [str(key)[:128] for key in (criterion.get("evidence_step_keys") or [])[:8]],
                "evidence_steps_truncated": len(criterion.get("evidence_step_keys") or []) > 8,
            }
            for position, criterion in enumerate(criteria)
        ] + [
            {"kind": "verification", "index": position, "code": issue.get("code"),
             "step_id": issue.get("step_id"), "step_key": issue.get("step_key")}
            for position, issue in enumerate(verification_issues)
        ]
        if offset > len(catalog):
            raise DeliveryInspectionError(
                "manifest offset exceeds catalog length", path="$.offset",
                schema_rule="maximum", expected=len(catalog),
            )
        end = min(offset + 12, len(catalog))
        task_preview = (
            request if len(request_content) <= 1_200 else {
                "id": request.get("id"),
                "title": str(request.get("title") or "")[:160],
                "request_truncated_in_manifest": True,
                "request_chars": len(request_content),
                "next_action": "Read kind=request pages for the complete original request.",
            }
        )
        while True:
            page = {
                **common, "kind": kind,
                "task": task_preview, "plan_id": payload.get("plan_id"),
                "step_count": len(steps), "artifact_count": len(artifacts),
                "criterion_count": len(criteria),
                "verification": {
                    "status": verification.get("status", "verified"),
                    "issue_count": len(verification_issues),
                    "execution_replay": "not_authorized_by_inspection",
                },
                "catalog": catalog[offset:end],
                "next_offset": end if end < len(catalog) else None,
            }
            if len(json.dumps(page, ensure_ascii=False, default=str)) <= INSPECTION_RESULT_MAX_CHARS:
                return page
            if end <= offset + 1:
                raise TaskAcceptanceError("A delivery manifest entry exceeds the safe inspection page size.")
            end -= 1
    if kind == "citations":
        steps = payload.get("steps") or []
        if index >= len(steps):
            raise DeliveryInspectionError(
                "step index is outside the delivered evidence", path="$.index",
                schema_rule="exclusiveMaximum", expected=len(steps),
            )
        citations, next_offset, omitted = _citation_page(
            _step_citations(index, steps[index].get("result")), offset=offset,
            budget=INSPECTION_RESULT_MAX_CHARS - 400,
        )
        return {**common, "kind": kind, "index": index, "citations": citations,
                "offset": offset, "next_offset": next_offset,
                **({"citation_hints_omitted": omitted} if omitted else {})}
    if kind == "request":
        content = request_content
        pointer = "/task"
    elif kind == "criterion":
        criteria = _plan_acceptance_criteria(payload)
        if index >= len(criteria):
            raise DeliveryInspectionError(
                "criterion index is outside the Plan acceptance contract", path="$.index",
                schema_rule="exclusiveMaximum", expected=len(criteria),
            )
        content = json.dumps(criteria[index], sort_keys=True, ensure_ascii=False, default=str)
        pointer = f"/plan_dag/metadata/acceptance_contract/criteria/{index}"
    elif kind == "verification":
        issues = (payload.get("verification") or {}).get("issues") or []
        if index >= len(issues):
            raise DeliveryInspectionError(
                "verification index is outside the delivery diagnostics", path="$.index",
                schema_rule="exclusiveMaximum", expected=len(issues),
            )
        content = json.dumps(issues[index], sort_keys=True, ensure_ascii=False, default=str)
        pointer = f"/verification/issues/{index}"
    elif kind == "contract":
        steps = payload.get("steps") or []
        if index >= len(steps) or not steps[index].get("delivery_contract"):
            raise DeliveryInspectionError(
                "contract index is outside the declared delivery contracts", path="$.index",
                schema_rule="available_contract",
                expected={"source": "manifest catalog contract_index", "exclusiveMaximum": len(steps)},
            )
        content = json.dumps(steps[index]["delivery_contract"], sort_keys=True, ensure_ascii=False, default=str)
        pointer = f"/steps/{index}/delivery_contract"
    elif kind in {"step", "artifact"}:
        items = payload.get("steps" if kind == "step" else "artifacts") or []
        if index >= len(items):
            raise DeliveryInspectionError(
                f"{kind} index is outside the delivered evidence", path="$.index",
                schema_rule="exclusiveMaximum", expected=len(items),
            )
        item = items[index]
        pointer = f"/steps/{index}/result" if kind == "step" else f"/artifacts/{index}/text"
    if kind == "step" and offset == 0:
        citations, citations_next_offset, citation_hints_omitted = _citation_page(
            _step_citations(index, item.get("result")), offset=0,
        )
    if kind == "step":
        content = json.dumps(item.get("result"), sort_keys=True, ensure_ascii=False, default=str)
    elif kind == "artifact":
        content = item.get("text")
        if not isinstance(content, str):
            content = ""
    if offset > len(content):
        raise DeliveryInspectionError(
            "content offset exceeds delivered evidence length", path="$.offset",
            schema_rule="maximum", expected=len(content),
        )
    limit = min(INSPECTION_CHUNK_CHARS, len(content) - offset)

    def content_page(chars: int) -> dict[str, Any]:
        end = offset + chars
        return {
            **common, "kind": kind, "index": index,
            **({"result_pointer": pointer} if kind == "step" else {}),
            **({"source_pointer": pointer} if kind in {"request", "criterion", "verification", "contract"} else {}),
            **({"pointer": pointer} if kind in {"artifact", "evidence"} else {}),
            **({"citation_pointer": f"{pointer}/text"}
               if kind == "step" and isinstance(item.get("result"), dict)
               and isinstance(item["result"].get("text"), str) else {}),
            **({"citations": citations, "citations_next_offset": citations_next_offset}
               if kind == "step" and offset == 0 else {}),
            **({"citation_hints_omitted": citation_hints_omitted}
               if kind == "step" and offset == 0 and citation_hints_omitted else {}),
            **({"citation_pointer": pointer, "value_type": _evidence_type(value)}
               if kind == "evidence" else {}),
            **({"citation_pointer": pointer, "value_type": "string"}
               if kind == "artifact" and isinstance(item.get("text"), str) else {}),
            "content": content[offset:end], "offset": offset,
            "returned_chars": chars, "total_chars": len(content),
            "next_offset": end if end < len(content) else None,
            **({"text_truncated": bool(item.get("text_truncated"))} if kind == "artifact" else {}),
            **({"text_truncated": bool(artifact_evidence.get("text_truncated"))}
               if artifact_evidence is not None else {}),
        }

    low, high = 0, limit
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(content_page(middle), ensure_ascii=False, default=str)) <= INSPECTION_RESULT_MAX_CHARS:
            low = middle
        else:
            high = middle - 1
    if limit and not low:
        raise TaskAcceptanceError("Delivery content cannot fit a safe inspection page.")
    return content_page(low)


def required_delivery_inspection_requests(snapshot: TaskAcceptanceSnapshot) -> list[dict[str, Any]]:
    """Required page identities and their explicit read-only parameter contract.

    A direct submit with one true citation is not proof that the rest of the
    delivered work was inspected. The host never infers semantic correctness;
    it only ensures the deciding loop received every bounded evidence page.
    """
    payload = snapshot.payload
    steps = payload.get("steps") or []
    artifacts = payload.get("artifacts") or []
    result: list[dict[str, Any]] = []

    def add_pages(kind: str, *, index: int = 0) -> None:
        offset = 0
        while True:
            page_id = (
                f"{kind}:{offset}" if kind in {"manifest", "request"}
                else f"{kind}:{index}:{offset}"
            )
            result.append({
                "page_id": page_id,
                "arguments": DeliveryInspectionRequest(kind=kind, index=index, offset=offset).as_arguments(),
            })
            page = inspect_task_delivery(snapshot, kind=kind, index=index, offset=offset)
            next_offset = page["next_offset"]
            if next_offset is None:
                return
            if next_offset <= offset:
                raise TaskAcceptanceError("Delivery inspection page made no progress.")
            offset = next_offset

    add_pages("manifest")
    for index, criterion in enumerate(_plan_acceptance_criteria(payload)):
        if (len(str(criterion.get("description") or "")) > 280
                or len(criterion.get("evidence_step_keys") or []) > 8):
            add_pages("criterion", index=index)
    request_content = json.dumps(payload.get("task") or {}, sort_keys=True, ensure_ascii=False, default=str)
    if len(request_content) > 1_200:
        add_pages("request")
    for kind, items in (("step", steps), ("artifact", artifacts)):
        for index, _ in enumerate(items):
            add_pages(kind, index=index)
    for index, step in enumerate(steps):
        if step.get("delivery_contract"):
            add_pages("contract", index=index)
    for index, _ in enumerate((payload.get("verification") or {}).get("issues") or []):
        add_pages("verification", index=index)
    return result


def required_delivery_inspections(snapshot: TaskAcceptanceSnapshot) -> list[str]:
    """Preserve durable checkpoint identities independently of model addressing."""
    return [page["page_id"] for page in required_delivery_inspection_requests(snapshot)]


async def build_task_acceptance_snapshot(db, *, task, plan, steps, claim, inspection: bool = False) -> TaskAcceptanceSnapshot:
    """Read exact delivered Documents without projecting/rebinding their origin.

    The caller owns the short submission transaction. The artifact verifier
    uses nonblocking shared file locks; no model/provider I/O occurs here.
    """
    from packages.core.services.artifact_delivery import (
        ArtifactDeliveryError,
        resolve_artifact_delivery_submission,
        verified_submission_refs,
        verify_artifact_delivery_refs,
    )
    from packages.core.contracts.step_execution import STEP_EXECUTION_CONTRACT_PARAM, StepDelivery
    from packages.core.contracts.artifact_delivery import (
        artifact_delivery_step_binding, readonly_artifact_delivery, reused_artifact_snapshots,
    )
    from packages.core.services.task_requester_identity import (
        TaskRequesterIdentityError,
        resolve_task_execution_user_id,
    )

    step_payloads = []
    verification_issues: list[dict[str, Any]] = []
    step_positions = {str(step.id): index for index, step in enumerate(sorted(steps, key=lambda row: (row.step_key, row.id)))}

    def verification_failure(exc, step=None) -> None:
        # Inspection is an observation, not submission. Preserve the facts
        # already gathered when one reference cannot be verified; completion
        # still takes the strict path and must pass the same file/ACL gates.
        error = TaskAcceptanceError(str(exc), code=exc.code, details=getattr(exc, "details", {}))
        if not inspection or error.retryable:
            raise error from exc
        issue = {
            "code": error.code, "message": str(error), "details": error.details,
            "failure_scope": "delivery_verification", "action_executed": False,
            "file_existence": "not_established_by_this_check",
            "execution_replay": "not_authorized_by_inspection",
        }
        if step is not None:
            issue.update({
                "step_id": str(step.id), "step_key": step.step_key,
                "step_status": step.step_status,
                "result_pointer": f"/steps/{step_positions[str(step.id)]}/result",
                # Historical receipts are observations, not a declaration of
                # final delivery or proof of current file bytes/access.
                "recorded_artifacts": [
                    {key: ref[key] for key in ("source", "document_id", "fs_path") if key in ref}
                    for ref in (step.evidence_refs or [])
                    if isinstance(ref, dict) and ref.get("kind") == "artifact"
                ][:MAX_REVIEW_ARTIFACTS],
            })
        verification_issues.append(sanitize_sensitive_payload(sanitize_approval_credentials(issue)))

    refs: list[dict[str, Any]] = []
    reused_snapshots: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    submitted_ref_count = 0
    submitted_by_step: dict[str, list[dict[str, Any]]] = {}
    for step in sorted(steps, key=lambda row: (row.step_key, row.id)):
        step_payloads.append({
            "id": step.id, "key": step.step_key, "kind": step.kind,
            "status": step.step_status, "attempt_count": step.attempt_count,
            "params": step.params, "depends_on": step.depends_on,
            "result": step.result, "evidence_refs": step.evidence_refs,
            "output_schema": step.expected_output_schema,
        })
        contract = (step.params or {}).get(STEP_EXECUTION_CONTRACT_PARAM) or {}
        # Tool artifacts may be intermediate inputs. An explicit inline
        # contract cannot be promoted to Task delivery by evidence_refs.
        if contract.get("delivery") == StepDelivery.INLINE.value:
            continue
        if not (
            contract.get("delivery") == StepDelivery.ARTIFACT.value
            or any(ref.get("kind") == "artifact" for ref in step.evidence_refs or [] if isinstance(ref, dict))
        ):
            continue
        step_payloads[-1]["delivery_contract"] = {
            "execution": contract, "output_schema": step.expected_output_schema,
            "semantic_acceptance": "not_established_by_file_identity",
        }
        try:
            submission = resolve_artifact_delivery_submission(step.result, output_schema=step.expected_output_schema)
            submitted = submission.refs
            step_payloads[-1]["delivery_contract"].update({
                "declaration": submission.declaration, "submitted_refs": submitted,
            })
        except ArtifactDeliveryError as exc:
            verification_failure(exc, step)
            continue
        submitted_by_step[str(step.id)] = submitted
        submitted_ref_count += len(submitted)
        if submitted_ref_count > 32:
            raise TaskAcceptanceError("Too many artifact delivery references.", code="artifact_reference_limit")
        if readonly_artifact_delivery(step.params):
            saved = reused_artifact_snapshots(step)
            if not saved:
                verification_failure(TaskAcceptanceError(
                    "The read-only artifact Step has no current host-verified delivery receipt.",
                    code="artifact_evidence_missing",
                ), step)
            reused_snapshots.extend(saved)
        for ref in submitted:
            key = json.dumps(ref, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen_refs:
                refs.append(ref)
                seen_refs.add(key)
    artifacts: list[dict[str, Any]] = []
    if refs:
        try:
            user_id = await resolve_task_execution_user_id(db, task)
            verified = await verify_artifact_delivery_refs(
                db, entity_id=task.entity_id, workspace_id=task.workspace_id,
                user_id=user_id, refs=refs,
                text_limit=MAX_ARTIFACT_TEXT_CHARS,
            )
        except ArtifactDeliveryError as exc:
            verification_failure(exc)
            verified = None
        except TaskRequesterIdentityError as exc:
            raise TaskAcceptanceError(str(exc)) from exc
        artifacts = list(verified.snapshots) if verified is not None else []
        # Aliases consume input budget, but only verified canonical Documents
        # consume the bounded artifact-content review budget.
        if len(artifacts) > MAX_REVIEW_ARTIFACTS:
            raise TaskAcceptanceError("Too many artifacts for one bounded Task acceptance review.")
        # Verify the version actually delivered by each read-only Step, not
        # just whatever currently lives at that address before first review.
        # Compare the one host read above rather than hashing each file twice.
        current_by_id = {item["document_id"]: item for item in artifacts}
        identity_keys = ("document_id", "fs_path", "document_updated_at", "source_sha256")
        for saved in reused_snapshots:
            if verification_issues and not artifacts:
                break
            current = current_by_id.get(saved.get("document_id"), {})
            if any(key not in saved or current.get(key) != saved[key] for key in identity_keys):
                verification_failure(TaskAcceptanceError(
                    "A read-only artifact changed after Step completion; read and deliver the current version before review.",
                    code="artifact_changed",
                ))
    manifest_steps = []
    for step in sorted(steps, key=lambda row: (row.step_key, row.id)):
        if verification_issues and not artifacts:
            manifest_steps.append({**artifact_delivery_step_binding(step), "refs": []})
            continue
        try:
            delivered = verified_submission_refs(
                submitted_by_step.get(str(step.id), []), artifacts, entity_id=task.entity_id,
            )
        except ArtifactDeliveryError as exc:
            verification_failure(exc, step)
            delivered = []
        manifest_steps.append({**artifact_delivery_step_binding(step), "refs": delivered})
    return acceptance_snapshot({
        "task": {
            "id": task.id, "entity_id": task.entity_id, "workspace_id": task.workspace_id,
            "title": task.title, "description": task.description,
            "details": task.details, "expected_output": task.expected_output,
        },
        "plan_id": plan.id, "plan_dag": plan.plan_dag,
        "claim": {"run_id": claim.run_id, "wake_generation": claim.wake_generation,
                  "claim_token": claim.claim_token},
        "steps": step_payloads, "artifacts": artifacts,
        "artifact_manifest": {"plan_id": str(plan.id), "steps": manifest_steps},
        "verification": {"status": "unverified" if verification_issues else "verified", "issues": verification_issues},
    })


def _load_review_json(raw: str) -> Any:
    """Model assessments must not depend on ambiguous duplicate-key parsing."""
    def unique_object(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError("duplicate review key")
            result[key] = child
        return result

    return json.loads(raw, object_pairs_hook=unique_object)


def _exact_container_quote(quote: str, value: dict | list) -> bool:
    """Allow complete structured evidence, never a partial or coerced match."""
    try:
        cited = _load_review_json(quote)
        # Canonical JSON preserves array order and distinguishes true/1/1.0.
        # Do not unescape twice or accept a substring of an opaque container.
        return json.dumps(cited, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        return False


class _AcceptanceCitationError(ValueError):
    """Locate an invalid submitted citation without reflecting evidence text."""

    def __init__(
        self, message: str, *, index: int, field: str | None = None,
        pointer: Any = None, expected: str | None = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = {
            "path": f"/assessment/checks/{index}" + (f"/{field}" if field else ""),
            "citation_index": index,
        }
        if isinstance(pointer, str) and len(pointer) <= 1000:
            self.details["source_pointer"] = pointer
        if expected is not None:
            self.details["expected"] = expected


def _grounded_acceptance_checks(raw_checks: Any, snapshot: TaskAcceptanceSnapshot) -> tuple[TaskAcceptanceCheck, ...]:
    """Resolve evidence in the supplied snapshot, never imagined hidden output.

    This proves that citations exist, not that a model covered every semantic
    requirement. Requirements/prompts/schemas are not delivered evidence.
    """
    if not isinstance(raw_checks, list) or len(raw_checks) > MAX_ACCEPTANCE_CHECKS:
        raise ValueError("invalid acceptance checks")
    payload = snapshot.payload
    checks = []
    for index, check in enumerate(raw_checks):
        if not isinstance(check, dict) or set(check) != {"criterion", "pointer", "quote"}:
            raise _AcceptanceCitationError("invalid acceptance check fields", index=index)
        criterion, pointer, quote = (check[key] for key in ("criterion", "pointer", "quote"))
        for field, value in check.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise _AcceptanceCitationError(
                    "empty or oversized acceptance citation", index=index,
                    field=field, pointer=pointer,
                )
        try:
            value = _evidence_value(payload, pointer)
        except ValueError as exc:
            raise _AcceptanceCitationError(
                str(exc), index=index, field="pointer", pointer=pointer,
            ) from exc
        if isinstance(value, (dict, list)):
            matches = _exact_container_quote(quote, value)
            expected = "complete JSON value, preserving types and array order"
        elif isinstance(value, str):
            matches = quote in value
            expected = "exact substring of the raw evidence string"
        else:
            matches = quote == json.dumps(value, ensure_ascii=False)
            expected = "exact JSON representation of the evidence value"
        if not matches:
            parts = pointer.split("/")
            if (parts[1] == "steps" and len(parts) == 4 and isinstance(value, dict)
                    and isinstance(value.get("text"), str) and quote in value["text"]):
                raise _AcceptanceCitationError(
                    f"citation points to a result object; cite /steps/{parts[2]}/result/text "
                    "for this text excerpt", index=index, field="pointer", pointer=pointer,
                )
            raise _AcceptanceCitationError(
                "citation quote is not present in delivered evidence", index=index,
                field="quote", pointer=pointer, expected=expected,
            )
        checks.append(TaskAcceptanceCheck(criterion.strip(), pointer, quote))
    return tuple(checks)


def _criterion_check_uses_declared_evidence(
    payload: dict[str, Any], criterion: dict[str, Any], check: TaskAcceptanceCheck,
) -> bool:
    """Bind a Plan criterion to its declared Step or that Step's delivered file."""
    allowed_keys = set(criterion.get("evidence_step_keys") or [])
    allowed_steps = [
        (index, step) for index, step in enumerate(payload.get("steps") or [])
        if step.get("key") in allowed_keys
    ]
    parts = check.pointer.split("/")
    if len(parts) < 4 or not parts[2].isdigit():
        return False
    index = int(parts[2])
    if parts[1] == "steps":
        return any(step_index == index for step_index, _ in allowed_steps)
    if parts[1] != "artifacts" or index >= len(payload.get("artifacts") or []):
        return False
    artifact = payload["artifacts"][index]
    manifest = payload.get("artifact_manifest")
    if isinstance(manifest, dict):
        allowed_ids = {step.get("id") for _, step in allowed_steps}
        return any(
            delivery.get("step_id") in allowed_ids
            and any(ref.get("document_id") == artifact.get("document_id")
                    and ref.get("fs_path") == artifact.get("fs_path") for ref in delivery.get("refs") or [])
            for delivery in manifest.get("steps") or []
        )
    # Compatibility for historical snapshots; newly prepared submissions
    # always use the verifier's exact per-Step ownership projection above.
    from packages.core.services.artifact_delivery import (
        ArtifactDeliveryError,
        _identity,
        submitted_artifact_refs,
    )

    entity_id = str((payload.get("task") or {}).get("entity_id") or "")
    for _, step in allowed_steps:
        try:
            refs = submitted_artifact_refs(step.get("result"), output_schema=step.get("output_schema"))
        except ArtifactDeliveryError:
            continue
        for ref in refs:
            try:
                document_id, path = _identity(ref, entity_id)
            except ArtifactDeliveryError:
                continue
            if ((document_id and document_id == artifact.get("document_id"))
                    or (path and path == artifact.get("fs_path"))):
                return True
    return False


def parse_task_acceptance_review(raw: str, snapshot: TaskAcceptanceSnapshot) -> TaskAcceptanceReview:
    """Model text can describe quality, never choose a different evidence digest."""
    try:
        data = _load_review_json(raw)
        if not isinstance(data, dict) or set(data) not in (
            {"verdict", "evidence", "issues"}, {"verdict", "evidence", "issues", "checks"},
        ):
            raise ValueError("unexpected review fields")
        verdict = TaskAcceptanceVerdict(data["verdict"])
        evidence, issues = data["evidence"], data["issues"]
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
            raise ValueError("missing review evidence")
        if (not isinstance(issues, list) or len(issues) > 12
                or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in issues)):
            raise ValueError("invalid review issues")
        if (verdict is TaskAcceptanceVerdict.ACCEPTED) != (not issues):
            raise ValueError("review verdict and issues disagree")
        checks = _grounded_acceptance_checks(data.get("checks", []), snapshot)
        if verdict is TaskAcceptanceVerdict.ACCEPTED and not checks:
            raise ValueError("acceptance requires grounded evidence for every explicit requirement")
        if verdict is TaskAcceptanceVerdict.ACCEPTED:
            payload = snapshot.payload
            if (payload.get("verification") or {}).get("status") == "unverified":
                raise ValueError("delivery verification is unresolved; inspect the verification diagnostics before deciding the next action")
            criteria = _plan_acceptance_criteria(payload)
            required_keys = {
                f"criterion:{index}:{item['key']}"
                for index, item in enumerate(criteria)
                if isinstance(item.get("key"), str) and item["key"]
            }
            missing = required_keys - {check.criterion for check in checks}
            if missing:
                raise ValueError(
                    "missing grounded checks for Plan acceptance criteria: "
                    + ", ".join(sorted(missing)[:8])
                )
            for index, criterion in enumerate(criteria):
                check_id = f"criterion:{index}:{criterion.get('key')}"
                if not any(
                    check.criterion == check_id
                    and _criterion_check_uses_declared_evidence(payload, criterion, check)
                    for check in checks
                ):
                    raise ValueError(f"Plan criterion {check_id} lacks evidence from its declared Step")
    except (TypeError, ValueError, KeyError, RecursionError) as exc:
        cause = sanitize_sensitive_payload(sanitize_approval_credentials(str(exc)))
        location = (
            sanitize_sensitive_payload(sanitize_approval_credentials(exc.details))
            if isinstance(exc, _AcceptanceCitationError) else {}
        )
        raise TaskAcceptanceError(
            "Task coordinator assessment is invalid; completion was not accepted.",
            code="invalid_assessment",
            details={"cause": cause[:2000], "exception_type": type(exc).__name__, **location},
        ) from exc
    return TaskAcceptanceReview(snapshot.digest, verdict, evidence.strip(), tuple(issues), checks)
