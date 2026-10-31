"""Host receipt ledger primitives.

``effect_state`` is the host's view of whether a tool call changed the world:

* ``none``        — read-only tool, or host-proven pre-I/O rejection
                    (preflight, no-effect, alternate-path and runtime
                    approval-gate ``RuntimeGateToolResult`` carriers).
* ``confirmed``   — a non-read-only tool returned success.
* ``unconfirmed`` — a non-read-only tool returned an error; the provider may
                    have crossed I/O before failing.

Only host carrier *types* prove "no mutation". Serialized tool or provider
JSON can never downgrade an error to ``none``.

A failed host call carrying ``RuntimeSettledToolResult`` takes the state its
handler proved at that exact failure branch (``none``: refused before it ran,
or ran only in a throwaway networkless sandbox; ``confirmed``: only completed
steps changed anything).

A call that ran a whole child loop (``invoke_skill``) takes its child ledger's
state, carried by the host on ``RuntimeStructuredToolResult.child_effect_state``.
An unconfirmed call can later be confirmed by reconciliation
(``RuntimeToolEvidence``): a host observation of the exact target it reported.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Callable


class EffectState(StrEnum):
    NONE = "none"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


_EFFECT_STATE_RANK = {EffectState.NONE: 0, EffectState.CONFIRMED: 1, EffectState.UNCONFIRMED: 2}


def runtime_aggregate_effect_state(states: Any) -> EffectState:
    """The run-level state: unconfirmed dominates confirmed dominates none."""
    worst = EffectState.NONE
    if isinstance(states, str):  # a bare str is one state, not an iterable of characters
        states = (states,)
    for raw in states or ():
        try:
            state = EffectState(str(raw))
        except ValueError:
            state = EffectState.UNCONFIRMED
        if _EFFECT_STATE_RANK[state] > _EFFECT_STATE_RANK[worst]:
            worst = state
    return worst


# No classification was handed in: classify here.
_UNCLASSIFIED: Any = object()


def _is_read_only(tool_name: str, arguments: dict | None, classification: Any = _UNCLASSIFIED) -> bool:
    """``classification``: one the caller already made (None when it failed).

    True for every call that executes nothing (reads, and the CONTROL calls
    in ``RUNTIME_NO_EFFECT_CONTROL_TOOL_NAMES``).
    """
    from packages.core.ai.runtime.approval_classifier import (
        classify_runtime_tool,
        runtime_call_executes_nothing,
    )

    if classification is _UNCLASSIFIED:
        try:
            classification = classify_runtime_tool(tool_name, arguments if isinstance(arguments, dict) else {})
        except Exception:
            return False
    return classification is not None and runtime_call_executes_nothing(tool_name, classification)


# Statuses that mean "a gate / human decision / refusal", not a completed action.
# When they arrive as plain JSON they prove nothing (any tool, or a child skill
# whose earlier steps already wrote files, can emit them), so they are
# UNCONFIRMED. Only the host carrier RuntimeGateToolResult proves a gate
# stopped the call before its handler ran (a suspension does not: composite
# tools suspend after real work).
_GATE_STATUSES = frozenset({
    "hitl_required", "approval_required", "waiting_human", "handoff_required",
    "rejected", "cancelled", "canceled", "blocked",
})
# Non-success execution outcomes (never a clean success even with a gate flag).
_NON_SUCCESS_STATUSES = frozenset({
    "error", "failed", "failure", "timeout",
    "unknown", "partial", "interrupted",
    "pending", "queued", "running", "submitted",
})
# Non-error statuses that say the outcome is not a clean success.
_INDETERMINATE_STATUSES = frozenset({"unknown", "partial", "interrupted"})


def _has_error_signal(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("error")
        or payload.get("isError") is True
        or payload.get("ok") is False
        or payload.get("success") is False
    )


def _incidental_gate_flag(payload: dict[str, Any] | None) -> bool:
    """True when the payload reports its own terminal success status.

    ``{"status": "ready_for_review", "approval_required": True}`` (video_edit
    review, after registering frame documents) carries the gate flag
    incidentally; the action did run.
    """
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().lower()
    return (
        bool(status)
        and status not in _GATE_STATUSES
        and status not in _NON_SUCCESS_STATUSES
        and not _has_error_signal(payload)
    )


def runtime_tool_effect_state(
    tool_name: str, arguments: dict | None, result: Any, *, classification: Any = _UNCLASSIFIED,
) -> EffectState:
    """Host effect state of one tool call.

    A suspended call has no terminal effect state; the ledger records the call
    when it resumes to a terminal result (the loop does not observe suspensions).
    """
    # classification only — never grants NONE
    from packages.core.ai.runtime.chrome_action_outcomes import pending_action_receipt
    from packages.core.ai.runtime.control import RuntimeToolSuspension
    from packages.core.ai.runtime.streams import (
        _runtime_tool_control_status,
        _runtime_tool_result_payload,
        runtime_tool_call_error,
    )
    from packages.core.ai.runtime.tool_results import (
        RuntimeAlternatePathToolResult,
        RuntimeDelegatedPauseToolResult,
        RuntimeDelegatedSettlementToolResult,
        RuntimeGateToolResult,
        RuntimeNoEffectToolResult,
        RuntimePreflightValidationError,
        RuntimeSettledToolResult,
        RuntimeStructuredToolResult,
    )

    # NONE comes only from host proof: these carriers are host-minted in-process
    # (RuntimeAlternatePathToolResult has a single mint site), never from JSON.
    if isinstance(result, (
        RuntimePreflightValidationError,
        RuntimeNoEffectToolResult,
        RuntimeAlternatePathToolResult,
        RuntimeGateToolResult,
        # A delegated child's pause; its effects are in its merged ledger.
        RuntimeDelegatedPauseToolResult,
        # A durable delegation's settled batch; its children's effects are
        # in their merged ledgers.
        RuntimeDelegatedSettlementToolResult,
    )):
        return EffectState.NONE
    if isinstance(result, RuntimeSettledToolResult):
        # The handler's own proof of what its failed call changed (nothing, or
        # only completed steps), minted at the exact failure branch.
        return EffectState(result.effect.value)
    if _is_read_only(tool_name, arguments, classification):
        return EffectState.NONE
    if isinstance(result, RuntimeStructuredToolResult) and result.child_effect_state is not None:
        # A call that ran a whole child loop (a Skill) changed exactly what its
        # child calls changed: the host's child ledger is its effect state, not
        # the reply's status. A Skill that reports "failed" over files it did
        # commit is confirmed; one whose child ran no effectful call is none;
        # an uncertain child call keeps it unconfirmed.
        return runtime_aggregate_effect_state((result.child_effect_state,))
    if isinstance(result, RuntimeToolSuspension):
        return EffectState.UNCONFIRMED
    payload = _runtime_tool_result_payload(result)
    if _runtime_tool_control_status(result) is not None and not _incidental_gate_flag(payload):
        # A gate/decline decoded from plain JSON: the action may have run.
        return EffectState.UNCONFIRMED
    if payload is not None and _has_error_signal(payload):
        return EffectState.UNCONFIRMED
    if runtime_tool_call_error(result) is not None:
        return EffectState.UNCONFIRMED
    if payload is not None:
        if pending_action_receipt(payload):
            return EffectState.UNCONFIRMED
        if str(payload.get("status") or "").strip().lower() in _INDETERMINATE_STATUSES:
            return EffectState.UNCONFIRMED
    return EffectState.CONFIRMED


ReceiptProjection = Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]

# Keys a receipt may carry. Projections copy only these, so a tool cannot
# smuggle arbitrary payload into the ledger.
RECEIPT_FIELDS = (
    "kind", "operation", "document_id", "fs_path", "sha256", "url", "name",
    "external_id", "revision",
)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
    return value if value not in (None, "", [], {}) else None


def _receipt(kind: str, operation: str, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "operation": operation}
    for key in RECEIPT_FIELDS[2:]:
        value = _clean(fields.get(key))
        if value is not None:
            out[key] = value
    return out


def _file_from_ref(operation: str, ref: dict[str, Any], **extra: Any) -> dict[str, Any] | None:
    if not isinstance(ref, dict):
        return None
    receipt = _receipt(
        "file", operation,
        document_id=ref.get("document_id") or ref.get("id"),
        fs_path=ref.get("fs_path") or ref.get("path"),
        url=ref.get("url") or ref.get("viewer_url") or ref.get("result_url"),
        name=ref.get("name"),
        sha256=ref.get("source_sha256") or ref.get("sha256"),
        **extra,
    )
    return receipt if any(k in receipt for k in ("document_id", "fs_path", "url")) else None


def _files_from_refs(operation: str, refs: Any) -> list[dict[str, Any]]:
    return [r for r in (_file_from_ref(operation, item) for item in refs or ()) if r] if isinstance(refs, list) else []


def _code_assets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Imported code-bundle assets. Their ``path`` is bundle-relative, unlike ``files[].path``."""
    bundle = _clean(payload.get("bundle_path"))
    out = []
    for item in payload.get("assets") or ():
        if not isinstance(item, dict):
            continue
        rel = _clean(item.get("path"))
        receipt = _receipt(
            "file", "code_asset",
            document_id=item.get("document_id"),
            fs_path=f"{bundle}/{rel}" if isinstance(bundle, str) and isinstance(rel, str) else None,
            sha256=item.get("sha256"),
        )
        if any(k in receipt for k in ("document_id", "fs_path")):
            out.append(receipt)
    return out


