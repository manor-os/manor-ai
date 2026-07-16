"""Dashboard analytics endpoints — stats, trends, goals, activity feed."""
from __future__ import annotations

import json
import hashlib
import re
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import runtime_execute_tool, runtime_tool_schema
from packages.core.ai.runtime.dashboard_module_validation import (
    DASHBOARD_BLOCKED_CSS,
    DASHBOARD_BLOCKED_HTML,
    DASHBOARD_BLOCKED_JAVASCRIPT,
)
from packages.core.cache import cache
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.analytics_service import (
    get_active_goals,
    get_dashboard_stats,
    get_recent_activity,
    get_task_trends,
    get_usage_trends,
)
from packages.core.services.dashboard_market import (
    DashboardMarketDataUnavailable,
    get_dashboard_stock_quotes,
)
from packages.core.services.dashboard_http import (
    DashboardHttpPolicyError,
    DashboardHttpUnavailable,
    get_dashboard_http_json,
    validate_dashboard_http_url,
)
from packages.core.services.dashboard_news import get_dashboard_news
from packages.core.services.dashboard_agent import (
    DASHBOARD_MODULE_BUILDER_SKILL,
    DashboardAgentTurnResult,
    dashboard_tool_is_read_only,
    run_dashboard_agent_turn,
)
from packages.core.services.conversation_lifecycle import (
    delete_conversation,
    get_or_create_conversation,
)
from packages.core.services.conversation_records import list_messages
from packages.core.services.settings_service import (
    get_user_preferences,
    update_user_preferences,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

DASHBOARD_WIDGET_IDS = (
    "daily_brief",
    "time_saved",
    "total_tasks",
    "tasks_running",
    "activity",
    "workspaces",
    "task_trend",
)


def _default_dashboard_layout() -> dict:
    return {
        "version": 2,
        "widgets": [
            {"id": widget_id, "visible": True}
            for widget_id in DASHBOARD_WIDGET_IDS
        ],
        "modules": [],
    }


def _normalize_dashboard_layout(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("widgets"), list):
        return _default_dashboard_layout()

    widgets: list[dict] = []
    seen: set[str] = set()
    for item in value["widgets"]:
        if not isinstance(item, dict):
            continue
        widget_id = str(item.get("id") or "")
        if widget_id not in DASHBOARD_WIDGET_IDS or widget_id in seen:
            continue
        seen.add(widget_id)
        widgets.append({"id": widget_id, "visible": bool(item.get("visible", True))})

    for widget_id in DASHBOARD_WIDGET_IDS:
        if widget_id not in seen:
            widgets.append({"id": widget_id, "visible": True})

    modules: list[dict] = []
    module_ids: set[str] = set()
    raw_modules = value.get("modules")
    if isinstance(raw_modules, list):
        for raw_module in raw_modules[:12]:
            try:
                module = DashboardGeneratedModule.model_validate(raw_module)
            except Exception:
                continue
            if module.id in module_ids:
                continue
            module_ids.add(module.id)
            modules.append(module.model_dump())

    return {"version": 2, "widgets": widgets, "modules": modules}


def _dashboard_module_title_key(value: object) -> str:
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _merge_dashboard_layout_suggestion(
    value: object,
    current: dict,
    *,
    conversation_id: str | None = None,
) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Dashboard suggestion was not an object")

    widgets: list[dict] = []
    seen: set[str] = set()
    raw_widgets = value.get("widgets")
    if isinstance(raw_widgets, list):
        for item in raw_widgets:
            if not isinstance(item, dict):
                continue
            widget_id = str(item.get("id") or "")
            if widget_id not in DASHBOARD_WIDGET_IDS or widget_id in seen:
                continue
            seen.add(widget_id)
            widgets.append({"id": widget_id, "visible": bool(item.get("visible", True))})

    if not widgets:
        widgets = [dict(item) for item in current["widgets"]]
        seen = {item["id"] for item in widgets}

    for item in current["widgets"]:
        if item["id"] not in seen:
            widgets.append(dict(item))

    modules = [dict(item) for item in current.get("modules", [])]
    module_index = {module["id"]: index for index, module in enumerate(modules)}
    raw_changes = value.get("module_changes", [])
    if not isinstance(raw_changes, list):
        raise ValueError("Dashboard module changes were not a list")

    for raw_change in raw_changes[:12]:
        if not isinstance(raw_change, dict):
            continue
        action = str(raw_change.get("action") or "")
        module_id = str(raw_change.get("id") or "")
        if action == "remove":
            if module_id in module_index:
                modules.pop(module_index[module_id])
                module_index = {module["id"]: index for index, module in enumerate(modules)}
            continue

        if action == "create":
            title_key = _dashboard_module_title_key(raw_change.get("title"))
            duplicate_index = next(
                (
                    index
                    for index, module in enumerate(modules)
                    if title_key
                    and _dashboard_module_title_key(module.get("title")) == title_key
                ),
                None,
            )
            if duplicate_index is not None:
                existing_module = modules[duplicate_index]
                candidate = {
                    **existing_module,
                    **{
                        key: raw_change[key]
                        for key in ("title", "description", "visible", "size", "code")
                        if key in raw_change
                    },
                    "id": existing_module["id"],
                    "conversation_id": conversation_id,
                }
                modules[duplicate_index] = (
                    DashboardGeneratedModule.model_validate(candidate).model_dump()
                )
                continue
            candidate = {
                "id": f"module_{secrets.token_hex(6)}",
                "title": raw_change.get("title"),
                "description": raw_change.get("description"),
                "visible": raw_change.get("visible", True),
                "size": raw_change.get("size", "compact"),
                "conversation_id": conversation_id,
                "code": raw_change.get("code"),
            }
            module = DashboardGeneratedModule.model_validate(candidate).model_dump()
            modules.append(module)
            module_index[module["id"]] = len(modules) - 1
            continue

        if action == "update" and module_id in module_index:
            current_module = modules[module_index[module_id]]
            candidate = {
                **current_module,
                **{
                    key: raw_change[key]
                    for key in ("title", "description", "visible", "size", "code")
                    if key in raw_change
                },
                **({"conversation_id": conversation_id} if conversation_id else {}),
                "id": module_id,
            }
            modules[module_index[module_id]] = (
                DashboardGeneratedModule.model_validate(candidate).model_dump()
            )

    if len(modules) > 12:
        raise ValueError("Dashboard cannot contain more than 12 generated modules")
    if not widgets and not raw_changes:
        raise ValueError("Dashboard suggestion did not include any changes")

    return {"version": 2, "widgets": widgets, "modules": modules}


# ── Schemas ──

class TaskStats(BaseModel):
    total: int = 0
    by_status: dict[str, int] = {}
    overdue: int = 0


class DocumentStats(BaseModel):
    total: int = 0
    indexed: int = 0


class AgentStats(BaseModel):
    total: int = 0
    subscribed: int = 0


class ConversationStats(BaseModel):
    total: int = 0
    today: int = 0


class ClientStats(BaseModel):
    total: int = 0
    active: int = 0


class StaffStats(BaseModel):
    total: int = 0


class UsageStats(BaseModel):
    total_tokens: int = 0
    total_cost: float = 0.0
    today_tokens: int = 0


class DashboardStatsResponse(BaseModel):
    tasks: TaskStats = TaskStats()
    documents: DocumentStats = DocumentStats()
    agents: AgentStats = AgentStats()
    conversations: ConversationStats = ConversationStats()
    clients: ClientStats = ClientStats()
    staff: StaffStats = StaffStats()
    usage: UsageStats = UsageStats()


class TaskTrendItem(BaseModel):
    date: str
    created: int = 0
    completed: int = 0


class UsageTrendItem(BaseModel):
    date: str
    tokens: int = 0
    cost: float = 0.0


class ActiveGoalItem(BaseModel):
    id: str
    task_id: str | None = None
    status: str
    execution_mode: str | None = None
    progress_pct: int = 0
    step_count: int = 0
    step_done: int = 0
    updated_at: str | None = None


class ActivityItem(BaseModel):
    type: str
    id: str
    name: str
    action: str
    description: str | None = None
    created_by: str | None = None
    timestamp: str | None = None
    task_id: str | None = None


class DashboardWidgetPreference(BaseModel):
    id: str
    visible: bool = True


class DashboardModuleDataRequest(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    source: Literal[
        "tasks",
        "workspaces",
        "activity",
        "task_trends",
        "stats",
        "news",
        "stocks",
        "http_json",
        "tool",
    ]
    params: dict[str, object] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=2_000)
    tool_name: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]{2,180}$",
    )
    tool_arguments: dict[str, object] | None = None
    refresh_seconds: int | None = Field(default=None, ge=30, le=3600)

    @field_validator("params")
    @classmethod
    def validate_params(cls, value: dict[str, object]) -> dict[str, object]:
        if len(value) > 16:
            raise ValueError("Dashboard data requests support at most 16 parameters")
        for key, item in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
                raise ValueError("Dashboard data request parameter names must be safe identifiers")
            values = item if isinstance(item, list) else [item]
            if len(values) > 30 or any(
                not isinstance(entry, (str, int, float, bool, type(None)))
                for entry in values
            ):
                raise ValueError("Dashboard data request parameters must contain simple JSON values")
            if any(isinstance(entry, str) and len(entry) > 240 for entry in values):
                raise ValueError("Dashboard data request strings are too long")
        return value

    @field_validator("tool_arguments")
    @classmethod
    def validate_tool_arguments(
        cls,
        value: dict[str, object] | None,
    ) -> dict[str, object] | None:
        if value is None:
            return None

        def validate_json(item: object, depth: int = 0) -> None:
            if depth > 5:
                raise ValueError("Dashboard tool arguments are too deeply nested")
            if isinstance(item, str):
                if len(item) > 2000:
                    raise ValueError("Dashboard tool argument strings are too long")
                return
            if isinstance(item, (int, float, bool, type(None))):
                return
            if isinstance(item, list):
                if len(item) > 50:
                    raise ValueError("Dashboard tool argument lists are too long")
                for child in item:
                    validate_json(child, depth + 1)
                return
            if isinstance(item, dict):
                if len(item) > 40:
                    raise ValueError("Dashboard tool arguments contain too many fields")
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > 100:
                        raise ValueError("Dashboard tool argument keys are invalid")
                    validate_json(child, depth + 1)
                return
            raise ValueError("Dashboard tool arguments must contain JSON values")

        validate_json(value)
        return value

    @model_validator(mode="after")
    def validate_tool_source(self):
        if self.source == "http_json":
            if not self.url:
                raise ValueError("Public JSON data requests require url")
            try:
                self.url = validate_dashboard_http_url(self.url)
            except DashboardHttpPolicyError as exc:
                raise ValueError(str(exc)) from exc
            if self.tool_name is not None or self.tool_arguments is not None:
                raise ValueError("Tool fields require source=tool")
        elif self.source == "tool":
            if not self.tool_name:
                raise ValueError("Dashboard tool data requests require tool_name")
            if not dashboard_tool_is_read_only(
                self.tool_name,
                self.tool_arguments or {},
            ):
                raise ValueError("Dashboard modules may only execute read-only tools")
            if self.url:
                raise ValueError("Public JSON url requires source=http_json")
        elif any(
            value is not None
            for value in (
                self.tool_name,
                self.tool_arguments,
                self.refresh_seconds,
                self.url,
            )
        ):
            raise ValueError("Network and tool fields require source=http_json or source=tool")
        return self


