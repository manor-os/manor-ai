"""Typed state and result contracts for the Chrome Skill child runtime.

Ephemeral browser handles (refs, snapshots, claim and approval tokens) remain
inside the live Harness. A resumable outcome carries only bounded evidence and
the information needed to re-observe and resolve the target on continuation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any, Literal


ChromeSkillStatus = Literal[
    "running",
    "completed",
    "needs_approval",
    "needs_user",
    "blocked",
    "failed",
]

_EPHEMERAL_KEYS = frozenset(
    {
        "ref",
        "refs",
        "snapshot_id",
        "latest_snapshot_id",
        "claimToken",
        "claim_token",
        "approvalToken",
        "approval_token",
    }
)
_MAX_DEPTH = 5
_MAX_LIST_ITEMS = 64
_MAX_STRING_LENGTH = 4000


def _bounded_public_value(value: Any, *, depth: int = 0) -> Any:
    """Copy bounded public resume data while dropping ephemeral handles."""

    if depth >= _MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): _bounded_public_value(item, depth=depth + 1)
            for key, item in list(value.items())[:128]
            if str(key) not in _EPHEMERAL_KEYS
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_bounded_public_value(item, depth=depth + 1) for item in list(value)[:_MAX_LIST_ITEMS]]
    if isinstance(value, str):
        return value[:_MAX_STRING_LENGTH]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_MAX_STRING_LENGTH]


@dataclass
class ChromeSkillRunState:
    """Runtime-owned continuation state for one Chrome Skill execution."""

    run_id: str
    status: ChromeSkillStatus = "running"
    goal: str = ""
    browser_group: dict[str, Any] = field(default_factory=dict)
    control_epoch: str | None = None
    pending_action: dict[str, Any] | None = None
    resume_context: dict[str, Any] = field(default_factory=dict)

    def to_resume_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "goal": self.goal[:_MAX_STRING_LENGTH],
            "browser_group": _bounded_public_value(self.browser_group),
            "control_epoch": self.control_epoch,
            "pending_action": _bounded_public_value(self.pending_action) if self.pending_action else None,
            "resume_context": _bounded_public_value(self.resume_context),
        }


@dataclass
class ChromeSkillOutcome:
    """Typed result consumed by the parent runtime after a Chrome child run."""

    status: ChromeSkillStatus
    skill: str = "chrome"
    run_id: str = ""
    summary: str = ""
    goal_status: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    pending_action: dict[str, Any] | None = None
    resume_context: dict[str, Any] = field(default_factory=dict)
    browser_group: dict[str, Any] = field(default_factory=dict)
    tabs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @classmethod
    def from_state(
        cls,
        state: ChromeSkillRunState,
        *,
        summary: str = "",
        goal_status: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        tabs: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> "ChromeSkillOutcome":
        return cls(
            status=state.status,
            run_id=state.run_id,
            summary=summary,
            goal_status=goal_status,
            evidence=list(evidence or []),
            pending_action=state.pending_action,
            resume_context=state.resume_context,
            browser_group=state.browser_group,
            tabs=list(tabs or []),
            artifacts=list(artifacts or []),
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "skill": self.skill,
            "run_id": self.run_id,
            "summary": self.summary[:_MAX_STRING_LENGTH],
            "goal_status": self.goal_status,
            "evidence": _bounded_public_value(self.evidence),
            "pending_action": _bounded_public_value(self.pending_action) if self.pending_action else None,
            "resume_context": _bounded_public_value(self.resume_context),
            "browser_group": _bounded_public_value(self.browser_group),
            "tabs": _bounded_public_value(self.tabs),
            "artifacts": _bounded_public_value(self.artifacts),
        }
        if self.error:
            payload["error"] = self.error[:_MAX_STRING_LENGTH]
        return payload


def chrome_runtime_state_for_child(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return an isolated copy of the Chrome Harness state for a child loop."""

    if not isinstance(metadata, dict):
        return {}
    state = metadata.get("chrome_runtime_contract_v1")
    return deepcopy(state) if isinstance(state, dict) else {}


