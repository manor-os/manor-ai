from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable

from packages.core.ai.runtime.surfaces import ChatSurface


class TurnExecutionMode(str, Enum):
    LEGACY = "legacy"
    DIRECT_CHAT = "direct_chat"
    TOOL_CHAT = "tool_chat"
    MEDIA = "media"
    WORKSPACE = "workspace"
    AGENT = "agent"


class TurnPromptMode(str, Enum):
    FULL = "full"
    MINIMAL = "minimal"


class TurnToolCatalogMode(str, Enum):
    """How much of the authorized tool catalog is prompt-visible this turn."""

    FULL = "full"
    LEDGER_QUERY = "ledger_query"
    WEB_RESEARCH = "web_research"


class TurnResearchStrategy(str, Enum):
    """Execution graph for turns that require current web evidence."""

    NONE = "none"
    GENERAL_WEB_RESEARCH = "general_web_research"
    PROSPECT_DISCOVERY = "prospect_discovery"


DIRECT_CHAT_HISTORY_TOKEN_BUDGET = 8_000
TOOL_CHAT_HISTORY_TOKEN_BUDGET = 24_000
WORKSPACE_HISTORY_TOKEN_BUDGET = 32_000
# One round budget for every tool-using chat turn (owner, Agent DM,
# Workspace, generation and Slides). Long research and multi-stage artifact
# work finish on their own; the cap only stops a runaway loop.
CHAT_MAX_ROUNDS = 1000

_WEB_RESEARCH_VISIBLE_TOOL_NAMES = (
    "invoke_skill",
    "web_search",
    "web_fetch",
    "browse_web",
    "search_tools",
)

_WORKSPACE_LEDGER_QUERY_VISIBLE_TOOL_NAMES = (
    "read_content_ledger",
    "read_finance_ledger",
    "read_recruiting_ledger",
    "read_relationship_ledger",
    "query_ledger",
    "manor",
    "search_tools",
)
_WORKSPACE_LEDGER_READ_VISIBLE_TOOL_NAMES = frozenset(
    _WORKSPACE_LEDGER_QUERY_VISIBLE_TOOL_NAMES[:4]
)

_GENERATION_CHAT_MODES = frozenset(
    {
        "image",
        "video",
        "audio",
        "document",
        "pdf",
        "slides",
        "sheet",
        "website",
    }
)

@dataclass(frozen=True)
class TurnExecutionPlan:
    """Per-turn cost/performance policy layered over Runtime authorization.

    Runtime profiles and tool surfaces remain the capability boundary.  This
    plan only decides how much of that capability should be loaded for the
    current turn.
    """

    mode: TurnExecutionMode = TurnExecutionMode.LEGACY
    disable_tools: bool = False
    use_fast_model: bool = False
    prompt_mode: TurnPromptMode = TurnPromptMode.FULL
    tool_catalog_mode: TurnToolCatalogMode = TurnToolCatalogMode.FULL
    research_strategy: TurnResearchStrategy = TurnResearchStrategy.NONE
    max_rounds: int | None = None
    history_token_budget: int | None = None
    reason: str = "legacy_runtime"
    confidence: float = 0.0

    @classmethod
    def from_metadata(cls, value: Any) -> "TurnExecutionPlan":
        """Build a typed plan from persisted Runtime metadata.

        Runtime metadata can be replayed from the database, so invalid enum or
        numeric values must fail closed to the legacy plan instead of changing
        the capability surface for a turn.
        """

        if not isinstance(value, dict):
            return cls()
        try:
            max_rounds = value.get("max_rounds")
            history_token_budget = value.get("history_token_budget")
            confidence = value.get("confidence", 0.0)
            if max_rounds is not None and (
                not isinstance(max_rounds, int)
                or isinstance(max_rounds, bool)
                or max_rounds <= 0
            ):
                raise ValueError("max_rounds must be a positive integer")
            if history_token_budget is not None and (
                not isinstance(history_token_budget, int)
                or isinstance(history_token_budget, bool)
                or history_token_budget <= 0
            ):
                raise ValueError("history_token_budget must be a positive integer")
            if isinstance(confidence, bool) or not 0.0 <= float(confidence) <= 1.0:
                raise ValueError("confidence must be between zero and one")
            return cls(
                mode=TurnExecutionMode(str(value.get("mode") or TurnExecutionMode.LEGACY.value)),
                disable_tools=value.get("disable_tools") is True,
                use_fast_model=value.get("use_fast_model") is True,
                prompt_mode=TurnPromptMode(
                    str(value.get("prompt_mode") or TurnPromptMode.FULL.value)
                ),
                tool_catalog_mode=TurnToolCatalogMode(
                    str(
                        value.get("tool_catalog_mode")
                        or TurnToolCatalogMode.FULL.value
                    )
                ),
                research_strategy=TurnResearchStrategy(
                    str(
                        value.get("research_strategy")
                        or TurnResearchStrategy.NONE.value
                    )
                ),
                max_rounds=int(max_rounds) if max_rounds is not None else None,
                history_token_budget=(
                    int(history_token_budget)
                    if history_token_budget is not None
                    else None
                ),
                reason=str(value.get("reason") or "legacy_runtime"),
                confidence=float(confidence),
            )
        except (TypeError, ValueError):
            return cls(reason="invalid_turn_execution_plan_metadata")

    def to_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if value is None:
                continue
            metadata[key] = value.value if isinstance(value, Enum) else value
        return metadata