class DashboardModuleCode(BaseModel):
    version: Literal[1] = 1
    runtime: Literal["sandboxed_html"] = "sandboxed_html"
    html: str = Field(default="", max_length=20_000)
    css: str = Field(default="", max_length=30_000)
    javascript: str = Field(min_length=20, max_length=50_000)
    data_requests: list[DashboardModuleDataRequest] = Field(
        default_factory=list,
        max_length=8,
    )

    @field_validator("html")
    @classmethod
    def validate_html(cls, value: str) -> str:
        if DASHBOARD_BLOCKED_HTML.search(value):
            raise ValueError("Generated dashboard HTML contains unsafe elements")
        return value

    @field_validator("css")
    @classmethod
    def validate_css(cls, value: str) -> str:
        if DASHBOARD_BLOCKED_CSS.search(value):
            raise ValueError("Generated dashboard CSS cannot load external resources")
        return value

    @field_validator("javascript")
    @classmethod
    def validate_javascript(cls, value: str) -> str:
        unsafe = DASHBOARD_BLOCKED_JAVASCRIPT.search(value)
        if unsafe:
            raise ValueError("Generated dashboard JavaScript uses a blocked capability")
        return value

    @model_validator(mode="after")
    def validate_runtime_entrypoint(self):
        if "renderDashboardModule" not in self.javascript:
            raise ValueError(
                "Generated dashboard JavaScript must define window.renderDashboardModule"
            )
        request_keys = [request.key for request in self.data_requests]
        if len(request_keys) != len(set(request_keys)):
            raise ValueError("Dashboard data request keys must be unique")
        return self