def commit_chrome_runtime_state(
    parent_metadata: dict[str, Any] | None,
    child_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy the child Chrome Harness state back into the parent metadata container."""

    contract_key = "chrome_runtime_contract_v1"
    if not isinstance(parent_metadata, dict):
        parent_metadata = {}
    parent_state = parent_metadata.get(contract_key)
    child_state = child_metadata.get(contract_key) if isinstance(child_metadata, dict) else None
    if not isinstance(child_state, dict):
        return deepcopy(parent_state) if isinstance(parent_state, dict) else {}

    committed_state = deepcopy(child_state)
    if isinstance(parent_state, dict):
        parent_state.clear()
        parent_state.update(committed_state)
    else:
        parent_metadata[contract_key] = committed_state
    return deepcopy(committed_state)


def ensure_chrome_run_state(metadata: dict[str, Any], *, goal: str = "") -> dict[str, Any]:
    """Create or continue one Chrome Run inside an isolated runtime metadata map."""

    contract = metadata.setdefault("chrome_runtime_contract_v1", {})
    if not isinstance(contract, dict):
        contract = {}
        metadata["chrome_runtime_contract_v1"] = contract
    contract.setdefault("run_id", f"chrome-{token_urlsafe(12)}")
    if goal and not contract.get("goal"):
        contract["goal"] = goal[:_MAX_STRING_LENGTH]
    return contract


def chrome_outcome_for_result(
    metadata: dict[str, Any] | None,
    *,
    skill: str,
    goal: str,
    content: Any,
    stop_reason: Any,
    error: Any = None,
) -> ChromeSkillOutcome:
    """Translate child-loop and Harness state into a parent-facing outcome."""

    contract = ensure_chrome_run_state(metadata if isinstance(metadata, dict) else {}, goal=goal)
    pending_confirmation = contract.get("pending_chrome_confirmation")
    interruption = contract.get("control_interruption")
    normalized_stop_reason = str(stop_reason or "").strip().lower()
    if error or normalized_stop_reason in {"error", "credit_exhausted"}:
        status: ChromeSkillStatus = "failed"
    elif isinstance(pending_confirmation, dict):
        status = "needs_approval"
    elif isinstance(interruption, dict):
        status = "needs_user"
    elif normalized_stop_reason in {"max_rounds", "cancelled", "canceled"}:
        status = "blocked"
    else:
        status = "completed"

    contract["status"] = status
    pending_action = pending_confirmation if isinstance(pending_confirmation, dict) else interruption
    resume_context = {
        "next": (
            "confirm_action_then_retry_once"
            if status == "needs_approval"
            else "reobserve_and_continue"
            if status in {"needs_user", "blocked"}
            else None
        ),
        "stop_reason": str(stop_reason or "").strip() or None,
    }
    state = ChromeSkillRunState(
        run_id=str(contract.get("run_id") or ""),
        status=status,
        goal=str(contract.get("goal") or goal or ""),
        browser_group={
            key: contract[key]
            for key in ("group_id", "groupId", "session_id", "sessionId", "session_name")
            if contract.get(key)
        },
        control_epoch=str(contract.get("control_epoch") or "") or None,
        pending_action=pending_action,
        resume_context=resume_context,
    )
    outcome = ChromeSkillOutcome.from_state(
        state,
        summary=str(content or ""),
        goal_status=status,
        evidence=[
            {
                "kind": "chrome_runtime",
                "last_tool": contract.get("last_tool"),
                "chrome_call_count": contract.get("chrome_call_count", 0),
                "read_page_call_count": contract.get("read_page_call_count", 0),
            }
        ],
        error=str(error) if error else None,
    )
    outcome.skill = skill or "chrome"
    return outcome