def _contains_multimodal_content(message: str | list[dict[str, Any]]) -> bool:
    if isinstance(message, str):
        return False
    for item in message:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type and item_type not in {"text", "input_text"}:
            return True
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "").strip().lower()
                if part_type and part_type not in {"text", "input_text"}:
                    return True
    return False


def build_turn_execution_plan(
    *,
    enabled: bool,
    surface: ChatSurface,
    message: str | list[dict[str, Any]],
    message_text: str,
    disable_tools: bool = False,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    agent_subscription_id: str | None = None,
    manual_skill_selected: bool = False,
    editor_context: dict[str, Any] | None = None,
    runtime_metadata: dict[str, Any] | None = None,
) -> TurnExecutionPlan:
    """Build a conservative execution plan from the chat surface and turn metadata."""

    if not enabled:
        return TurnExecutionPlan(reason="chat_turn_routing_v1_disabled")

    metadata = dict(runtime_metadata or {})
    presentation_turn = str(metadata.get("chat_mode") or "").strip().lower() == "slides"
    generation_chat_mode = (
        str(metadata.get("chat_mode") or "").strip().lower()
        in _GENERATION_CHAT_MODES
    )
    forced_tool_calls = metadata.get("forced_tool_calls")
    if isinstance(forced_tool_calls, list) and forced_tool_calls:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.MEDIA,
            # Dedicated generation modes may need to wait, retry, compose,
            # render, and verify.
            max_rounds=CHAT_MAX_ROUNDS,
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="deterministic_forced_tool_call",
            confidence=1.0,
        )

    if surface is ChatSurface.WORKSPACE_CHAT:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.WORKSPACE,
            tool_catalog_mode=TurnToolCatalogMode.FULL,
            max_rounds=CHAT_MAX_ROUNDS,
            history_token_budget=WORKSPACE_HISTORY_TOKEN_BUDGET,
            reason="workspace_surface",
            confidence=1.0,
        )

    if workspace_id or surface in {
        ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
        ChatSurface.WORKFLOW_AGENT_STEP,
    }:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.WORKSPACE,
            tool_catalog_mode=TurnToolCatalogMode.FULL,
            max_rounds=CHAT_MAX_ROUNDS,
            history_token_budget=WORKSPACE_HISTORY_TOKEN_BUDGET,
            reason="workspace_surface",
            confidence=1.0,
        )

    if (
        agent_id
        or agent_subscription_id
        or manual_skill_selected
        or editor_context
        or surface in {
            ChatSurface.AGENT_DM,
            ChatSurface.FILE_EDITOR_CHAT,
            ChatSurface.TASK_COMMENT_THREAD,
            ChatSurface.SCHEDULED_AGENT_RUN,
        }
    ):
        return TurnExecutionPlan(
            mode=TurnExecutionMode.AGENT,
            disable_tools=disable_tools,
            tool_catalog_mode=TurnToolCatalogMode.FULL,
            max_rounds=1 if disable_tools else CHAT_MAX_ROUNDS,
            history_token_budget=WORKSPACE_HISTORY_TOKEN_BUDGET,
            reason="explicit_agent_or_editor_surface",
            confidence=1.0,
        )

    if surface != ChatSurface.GLOBAL_OWNER_CHAT:
        return TurnExecutionPlan(reason="non_owner_chat_surface")

    if disable_tools:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.DIRECT_CHAT,
            disable_tools=True,
            use_fast_model=True,
            prompt_mode=TurnPromptMode.MINIMAL,
            max_rounds=1,
            history_token_budget=DIRECT_CHAT_HISTORY_TOKEN_BUDGET,
            reason="tools_explicitly_disabled",
            confidence=1.0,
        )

    if generation_chat_mode:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            max_rounds=CHAT_MAX_ROUNDS,
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="generation_chat_mode_uses_runtime_default",
            confidence=1.0,
        )

    if presentation_turn:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            # PPTX authoring is a multi-stage workflow (plan, images, native
            # charts, page authoring, render, repair, export, and final gate).
            max_rounds=CHAT_MAX_ROUNDS,
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="presentation_artifact_uses_runtime_default",
            confidence=1.0,
        )

    has_tool_directive = bool(
        metadata.get("chat_mode_prompt")
        or metadata.get("extra_tool_names")
        or metadata.get("approval_resume_guidance")
    )
    if has_tool_directive or _contains_multimodal_content(message):
        return TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            max_rounds=CHAT_MAX_ROUNDS,
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="explicit_tool_or_multimodal_context",
            confidence=1.0,
        )

    return TurnExecutionPlan(
        mode=TurnExecutionMode.TOOL_CHAT,
        tool_catalog_mode=TurnToolCatalogMode.FULL,
        research_strategy=TurnResearchStrategy.NONE,
        max_rounds=CHAT_MAX_ROUNDS,
        history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
        reason="model_selects_chat_tools",
        confidence=1.0,
    )