class DashboardGeneratedModule(BaseModel):
    id: str = Field(pattern=r"^module_[a-z0-9_-]{4,64}$")
    title: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=180)
    visible: bool = True
    size: Literal["compact", "wide"] = "compact"
    conversation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{10,64}$",
    )
    code: DashboardModuleCode


class DashboardNewsItem(BaseModel):
    id: str
    title: str
    url: str
    source: str | None = None
    published_at: str | None = None
    language: str | None = None


class DashboardStockQuote(BaseModel):
    symbol: str
    price: float | None = None
    change: float | None = None
    change_percent: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    previous_close: float | None = None
    currency: str | None = None
    updated_at: str | None = None
    status: Literal["ok", "unavailable"] = "unavailable"
    provider: str | None = None


class DashboardLayoutResponse(BaseModel):
    version: int = 2
    widgets: list[DashboardWidgetPreference] = Field(default_factory=list)
    modules: list[DashboardGeneratedModule] = Field(default_factory=list)


class DashboardLayoutSuggestionResponse(DashboardLayoutResponse):
    assistant_message: str | None = None
    changed_module_id: str | None = None
    conversation_id: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    hitl_requests: list[dict] = Field(default_factory=list)
    preview_created: bool = False


class DashboardLayoutUpdate(BaseModel):
    widgets: list[DashboardWidgetPreference] = Field(default_factory=list)
    modules: list[DashboardGeneratedModule] | None = None


class DashboardConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1000)


class DashboardLayoutSuggestionRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)
    widgets: list[DashboardWidgetPreference] = Field(default_factory=list)
    modules: list[DashboardGeneratedModule] = Field(default_factory=list, max_length=12)
    target_module_id: str | None = Field(
        default=None,
        pattern=r"^module_[a-z0-9_-]{4,64}$",
    )
    conversation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{10,64}$",
    )
    conversation: list[DashboardConversationMessage] = Field(
        default_factory=list,
        max_length=20,
    )


class DashboardModuleChange(BaseModel):
    action: Literal["create", "update", "remove"]
    id: str | None = None
    title: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=180)
    visible: bool | None = None
    size: Literal["compact", "wide"] | None = None
    code: DashboardModuleCode | None = None

    @model_validator(mode="after")
    def validate_change_contract(self):
        if self.action == "create" and (not self.title or self.code is None):
            raise ValueError("Created dashboard modules require a title and code")
        if self.action in {"update", "remove"} and not self.id:
            raise ValueError("Updated or removed dashboard modules require an id")
        return self


class DashboardLayoutAISuggestion(BaseModel):
    widgets: list[DashboardWidgetPreference]
    module_changes: list[DashboardModuleChange] = Field(default_factory=list, max_length=12)
    assistant_message: str | None = Field(default=None, max_length=400)


class DashboardModuleConversationItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    tool_calls: list[str] = Field(default_factory=list)


class DashboardModuleConversationResponse(BaseModel):
    conversation_id: str | None = None
    messages: list[DashboardModuleConversationItem] = Field(default_factory=list)


class DashboardToolDataRequest(BaseModel):
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_.:-]{2,180}$")
    arguments: dict[str, object] = Field(default_factory=dict)
    conversation_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{10,64}$",
    )
    refresh_seconds: int = Field(default=300, ge=30, le=3600)

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, object]) -> dict[str, object]:
        DashboardModuleDataRequest.validate_tool_arguments(value)
        return value


class DashboardToolDataResponse(BaseModel):
    tool_name: str
    result: object
    cached: bool = False


class DashboardHttpDataRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2_000)
    refresh_seconds: int = Field(default=300, ge=30, le=3600)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            return validate_dashboard_http_url(value)
        except DashboardHttpPolicyError as exc:
            raise ValueError(str(exc)) from exc


class DashboardHttpDataResponse(BaseModel):
    url: str
    result: object
    cached: bool = False


# ── Endpoints ──

