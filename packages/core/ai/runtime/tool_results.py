from __future__ import annotations

import json
from enum import StrEnum
from typing import Any


class RuntimePreflightValidationError(str):
    """Host-only rejection before mutation or terminal-control acceptance.

    Runtime input/binding checks run before approval and handler I/O. The
    readonly artifact submit guard can also create it after verification reads,
    but before capturing a terminal result. Both prove that the rejected call
    made no mutation, so the live loop can correct through the normal gates.
    Tool/provider JSON and serialized checkpoints cannot recreate that proof;
    this carrier never settles an already-recorded effect intent.
    """


class RuntimeAlternatePathToolResult(str):
    """Host proof of a pre-I/O failure with an already-settled approval.

    The serialized JSON describes the failure to the model, but cannot
    recreate this in-process permission to continue with read-only tools.
    """


class RuntimeGateToolResult(str):
    """Host proof that the runtime approval/governance gate (guard_runtime_tool_action)
    stopped the call before its handler ran — approvals pending, human declines,
    permission/governance denials, consumption blocks.

    Minted only where the runtime approval gate returns a blocking result
    instead of executing the tool, and where the AgentLoop replays a stored
    ``pending_tool_result`` that its host wrote with ``gate: true`` (a durable
    host that decided the parked call itself, e.g. a rejected approval; honoured
    only for a loop run with ``hitl_resume_checkpoint=True``). Tool or provider
    JSON that merely looks like a gate (``approval_required``, ``rejected`` ...)
    cannot recreate this proof.
    """


class RuntimeDelegatedPauseToolResult(str):
    """Host proof that a delegated agent paused (to ask the person) and that
    its whole ledger reached the calling tool call.

    The pause itself is no effect: whatever the child did before it asked is
    in its merged child ledger. Minted only by the delegation host after the
    hand-off succeeded; the same JSON without it stays an unconfirmed effect.
    """


class RuntimeNoEffectReason(StrEnum):
    DOCUMENT_TARGET_EXISTS = "document_target_exists"
    PATCH_TARGET_NOT_FOUND = "patch_target_not_found"
    CHROME_PUBLICATION_FRESH_READ = "chrome_publication_fresh_read"
    VIDEO_REFERENCE_DENIED = "video_reference_denied"
    VIDEO_REFERENCE_UNAVAILABLE = "video_reference_unavailable"


def _proves_unapplied(payload: dict[str, Any]) -> bool:
    details = payload.get("details")
    return isinstance(details, dict) and details.get("applied") is False


