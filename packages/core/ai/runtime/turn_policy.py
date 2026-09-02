from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable

from packages.core.ai.runtime.auto_route_classifier import (
    AutoRouteDecision,
    AutoRouteMode,
    DIRECT_CHAT_CONFIDENCE_THRESHOLD,
)
from packages.core.ai.runtime.skill_routing import presentation_artifact_intent
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
PRESENTATION_GENERATION_MAX_ROUNDS = 200

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

_GENERATION_ARTIFACT_INTENT = re.compile(
    r"(?:"
    r"\b(?:pptx?|powerpoint|slides?|presentation|pdf|docx?|word\s+document|"
    r"spreadsheet|xlsx?|workbook|website|webpage|image|video|audio)\b|"
    r"演示文稿|幻灯片|文档|表格|工作簿|网站|网页|图片|图像|视频|音频"
    r")",
    re.IGNORECASE,
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


_EXTERNAL_OR_ACTION_INTENT = re.compile(
    r"(?:"
    r"https?://|www\.|"
    r"\b(?:search|browse|look\s*up|find\s+(?:me|us)|research|latest|current|today|"
    r"weather|news|stock|price|quote|verify|fact[- ]?check|citation|sources?|"
    r"open|download|upload|send|email|calendar|schedule|book|purchase|buy|"
    r"run|execute|terminal|shell|database|workspace|knowledge\s*base|"
    r"website|webpage|slides?|presentation|spreadsheet|image|video|audio|"
    r"pdf|document|file|github|slack|notion|gmail)\b|"
    r"搜索|搜一下|查询|查一下|检索|浏览|调研|研究一下|最新|当前|今天|实时|"
    r"天气|新闻|股价|价格|报价|核实|验证|引用|来源|打开|下载|上传|发送|邮件|"
    r"日历|安排|预订|购买|运行|执行|终端|数据库|工作区|知识库|网页|网站|"
    r"幻灯片|演示文稿|表格|图片|视频|音频|文档|文件|公众号"
    r")",
    re.IGNORECASE,
)

_DIRECT_INTENT = re.compile(
    r"(?:"
    r"^(?:hi|hello|hey|thanks?|thank\s+you|good\s+(?:morning|afternoon|evening))\b|"
    r"^(?:你好|您好|嗨|哈喽|谢谢|感谢|早上好|下午好|晚上好)(?:[！!。,.，\s]|$)|"
    r"\b(?:translate|rewrite|rephrase|proofread|polish|summari[sz]e|brainstorm|"
    r"explain|define|compare|write\s+(?:a|an|me)|draft\s+(?:a|an))\b|"
    r"翻译|改写|重写|润色|校对|总结(?:以下|下面|这段|这篇)?|解释|说明|"
    r"是什么|为什么|有何区别|对比|头脑风暴|写一(?:个|封|段|篇|首)"
    r")",
    re.IGNORECASE,
)

_WEB_RESEARCH_INTENT = re.compile(
    r"(?:"
    r"https?://|www\.|"
    r"\b(?:search|browse|look\s*up|research|latest|current|today|online|internet|"
    r"news|weather|price|quote|citation|sources?|prospects?|leads?|contact\s+info)\b|"
    r"搜索|搜一下|搜一搜|查一查|调研|研究一下|网上|互联网|网页|公开资料|"
    r"最新|当前|今天|实时|新闻|天气|价格|报价|引用|来源|潜在客户|联系方式"
    r")",
    re.IGNORECASE,
)

_LOCAL_RETRIEVAL_INTENT = re.compile(
    r"(?:"
    r"\b(?:database|workspace|knowledge\s*base|repository|repo|local\s+files?)\b|"
    r"数据库|工作区|知识库|代码库|本地文件"
    r")",
    re.IGNORECASE,
)

_PROSPECT_RESEARCH_INTENT = re.compile(
    r"(?:"
    r"\b(?:prospects?|leads?|potential\s+(?:customers?|clients?)|contact\s+info|"
    r"outreach\s+(?:list|targets?))\b|"
    r"潜在客户|客户名单|销售线索|获客名单|联系方式|联系信息|外联名单"
    r")",
    re.IGNORECASE,
)

_LEDGER_REFERENCE_INTENT = re.compile(
    r"(?:\bledgers?\b|台账)",
    re.IGNORECASE,
)

_LEDGER_QUERY_INTENT = re.compile(
    r"(?:"
    r"\b(?:query|read|show|display|list|summari[sz]e|summary|overview|count|"
    r"total|aggregate|metrics?|status|stage|trend|current|inflow|outflow|"
    r"revenue|expense|spent)\b|"
    r"查询|查一下|查一查|读取|展示|显示|列出|汇总|统计|总计|多少|收入|支出|"
    r"阶段|状态|趋势|概览|总览"
    r")",
    re.IGNORECASE,
)

_LEDGER_AGGREGATE_FIELD_INTENT = re.compile(
    r"\bsum_(?:amount|tax|fee|inflow|outflow)_minor\b",
    re.IGNORECASE,
)

_LEDGER_IMPLICIT_FINANCE_QUERY_INTENT = re.compile(
    r"(?:"
    r"\bhow\s+much\s+(?:(?:did|have|has)\b.{0,32}\b(?:spend|spent)|"
    r"(?:revenue|income|expenses?|inflow|outflow)\b)|"
    r"\b(?:revenue|income|expenses?|inflow|outflow)\b.{0,32}"
    r"\b(?:total|summary|how\s+much)\b|"
    r"花(?:了|费(?:了)?)?多少钱|花费(?:了)?多少|"
    r"(?:收入|支出|营收|回款|现金流|收支).{0,16}(?:多少|汇总|总计|情况|趋势)|"
    r"(?:多少|汇总|总计).{0,16}(?:收入|支出|营收|回款|现金流|收支)"
    r")",
    re.IGNORECASE,
)

_LEDGER_NON_BUSINESS_COST_INTENT = re.compile(
    r"(?:"
    r"\b(?:ai\s+credits?|tokens?|llm|"
    r"model\s+(?:usage|costs?|spend)|"
    r"api\s+(?:usage|tokens?|credits?))\b|"
    r"(?:AI|模型|接口|API).{0,8}(?:积分|额度|用量|令牌|token)|"
    r"(?:积分|额度|用量|令牌|token).{0,8}(?:AI|模型|接口|API)"
    r")",
    re.IGNORECASE,
)

_LEDGER_WRITE_INTENT = re.compile(
    r"(?:"
    r"\b(?:append|add|create|write|update|delete|remove|correct|mark)\b|"
    r"\b(?:record|log)\s+(?:a|an|the|this|that|new)\b|"
    r"\b(?:record|log)\s+(?!(?:entries?|events?|history|status)\b)|"
    r"录入|新增|添加|创建|写入|更新|删除|移除|更正|标记|登记|记账|记到|记入|"
    r"记(?:一|这|该)?笔|记录(?:一|这|该|新)"
    r")",
    re.IGNORECASE,
)


def _workspace_ledger_query_intent(message_text: str) -> bool:
    """Identify explicit read-only Ledger turns without changing authorization."""

    text = str(message_text or "")
    if _LEDGER_WRITE_INTENT.search(text):
        return False
    if _LEDGER_AGGREGATE_FIELD_INTENT.search(text):
        return True
    if _LEDGER_REFERENCE_INTENT.search(text) and _LEDGER_QUERY_INTENT.search(text):
        return True
    if _LEDGER_NON_BUSINESS_COST_INTENT.search(text):
        return False
    return bool(_LEDGER_IMPLICIT_FINANCE_QUERY_INTENT.search(text))


def _workspace_tool_catalog_mode_for_message(
    message_text: str,
) -> TurnToolCatalogMode:
    if _workspace_ledger_query_intent(message_text):
        return TurnToolCatalogMode.LEDGER_QUERY
    return TurnToolCatalogMode.FULL


def _tool_catalog_mode_for_message(message_text: str) -> TurnToolCatalogMode:
    text = str(message_text or "")
    if _WEB_RESEARCH_INTENT.search(text) and not _LOCAL_RETRIEVAL_INTENT.search(text):
        return TurnToolCatalogMode.WEB_RESEARCH
    return TurnToolCatalogMode.FULL


def _research_strategy_for_message(message_text: str) -> TurnResearchStrategy:
    text = str(message_text or "")
    if not _WEB_RESEARCH_INTENT.search(text) or _LOCAL_RETRIEVAL_INTENT.search(text):
        return TurnResearchStrategy.NONE
    if _PROSPECT_RESEARCH_INTENT.search(text):
        return TurnResearchStrategy.PROSPECT_DISCOVERY
    return TurnResearchStrategy.GENERAL_WEB_RESEARCH


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


def _high_confidence_direct_chat(message_text: str) -> bool:
    text = str(message_text or "").strip()
    if not text or len(text) > 12_000:
        return False
    if _EXTERNAL_OR_ACTION_INTENT.search(text):
        return False
    if _DIRECT_INTENT.search(text):
        return True

    # Short, self-contained questions are safe for a direct answer when they
    # contain no freshness, retrieval, file, or action signal.  Ambiguous
    # requests stay on the legacy tool-capable path.
    question_like = (
        text.endswith(("?", "？"))
        or text.lower().startswith(("what ", "why ", "how ", "who ", "when "))
        or text.startswith(("什么", "为什么", "怎么", "如何", "谁", "请问"))
    )
    return question_like and len(text) <= 1_200


def _worker_classified_direct_chat(
    decision: AutoRouteDecision | None,
) -> bool:
    return bool(
        decision is not None
        and decision.mode is AutoRouteMode.DIRECT_CHAT
        and decision.confidence >= DIRECT_CHAT_CONFIDENCE_THRESHOLD
    )


def build_turn_execution_plan(
    *,
    enabled: bool,
    surface: ChatSurface,
    message: str | list[dict[str, Any]],
    message_text: str,
    disable_tools: bool = False,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    manual_skill_selected: bool = False,
    editor_context: dict[str, Any] | None = None,
    runtime_metadata: dict[str, Any] | None = None,
    auto_route_decision: AutoRouteDecision | None = None,
) -> TurnExecutionPlan:
    """Build a conservative execution plan from surface and Auto routing state."""

    if not enabled:
        return TurnExecutionPlan(reason="chat_turn_routing_v1_disabled")

    metadata = dict(runtime_metadata or {})
    continuation_context_text = str(
        metadata.get("continuation_context_text") or ""
    ).strip()
    presentation_intent = presentation_artifact_intent(
        message_text
    ) or presentation_artifact_intent(continuation_context_text)
    presentation_chat_mode = str(metadata.get("chat_mode") or "").strip().lower() == "slides"
    presentation_turn = presentation_intent or presentation_chat_mode
    continuation_generation_intent = bool(
        continuation_context_text
        and _GENERATION_ARTIFACT_INTENT.search(continuation_context_text)
    )
    generation_chat_mode = (
        str(metadata.get("chat_mode") or "").strip().lower()
        in _GENERATION_CHAT_MODES
    ) or continuation_generation_intent
    forced_tool_calls = metadata.get("forced_tool_calls")
    if isinstance(forced_tool_calls, list) and forced_tool_calls:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.MEDIA,
            # Dedicated generation modes may need to wait, retry, compose,
            # render, and verify. Leave their budget unset so the Runtime
            # Harness uses the underlying agentic-loop default.
            max_rounds=(
                PRESENTATION_GENERATION_MAX_ROUNDS
                if presentation_turn
                else (None if generation_chat_mode else 4)
            ),
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="deterministic_forced_tool_call",
            confidence=1.0,
        )

    if surface is ChatSurface.WORKSPACE_CHAT:
        if _worker_classified_direct_chat(auto_route_decision):
            return TurnExecutionPlan(
                mode=TurnExecutionMode.DIRECT_CHAT,
                disable_tools=True,
                use_fast_model=True,
                prompt_mode=TurnPromptMode.MINIMAL,
                max_rounds=1,
                history_token_budget=DIRECT_CHAT_HISTORY_TOKEN_BUDGET,
                reason="worker_classified_direct_chat",
                confidence=auto_route_decision.confidence,
            )
        return TurnExecutionPlan(
            mode=TurnExecutionMode.WORKSPACE,
            tool_catalog_mode=_workspace_tool_catalog_mode_for_message(
                message_text
            ),
            history_token_budget=WORKSPACE_HISTORY_TOKEN_BUDGET,
            reason=(
                "worker_classified_general_chat"
                if auto_route_decision is not None
                else "workspace_surface"
            ),
            confidence=(
                auto_route_decision.confidence
                if auto_route_decision is not None
                else 1.0
            ),
        )

    if workspace_id or surface in {
        ChatSurface.WORKSPACE_DRAFT_ARCHITECT,
        ChatSurface.WORKFLOW_AGENT_STEP,
    }:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.WORKSPACE,
            tool_catalog_mode=_workspace_tool_catalog_mode_for_message(
                message_text
            ),
            history_token_budget=WORKSPACE_HISTORY_TOKEN_BUDGET,
            reason="workspace_surface",
            confidence=1.0,
        )

    if agent_id or manual_skill_selected or editor_context or surface in {
        ChatSurface.AGENT_DM,
        ChatSurface.FILE_EDITOR_CHAT,
        ChatSurface.TASK_COMMENT_THREAD,
        ChatSurface.SCHEDULED_AGENT_RUN,
    }:
        catalog_mode = TurnToolCatalogMode.FULL
        if manual_skill_selected and not agent_id and not editor_context and not presentation_turn:
            catalog_mode = _tool_catalog_mode_for_message(message_text)
        research_strategy = (
            _research_strategy_for_message(message_text)
            if catalog_mode is TurnToolCatalogMode.WEB_RESEARCH
            else TurnResearchStrategy.NONE
        )
        return TurnExecutionPlan(
            mode=TurnExecutionMode.AGENT,
            disable_tools=disable_tools,
            tool_catalog_mode=catalog_mode,
            research_strategy=research_strategy,
            max_rounds=(
                1
                if disable_tools
                else (
                    PRESENTATION_GENERATION_MAX_ROUNDS
                    if presentation_turn
                    else (8 if catalog_mode is TurnToolCatalogMode.WEB_RESEARCH else None)
                )
            ),
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
            max_rounds=(PRESENTATION_GENERATION_MAX_ROUNDS if presentation_turn else None),
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason=(
                "generation_continuation_uses_runtime_default"
                if continuation_generation_intent
                and str(metadata.get("chat_mode") or "").strip().lower()
                in {"", "auto"}
                else "generation_chat_mode_uses_runtime_default"
            ),
            confidence=1.0,
        )

    if presentation_turn:
        return TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            # PPTX authoring is a multi-stage workflow (plan, images, native
            # charts, page authoring, render, repair, export, and final gate).
            # Use the agentic-loop default instead of the eight-round budget
            # for an ordinary ambiguous tool chat, even when the user did not
            # click the Slides mode first.
            max_rounds=PRESENTATION_GENERATION_MAX_ROUNDS,
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
            max_rounds=8,
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="explicit_tool_or_multimodal_context",
            confidence=1.0,
        )

    if auto_route_decision is not None:
        if _worker_classified_direct_chat(auto_route_decision):
            return TurnExecutionPlan(
                mode=TurnExecutionMode.DIRECT_CHAT,
                disable_tools=True,
                use_fast_model=True,
                prompt_mode=TurnPromptMode.MINIMAL,
                max_rounds=1,
                history_token_budget=DIRECT_CHAT_HISTORY_TOKEN_BUDGET,
                reason="worker_classified_direct_chat",
                confidence=auto_route_decision.confidence,
            )
        return TurnExecutionPlan(
            mode=TurnExecutionMode.TOOL_CHAT,
            tool_catalog_mode=_tool_catalog_mode_for_message(message_text),
            research_strategy=_research_strategy_for_message(message_text),
            max_rounds=8,
            history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
            reason="worker_classified_general_chat",
            confidence=auto_route_decision.confidence,
        )

    if _high_confidence_direct_chat(message_text):
        return TurnExecutionPlan(
            mode=TurnExecutionMode.DIRECT_CHAT,
            disable_tools=True,
            use_fast_model=True,
            prompt_mode=TurnPromptMode.MINIMAL,
            max_rounds=1,
            history_token_budget=DIRECT_CHAT_HISTORY_TOKEN_BUDGET,
            reason="high_confidence_self_contained_chat",
            confidence=0.9,
        )

    return TurnExecutionPlan(
        mode=TurnExecutionMode.TOOL_CHAT,
        tool_catalog_mode=_tool_catalog_mode_for_message(message_text),
        research_strategy=_research_strategy_for_message(message_text),
        max_rounds=8,
        history_token_budget=TOOL_CHAT_HISTORY_TOKEN_BUDGET,
        reason="ambiguous_or_action_capable_chat",
        confidence=0.6,
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

    A manually selected skill can inject tools after the turn plan is built.  Do
    not let a broad research-keyword match hide that explicit skill surface.
    Authorization is still enforced separately by the execution allowlist.
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