@router.get(
    "/layout",
    response_model=DashboardLayoutResponse,
    response_model_exclude_none=True,
)
async def dashboard_layout(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    preferences = await get_user_preferences(db, user.id)
    return DashboardLayoutResponse(
        **_normalize_dashboard_layout(preferences.get("dashboard_layout"))
    )


@router.put(
    "/layout",
    response_model=DashboardLayoutResponse,
    response_model_exclude_none=True,
)
async def update_dashboard_layout(
    req: DashboardLayoutUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    widget_ids = [widget.id for widget in req.widgets]
    invalid = sorted(set(widget_ids) - set(DASHBOARD_WIDGET_IDS))
    if invalid:
        raise HTTPException(422, f"Unknown dashboard widgets: {', '.join(invalid)}")
    if len(widget_ids) != len(set(widget_ids)):
        raise HTTPException(422, "Dashboard widgets must not contain duplicates")

    preferences = await get_user_preferences(db, user.id)
    current = _normalize_dashboard_layout(preferences.get("dashboard_layout"))
    modules = (
        current["modules"]
        if req.modules is None
        else [module.model_dump() for module in req.modules]
    )
    module_ids = [module["id"] for module in modules]
    if len(module_ids) != len(set(module_ids)):
        raise HTTPException(422, "Dashboard modules must not contain duplicates")
    for module in modules:
        conversation_id = str(module.get("conversation_id") or "") or None
        if not conversation_id:
            continue
        try:
            await get_or_create_conversation(
                db,
                user.entity_id,
                user.id,
                conversation_id=conversation_id,
            )
        except (LookupError, PermissionError) as exc:
            raise HTTPException(
                422,
                "Dashboard module conversation is not available to this user",
            ) from exc

    layout = _normalize_dashboard_layout(
        {
            "widgets": [widget.model_dump() for widget in req.widgets],
            "modules": modules,
        }
    )
    await update_user_preferences(db, user.id, {"dashboard_layout": layout})
    retained_conversations = {
        str(module.get("conversation_id"))
        for module in modules
        if module.get("conversation_id")
    }
    removed_conversations = {
        str(module.get("conversation_id"))
        for module in current["modules"]
        if module.get("conversation_id")
        and str(module.get("conversation_id")) not in retained_conversations
    }
    for conversation_id in removed_conversations:
        try:
            await get_or_create_conversation(
                db,
                user.entity_id,
                user.id,
                conversation_id=conversation_id,
            )
        except (LookupError, PermissionError):
            continue
        await delete_conversation(db, conversation_id, user.entity_id)
    return DashboardLayoutResponse(**layout)


@router.post(
    "/layout/suggest",
    response_model=DashboardLayoutSuggestionResponse,
    response_model_exclude_none=True,
)
async def suggest_dashboard_layout(
    req: DashboardLayoutSuggestionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not req.prompt.strip():
        raise HTTPException(422, "Dashboard request must not be empty")

    current = _normalize_dashboard_layout(
        {
            "widgets": [widget.model_dump() for widget in req.widgets],
            "modules": [module.model_dump() for module in req.modules],
        }
    )
    target_module = next(
        (
            module
            for module in current["modules"]
            if module["id"] == req.target_module_id
        ),
        None,
    )
    if req.target_module_id and target_module is None:
        raise HTTPException(422, "Dashboard module is not available to this user")
    target_conversation_id = (
        str(target_module.get("conversation_id") or "") or None
        if target_module
        else None
    )
    if (
        req.conversation_id
        and target_conversation_id
        and req.conversation_id != target_conversation_id
    ):
        raise HTTPException(422, "Dashboard conversation does not belong to this module")
    conversation_id = req.conversation_id or target_conversation_id

    widget_catalog = [
        {
            "id": "daily_brief",
            "description": "AI summary, key counts, and actions needing attention",
        },
        {"id": "time_saved", "description": "estimated time saved"},
        {"id": "total_tasks", "description": "total task volume"},
        {"id": "tasks_running", "description": "active and pending tasks"},
        {"id": "activity", "description": "recent work and activity"},
        {"id": "workspaces", "description": "workspace status"},
        {"id": "task_trend", "description": "14-day task trend"},
    ]
    skill_context = json.dumps(
        {
            "mode": "edit_module" if target_module is not None else "customize_dashboard",
            "available_widgets": widget_catalog,
            "current_widgets": current["widgets"],
            "current_modules": current["modules"],
            "target_module": target_module,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    try:
        turn: DashboardAgentTurnResult = await run_dashboard_agent_turn(
            db,
            user=user,
            message=req.prompt.strip(),
            system_prompt=skill_context,
            conversation_id=conversation_id,
            module_id=req.target_module_id,
            module_title=(target_module or {}).get("title"),
        )
        if turn.submission is None:
            return DashboardLayoutSuggestionResponse(
                **current,
                assistant_message=turn.assistant_message or None,
                changed_module_id=req.target_module_id,
                conversation_id=turn.conversation_id,
                tool_calls=turn.tool_calls,
                hitl_requests=turn.hitl_requests,
                preview_created=False,
            )
        validated_suggestion = DashboardLayoutAISuggestion.model_validate(
            turn.submission
        )
        suggestion = validated_suggestion.model_dump(exclude_none=True)
        if target_module is not None:
            target_changes = [
                change
                for change in suggestion["module_changes"]
                if change.get("id") == req.target_module_id
                and change.get("action") in {"update", "remove"}
            ]
            if len(target_changes) != 1:
                raise ValueError("Dashboard module edit did not target exactly one module")
            suggestion["widgets"] = current["widgets"]
            suggestion["module_changes"] = target_changes
        layout = _merge_dashboard_layout_suggestion(
            suggestion,
            current,
            conversation_id=turn.conversation_id,
        )
    except HTTPException:
        raise
    except (LookupError, PermissionError) as exc:
        raise HTTPException(404, "Dashboard conversation not found") from exc
    except Exception as exc:
        raise HTTPException(502, "Could not interpret dashboard request") from exc

    return DashboardLayoutSuggestionResponse(
        **layout,
        assistant_message=(
            turn.assistant_message
            or validated_suggestion.assistant_message
            or None
        ),
        changed_module_id=req.target_module_id,
        conversation_id=turn.conversation_id,
        tool_calls=turn.tool_calls,
        hitl_requests=turn.hitl_requests,
        preview_created=layout != current,
    )


@router.get(
    "/modules/{module_id}/conversation",
    response_model=DashboardModuleConversationResponse,
)
async def dashboard_module_conversation(
    module_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    preferences = await get_user_preferences(db, user.id)
    layout = _normalize_dashboard_layout(preferences.get("dashboard_layout"))
    module = next(
        (item for item in layout["modules"] if item["id"] == module_id),
        None,
    )
    if module is None:
        raise HTTPException(404, "Dashboard module not found")
    conversation_id = str(module.get("conversation_id") or "") or None
    if not conversation_id:
        return DashboardModuleConversationResponse()
    try:
        await get_or_create_conversation(
            db,
            user.entity_id,
            user.id,
            conversation_id=conversation_id,
        )
    except (LookupError, PermissionError) as exc:
        raise HTTPException(404, "Dashboard conversation not found") from exc

    messages = await list_messages(db, conversation_id, limit=100)
    return DashboardModuleConversationResponse(
        conversation_id=conversation_id,
        messages=[
            DashboardModuleConversationItem(
                role=message.role,
                content=message.content or "",
                tool_calls=[
                    (
                        DASHBOARD_MODULE_BUILDER_SKILL
                        if str(call.get("name")) == "invoke_skill"
                        else str(call.get("name"))
                    )
                    for call in (message.tool_calls or [])
                    if isinstance(call, dict)
                    and call.get("name")
                    and call.get("name") != "dashboard_submit_module"
                ],
            )
            for message in messages
            if message.role in {"user", "assistant"} and (message.content or "").strip()
        ],
    )


@router.post("/tool-data", response_model=DashboardToolDataResponse)
async def dashboard_tool_data(
    req: DashboardToolDataRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if runtime_tool_schema(req.tool_name) is None:
        raise HTTPException(422, "Dashboard tool is not available")
    if not dashboard_tool_is_read_only(req.tool_name, req.arguments):
        raise HTTPException(403, "Dashboard modules may only execute read-only tools")
    if req.conversation_id:
        try:
            await get_or_create_conversation(
                db,
                user.entity_id,
                user.id,
                conversation_id=req.conversation_id,
            )
        except (LookupError, PermissionError) as exc:
            raise HTTPException(404, "Dashboard conversation not found") from exc

    digest = hashlib.sha256(
        json.dumps(
            {
                "tool": req.tool_name,
                "arguments": req.arguments,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cache_key = f"dashboard-tool:{user.id}:{digest}"
    cached = await cache.get(cache_key)
    if isinstance(cached, dict) and "result" in cached:
        return DashboardToolDataResponse(
            tool_name=req.tool_name,
            result=cached["result"],
            cached=True,
        )

    raw_result = await runtime_execute_tool(
        req.tool_name,
        req.arguments,
        entity_id=user.entity_id,
        user_id=user.id,
        conversation_id=req.conversation_id,
        active_user_message="Refresh a private Dashboard module using read-only data",
        allowed_tool_names={req.tool_name},
    )
    if raw_result.startswith("Error:"):
        raise HTTPException(502, raw_result[:500])
    try:
        decoded: object = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError):
        decoded = raw_result
    if isinstance(decoded, dict) and "__hitl__" in decoded:
        raise HTTPException(403, "Dashboard tool requires an interactive approval")
    serialized = json.dumps(decoded, default=str)
    if len(serialized) > 250_000:
        decoded = {
            "truncated": True,
            "content": serialized[:250_000],
        }
    await cache.set(
        cache_key,
        {"result": decoded},
        ttl=req.refresh_seconds,
    )
    return DashboardToolDataResponse(
        tool_name=req.tool_name,
        result=decoded,
    )


@router.post("/http-data", response_model=DashboardHttpDataResponse)
async def dashboard_http_data(
    req: DashboardHttpDataRequest,
    user: User = Depends(get_current_user),
):
    digest = hashlib.sha256(req.url.encode("utf-8")).hexdigest()
    cache_key = f"dashboard-http:{user.id}:{digest}"
    cached = await cache.get(cache_key)
    if isinstance(cached, dict) and "result" in cached:
        return DashboardHttpDataResponse(
            url=req.url,
            result=cached["result"],
            cached=True,
        )
    try:
        result = await get_dashboard_http_json(req.url)
    except DashboardHttpPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DashboardHttpUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    serialized = json.dumps(result, default=str)
    if len(serialized.encode("utf-8")) > 250_000:
        raise HTTPException(502, "Public JSON response is too large")
    await cache.set(
        cache_key,
        {"result": result},
        ttl=req.refresh_seconds,
    )
    return DashboardHttpDataResponse(url=req.url, result=result)


@router.get("/news", response_model=list[DashboardNewsItem])
async def dashboard_news(
    query: str | None = Query(default=None, max_length=120),
    days: int = Query(default=1, ge=1, le=365),
    limit: int = Query(default=8, ge=1, le=20),
    user: User = Depends(get_current_user),
):
    return await get_dashboard_news(
        query=query,
        days=days,
        limit=limit,
        locale=user.locale,
    )


@router.get("/stocks", response_model=list[DashboardStockQuote])
async def dashboard_stocks(
    symbols: str = Query(min_length=1, max_length=120),
    _user: User = Depends(get_current_user),
):
    try:
        return await get_dashboard_stock_quotes(symbols=symbols.split(","))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DashboardMarketDataUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/stats", response_model=DashboardStatsResponse)
async def dashboard_stats(
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await get_dashboard_stats(
        db,
        user.entity_id,
        workspace_id=workspace_id,
        timezone_name=user.timezone,
    )
    return DashboardStatsResponse(**data)


@router.get("/task-trends", response_model=list[TaskTrendItem])
async def task_trends(
    days: int = Query(30, ge=1, le=365),
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_task_trends(
        db,
        user.entity_id,
        days=days,
        workspace_id=workspace_id,
        timezone_name=user.timezone,
    )


@router.get("/usage-trends", response_model=list[UsageTrendItem])
async def usage_trends(
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_usage_trends(db, user.entity_id, days=days, timezone_name=user.timezone)


@router.get("/active-goals", response_model=list[ActiveGoalItem])
async def active_goals(
    limit: int = Query(5, ge=1, le=50),
    workspace_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_active_goals(db, user.entity_id, limit=limit, workspace_id=workspace_id)


@router.get("/recent-activity")
async def recent_activity(
    workspace_id: str | None = Query(None),
    since: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await get_recent_activity(
            db,
            user.entity_id,
            limit=limit,
            workspace_id=workspace_id,
            since=since,
        )
    except Exception:
        return []
