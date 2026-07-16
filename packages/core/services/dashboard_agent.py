from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import (
    ChatSurface,
    runtime_registered_tool_names,
    runtime_run_chat_turn,
)
from packages.core.ai.runtime.approval_classifier import (
    classify_runtime_tool_action,
)
from packages.core.ai.runtime.dashboard_submission import (
    DASHBOARD_SUBMIT_TOOL_NAME,
    runtime_capture_dashboard_submission,
)
from packages.core.models.user import User
from packages.core.models.skill import Skill
from packages.core.services.conversation_lifecycle import (
    get_or_create_conversation,
)
from packages.core.services.conversation_messages import add_message


DASHBOARD_MODULE_BUILDER_SKILL = "dashboard-module-builder"
DASHBOARD_CODE_ACTIONS = frozenset({"dashboard_module_validate"})


_DASHBOARD_BLOCKED_EXACT = {
    "bash",
    "code",
    "generate_file",
    "manor",
    "notify_user",
    "workspace_agent",
    "workspace_operation",
}
_DASHBOARD_BLOCKED_PREFIXES = (
    "browser_",
    "mcp__browser",
    "mcp__chrome__",
    "sandbox_",
)
_DASHBOARD_MUTATION_WORDS = {
    "approve",
    "archive",
    "cancel",
    "copy",
    "create",
    "delete",
    "draft",
    "edit",
    "execute",
    "follow",
    "generate",
    "like",
    "mark",
    "move",
    "notify",
    "post",
    "publish",
    "reject",
    "remove",
    "reply",
    "run",
    "save",
    "schedule",
    "send",
    "toggle",
    "unfollow",
    "unlike",
    "update",
    "upload",
    "write",
}


@dataclass(frozen=True)
class DashboardAgentTurnResult:
    conversation_id: str
    assistant_message: str
    submission: dict[str, Any] | None
    tool_calls: list[str]
    hitl_requests: list[dict[str, Any]]


def dashboard_tool_is_read_only(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    name = str(tool_name or "").strip()
    if not name or name == DASHBOARD_SUBMIT_TOOL_NAME:
        return False
    if name in _DASHBOARD_BLOCKED_EXACT or name.startswith(_DASHBOARD_BLOCKED_PREFIXES):
        return False
    if classify_runtime_tool_action(name, arguments or {}) is not None:
        return False
    words = {
        word
        for word in re.split(r"[^a-z0-9]+", name.lower())
        if word
    }
    return not bool(words & _DASHBOARD_MUTATION_WORDS)


def dashboard_agent_tool_is_allowed(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> bool:
    name = str(tool_name or "").strip()
    if name == "code":
        if arguments is None:
            return True
        return str(arguments.get("action") or "").strip() in DASHBOARD_CODE_ACTIONS
    return dashboard_tool_is_read_only(name, arguments)


def dashboard_blocked_tool_names() -> tuple[str, ...]:
    return tuple(
        name
        for name in runtime_registered_tool_names()
        if name not in {DASHBOARD_SUBMIT_TOOL_NAME, "invoke_skill"}
        and not dashboard_agent_tool_is_allowed(name)
    )


async def _dashboard_module_builder_skill_id(db: AsyncSession) -> str:
    skill_id = (
        await db.execute(
            select(Skill.id).where(
                Skill.entity_id.is_(None),
                Skill.slug == DASHBOARD_MODULE_BUILDER_SKILL,
                Skill.status == "active",
            )
        )
    ).scalar_one_or_none()
    if skill_id:
        return skill_id
    from packages.core.services.builtin_skill_loader import seed_builtin_skills

    await seed_builtin_skills(db)
    skill_id = (
        await db.execute(
            select(Skill.id).where(
                Skill.entity_id.is_(None),
                Skill.slug == DASHBOARD_MODULE_BUILDER_SKILL,
                Skill.status == "active",
            )
        )
    ).scalar_one_or_none()
    if not skill_id:
        raise LookupError("Dashboard module builder skill is not installed")
    return skill_id


async def run_dashboard_agent_turn(
    db: AsyncSession,
    *,
    user: User,
    message: str,
    system_prompt: str,
    conversation_id: str | None = None,
    module_id: str | None = None,
    module_title: str | None = None,
) -> DashboardAgentTurnResult:
    dashboard_skill_id = await _dashboard_module_builder_skill_id(db)
    conv = await get_or_create_conversation(
        db,
        user.entity_id,
        user.id,
        conversation_id=conversation_id,
        title=f"Dashboard module: {module_title or 'New module'}",
    )
    meta = dict(conv.meta or {})
    existing_module_id = str(meta.get("dashboard_module_id") or "")
    if module_id and existing_module_id and existing_module_id != module_id:
        raise PermissionError("Dashboard conversation belongs to another module")
    conv.meta = {
        **meta,
        "surface": "dashboard_module",
        **({"dashboard_module_id": module_id} if module_id else {}),
    }
    await add_message(
        db,
        conv.id,
        role="user",
        content=message,
        meta={"author_user_id": user.id, "surface": "dashboard_module"},
    )
    await db.commit()

    skill_input = (
        f"User request:\n{message.strip()}\n\n"
        "Dashboard context supplied by the host:\n"
        f"{system_prompt.strip()}"
    )

    with runtime_capture_dashboard_submission() as capture:
        result = await runtime_run_chat_turn(
            message,
            conv.id,
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            entity_id=user.entity_id,
            user_id=user.id,
            db=db,
            manual_skill_refs=[
                {
                    "id": dashboard_skill_id,
                    "display_name": "Dashboard Module Builder",
                    "category": "dashboard-builder",
                    "output_format": "json",
                    "input": skill_input,
                }
            ],
            blocked_tools=dashboard_blocked_tool_names(),
            runtime_metadata={
                "chat_mode_prompt": (
                    "You are Manor AI in a private Dashboard module conversation. "
                    "The dashboard-module-builder skill is mandatory for this turn and has "
                    "already been selected. Let the skill perform all module generation, then "
                    "briefly relay its result. Do not invent or paste module code yourself."
                ),
                "extra_tool_names": [DASHBOARD_SUBMIT_TOOL_NAME],
                "dashboard_module_id": module_id,
            },
        )

    tool_calls = [
        (
            DASHBOARD_MODULE_BUILDER_SKILL
            if str(name) == "invoke_skill"
            else str(name)
        )
        for name in result.get("tool_calls_made") or []
        if str(name) != DASHBOARD_SUBMIT_TOOL_NAME
    ]
    if capture.validated_code_hashes and "code" not in tool_calls:
        tool_calls.append("code")

    return DashboardAgentTurnResult(
        conversation_id=conv.id,
        assistant_message=str(result.get("content") or "").strip(),
        submission=capture.submission,
        tool_calls=tool_calls,
        hitl_requests=[
            item
            for item in result.get("hitl_requests") or []
            if isinstance(item, dict)
        ],
    )