def runtime_turn_max_rounds(ctx: Any) -> int | None:
    plan = getattr(ctx, "turn_execution_plan", None)
    if isinstance(plan, dict):
        plan = TurnExecutionPlan.from_metadata(plan)
    value = getattr(plan, "max_rounds", None)
    return value if isinstance(value, int) and value > 0 else None


def runtime_turn_visible_tool_names(
    plan: TurnExecutionPlan,
    *,
    manual_skill_selected: bool = False,
    available_tool_names: Iterable[str] | None = None,
) -> tuple[str, ...] | None:
    """Return prompt-visible tools without shrinking the execution allowlist.

    A manually selected skill can inject tools after the turn plan is built.
    Authorization is enforced separately by the execution allowlist.
    """

    if manual_skill_selected:
        return None

    if plan.tool_catalog_mode is TurnToolCatalogMode.WEB_RESEARCH:
        return _WEB_RESEARCH_VISIBLE_TOOL_NAMES
    if plan.tool_catalog_mode is TurnToolCatalogMode.LEDGER_QUERY:
        if available_tool_names is not None:
            available = {
                str(name).strip()
                for name in available_tool_names
                if str(name or "").strip()
            }
            if not available.intersection(
                _WORKSPACE_LEDGER_READ_VISIBLE_TOOL_NAMES
            ):
                return None
        return _WORKSPACE_LEDGER_QUERY_VISIBLE_TOOL_NAMES
    return None