def _generate_file(args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    kind = str(args.get("kind") or payload.get("kind") or "file")
    if isinstance(payload.get("document"), dict) and payload.get("created") is not False:
        receipt = _file_from_ref(kind, payload["document"])
        return [receipt] if receipt else []
    if isinstance(payload.get("files"), list):
        out = _files_from_refs(kind, payload["files"])
        if isinstance(payload.get("assets"), list):
            out.extend(_code_assets(payload))
        return out
    if str(payload.get("status") or "").lower() in {"pending", "queued", "running", "error", "failed"}:
        return []
    if not _clean(payload.get("fs_path")) and payload.get("saved_to_knowledge") is False:
        return []  # provider-only URL, not a stored artifact
    url = payload.get("image_url") or payload.get("audio_url") or payload.get("video_url") or payload.get("result_url")
    receipt = _receipt("file", kind, fs_path=payload.get("fs_path"), url=url, document_id=payload.get("document_id"))
    return [receipt] if any(k in receipt for k in ("document_id", "fs_path", "url")) else []


def _wait_media_jobs(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict) or str(job.get("status") or "").lower() != "completed":
            continue
        receipt = _file_from_ref("media_job", job, external_id=job.get("job_id"))
        if receipt:
            out.append(receipt)
    return out


def _import_free_music(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    if str(payload.get("status") or "").lower() != "completed":
        return []
    receipt = _file_from_ref("import", payload.get("document") or {})
    return [receipt] if receipt else []


def _video_edit(args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    operation = str(args.get("operation") or "").strip().lower()
    status = str(payload.get("status") or "").lower()
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    if status == "error" and isinstance(details.get("uncertain_targets"), list):
        # A failure cut off mid-write (its handler's write trace): the Workspace
        # files it was writing, with the bytes' hash when known. The call is
        # unconfirmed, so these are never receipts, only targets a later host
        # read can reconcile.
        return [
            _receipt("file", "uncertain_write", fs_path=item.get("fs_path"), sha256=item.get("sha256"))
            for item in details["uncertain_targets"]
            if isinstance(item, dict) and isinstance(item.get("fs_path"), str)
            and not item["fs_path"].startswith("sandbox:")
        ]
    if operation == "review" and status == "ready_for_review":
        out = []
        if _clean(payload.get("review_sha256")) is not None:
            out.append(_receipt(
                "review", "review", sha256=payload.get("review_sha256"),
                external_id=payload.get("session_id"), fs_path=payload.get("project_path"),
            ))
        for item in payload.get("review_artifacts") or []:
            receipt = _file_from_ref("review_artifact", item)
            if receipt:
                out.append(receipt)
        return out
    if operation == "render" and status == "completed" and isinstance(payload.get("video"), dict):
        receipt = _file_from_ref(
            "render", payload["video"],
            external_id=payload.get("session_id"),
        )
        if receipt is None:
            return []
        if payload.get("source_sha256"):
            receipt["sha256"] = str(payload["source_sha256"])
        return [receipt]
    return []


def _publish_site(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("published") is not True:
        return []
    receipt = _receipt(
        "publication", "publish", url=payload.get("url"),
        external_id=payload.get("site_id"), revision=payload.get("revision"),
    )
    return [receipt] if any(k in receipt for k in ("external_id", "url")) else []


def _completed(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "").strip().lower() == "completed"


def _generate_image(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # Same payload as generate_file(kind="image"), which delegates to this handler.
    return _generate_file({"kind": "image"}, payload)


def _completed_file(operation: str) -> ReceiptProjection:
    """Media tools whose success payload *is* the output file (status=completed, document_id, fs_path, result_url)."""

    def project(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not _completed(payload):
            return []
        receipt = _file_from_ref(operation, payload)
        return [receipt] if receipt else []

    return project


def _derived_poster(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # derive_image_aspect returns {ready, poster}; only a ready poster is an output file.
    if not payload.get("ready"):
        return []
    poster = payload.get("poster") if isinstance(payload.get("poster"), dict) else {}
    receipt = _file_from_ref("derive", {**poster, "name": poster.get("filename")})
    return [receipt] if receipt else []


def _compose_video_timeline(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not _completed(payload):
        return []
    video = _file_from_ref("compose", payload)
    recipe = _file_from_ref("editor_recipe", payload.get("editor_recipe") or {})
    return [r for r in (video, recipe) if r]


def _narration_timeline(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # build_narration_timeline; prepare_narration_timeline returns the same
    # artifacts (it calls build). Its normalized_audio_paths mix new and reused
    # segments and carry no per-path identity, so they are not projected.
    if not _completed(payload):
        return []
    out = []
    for key, operation in (
        ("manifest", "narration_manifest"), ("timeline", "narration_timeline"), ("cues", "subtitle_cues"),
    ):
        receipt = _file_from_ref(operation, payload.get(key) or {})
        if receipt:
            out.append(receipt)
    return out


def _render_frame_samples(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not _completed(payload):
        return []
    return _files_from_refs("frame_sample", payload.get("frames"))


def _snapshot(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # prepare_recording / prepare_creative_studio: the frozen input snapshot they write.
    if not _completed(payload):
        return []
    receipt = _file_from_ref("snapshot", payload.get("snapshot") or {})
    return [receipt] if receipt else []


def _render_recording(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # final_videos repeats outputs[].video under a display name; outputs is the source.
    if not _completed(payload):
        return []
    out: list[dict[str, Any]] = []
    for item in payload.get("outputs") or ():
        if not isinstance(item, dict):
            continue
        video = item.get("video") if isinstance(item.get("video"), dict) else {}
        render = _file_from_ref("render", video)
        if render and _clean(item.get("sha256")) is not None:
            render["sha256"] = str(item["sha256"]).strip()
        out.extend(r for r in (
            render,
            _file_from_ref("editor_recipe", video.get("editor_recipe") or {}),
            _file_from_ref("subtitle", item.get("subtitles") or {}),
            _file_from_ref("burned_subtitle", item.get("burned_subtitles") or {}),
            _file_from_ref("timeline", item.get("timeline") or {}),
        ) if r)
        preview = item.get("preview")
        if isinstance(preview, dict):
            out.extend(_render_frame_samples({}, preview))
    receipt = _file_from_ref("delivery_receipt", payload.get("receipt") or {})
    return out + ([receipt] if receipt else [])


def _creative_file_paths(operation: str) -> ReceiptProjection:
    """compose_creative_publication / compose_creative_dub: files is a list of fs_path strings."""

    def project(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not _completed(payload) or not isinstance(payload.get("files"), list):
            return []
        receipts = (_receipt("file", operation, fs_path=path) for path in payload["files"] if isinstance(path, str))
        return [r for r in receipts if "fs_path" in r]

    return project


def _verify_creative_studio(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # final_files are the deliverables the host re-hashed (sha256 measured, not
    # model-reported); previews and the verification receipt are written here.
    if not _completed(payload) or payload.get("technical_checks_passed") is not True:
        return []
    out = _files_from_refs("verified", payload.get("final_files"))
    out.extend(_files_from_refs("preview", payload.get("previews")))
    receipt = _file_from_ref("verification_receipt", payload.get("receipt") or {})
    return out + ([receipt] if receipt else [])


def _patch_file(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # A rolled-back projection reports patched=False with an error.
    if payload.get("patched") is not True:
        return []
    receipt = _receipt(
        "file", "patch", document_id=payload.get("document_id"),
        fs_path=payload.get("path"), sha256=payload.get("source_sha256"),
    )
    return [receipt] if any(k in receipt for k in ("document_id", "fs_path")) else []


def _replace_file(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # unchanged=True (identical bytes) writes nothing and reports replaced=False.
    if payload.get("replaced") is not True:
        return []
    receipt = _receipt(
        "file", "replace", document_id=payload.get("document_id"),
        fs_path=payload.get("target_path"), sha256=payload.get("target_sha256"),
    )
    return [receipt] if any(k in receipt for k in ("document_id", "fs_path")) else []


def _archive_file_collection(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # files[] also passes through un-archived roles; only items with an
    # archive_path are the copies this call wrote (or re-verified).
    out = []
    for item in payload.get("files") or ():
        if not isinstance(item, dict) or _clean(item.get("archive_path")) is None:
            continue
        receipt = _receipt(
            "file", "archive", document_id=item.get("document_id"), fs_path=item.get("archive_path"),
            url=item.get("viewer_url"), name=item.get("name"), sha256=item.get("source_sha256"),
        )
        out.append(receipt)
    return out


def _code_project(args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = _clean(payload.get("root_path"))
    if isinstance(payload.get("written"), list):
        # write: written[].path is project-relative; deletions leave no artifact.
        out = []
        for item in payload["written"]:
            if not isinstance(item, dict):
                continue
            rel = _clean(item.get("path"))
            receipt = _receipt(
                "file", "code_write", document_id=item.get("document_id"),
                fs_path=f"{root}/{rel}" if isinstance(root, str) and isinstance(rel, str) else None,
                sha256=item.get("sha256"),
            )
            if any(k in receipt for k in ("document_id", "fs_path")):
                out.append(receipt)
        return out
    if str(args.get("action") or "").strip() == "create" and payload.get("created") is True:
        receipt = _receipt(
            "code_project", "create", external_id=payload.get("project_id"),
            fs_path=root, name=payload.get("name"),
        )
        return [receipt] if "external_id" in receipt else []
    return []


def _sandbox_save_result(_args: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    # Both the Knowledge-registered and the filesystem-only save write a file.
    if payload.get("saved") is not True:
        return []
    receipt = _file_from_ref("save_result", payload)
    return [receipt] if receipt else []


RECEIPT_PROJECTIONS: dict[str, ReceiptProjection] = {
    "generate_file": _generate_file,
    "wait_media_jobs": _wait_media_jobs,
    "import_free_music": _import_free_music,
    "video_edit": _video_edit,
    "publish_site": _publish_site,
    "generate_image": _generate_image,
    "merge_videos": _completed_file("merge"),
    "still_to_video": _completed_file("render"),
    "derive_image_aspect": _derived_poster,
    "normalize_audio_loudness": _completed_file("normalize"),
    "align_subtitles": _completed_file("subtitle"),
    "compose_video_timeline": _compose_video_timeline,
    "build_narration_timeline": _narration_timeline,
    "prepare_narration_timeline": _narration_timeline,
    "render_frame_samples": _render_frame_samples,
    "prepare_recording": _snapshot,
    "render_recording": _render_recording,
    "prepare_creative_studio": _snapshot,
    "verify_creative_studio": _verify_creative_studio,
    "compose_creative_publication": _creative_file_paths("publication"),
    "compose_creative_deck": _creative_file_paths("deck"),
    "compose_creative_dub": _creative_file_paths("dub"),
    "compose_creative_video": _creative_file_paths("video"),
    "patch_file": _patch_file,
    "replace_file": _replace_file,
    "archive_file_collection": _archive_file_collection,
    "code_project": _code_project,
    "sandbox_save_result": _sandbox_save_result,
}


def runtime_project_receipts(tool_name: str, arguments: dict | None, payload: Any) -> list[dict[str, Any]]:
    """Project one successful tool payload into zero or more receipts."""
    projection = RECEIPT_PROJECTIONS.get(str(tool_name or ""))
    if projection is None or not isinstance(payload, dict):
        return []
    try:
        receipts = projection(arguments if isinstance(arguments, dict) else {}, payload)
    except Exception:
        return []
    return [r for r in receipts if isinstance(r, dict)]