class RuntimeNoEffectToolResult(str):
    """Host proof that this exact handler call stopped before any mutation.

    Unlike input preflight, this can follow an already-persisted intent. The
    Runtime must durably settle that intent under its current execution fence
    before giving the loop permission to correct the call. JSON, provider
    errors and serialized checkpoints cannot mint this in-process authority.
    """

    reason: RuntimeNoEffectReason

    def __new__(cls, display_text: str, *, reason: RuntimeNoEffectReason):
        instance = super().__new__(cls, display_text)
        instance.reason = RuntimeNoEffectReason(reason)
        return instance

    def __getnewargs_ex__(self):
        return (str(self),), {"reason": self.reason}

    @classmethod
    def document_target_exists(cls) -> RuntimeNoEffectToolResult:
        from packages.core.ai.runtime.tool_error_contract import tool_error_payload

        return cls(
            json.dumps(tool_error_payload(
                tool_name="generate_file",
                code="file_already_exists",
                message="The document target already exists; this call did not create or change it.",
                details={"created": False, "modified": False},
            )),
            reason=RuntimeNoEffectReason.DOCUMENT_TARGET_EXISTS,
        )

    @classmethod
    def patch_target_not_found(cls, display_text: str) -> RuntimeNoEffectToolResult:
        from packages.core.ai.runtime.tool_error_contract import tool_error_payload

        payload = json.loads(display_text)
        payload = tool_error_payload(
            tool_name="patch_file",
            code=payload["error"],
            message="The exact patch target was not found; this call did not change a file.",
            details={
                key: value for key, value in payload.items()
                if key not in {"error", "hint", "recovery_hint"}
            },
        )
        return cls(json.dumps(payload, ensure_ascii=False), reason=RuntimeNoEffectReason.PATCH_TARGET_NOT_FOUND)

    @classmethod
    def chrome_publication_fresh_read(cls, display_text: str) -> RuntimeNoEffectToolResult:
        payload = json.loads(display_text)
        if not isinstance(payload, dict) or payload.get("action_executed") is not False:
            raise ValueError("Chrome publication was not proven unexecuted")
        return cls(display_text, reason=RuntimeNoEffectReason.CHROME_PUBLICATION_FRESH_READ)

    _VIDEO_REFERENCE_ERRORS = {
        RuntimeNoEffectReason.VIDEO_REFERENCE_DENIED: "permission_denied",
        RuntimeNoEffectReason.VIDEO_REFERENCE_UNAVAILABLE: "reference_unavailable",
    }

    @classmethod
    def _video_reference(cls, display_text: str, reason: RuntimeNoEffectReason) -> RuntimeNoEffectToolResult:
        payload = json.loads(display_text)
        if (
            not isinstance(payload, dict)
            or payload.get("error") != cls._VIDEO_REFERENCE_ERRORS[reason]
            or not _proves_unapplied(payload)
        ):
            raise ValueError("Video Edit reference rejection was not proven unapplied")
        return cls(display_text, reason=reason)

    @classmethod
    def video_reference_denied(cls, display_text: str) -> RuntimeNoEffectToolResult:
        """video_edit rejected a reference's Workspace scope or ACL before staging.

        Only the reference preflight that runs before the project directory,
        the sandbox or the session record is touched may mint this proof.
        """
        return cls._video_reference(display_text, RuntimeNoEffectReason.VIDEO_REFERENCE_DENIED)

    @classmethod
    def video_reference_unavailable(cls, display_text: str) -> RuntimeNoEffectToolResult:
        """video_edit found a malformed, missing or over-budget reference in that same preflight."""
        return cls._video_reference(display_text, RuntimeNoEffectReason.VIDEO_REFERENCE_UNAVAILABLE)


    def matches_call(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Allow only the exact local pre-write branches with host proof."""
        try:
            payload = json.loads(str(self))
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        if self.reason is RuntimeNoEffectReason.DOCUMENT_TARGET_EXISTS:
            return (
                tool_name == "generate_file"
                and str(arguments.get("kind") or "").strip().lower() == "document"
                and payload.get("error") == "file_already_exists"
            )
        if self.reason in self._VIDEO_REFERENCE_ERRORS:
            return (
                tool_name == "video_edit"
                and str(arguments.get("operation") or "").strip().lower() in {"start", "prepare", "push_assets"}
                and bool(arguments.get("reference_paths"))
                and payload.get("error") == self._VIDEO_REFERENCE_ERRORS[self.reason]
                and _proves_unapplied(payload)
            )
        if self.reason is RuntimeNoEffectReason.CHROME_PUBLICATION_FRESH_READ:
            return (
                tool_name == "mcp__chrome__click_element"
                and isinstance(arguments.get("operation_intent"), dict)
                and arguments["operation_intent"].get("effect") == "publish"
                and payload.get("action_executed") is False
            )
        return (
            self.reason is RuntimeNoEffectReason.PATCH_TARGET_NOT_FOUND
            and tool_name == "patch_file"
            and payload.get("error") == f"File not found: {arguments.get('path', '')}"
        )


class RuntimeStructuredToolResult(str):
    """Display-compatible tool text carrying an authoritative typed payload."""

    structured_content: Any
    execution_evidence: tuple[dict[str, Any], ...]

    def __new__(
        cls,
        display_text: str,
        *,
        structured_content: Any,
        execution_evidence: tuple[dict[str, Any], ...] = (),
    ) -> RuntimeStructuredToolResult:
        instance = super().__new__(cls, display_text)
        instance.structured_content = structured_content
        instance.execution_evidence = execution_evidence
        return instance
