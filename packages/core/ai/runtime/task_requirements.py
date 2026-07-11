from __future__ import annotations

from typing import Any

from packages.core.ai.runtime.capabilities import (
    capability_for_id,
    tool_names_for_capability_ids,
)
from packages.core.ai.runtime.profiles import RuntimeProfile


TASK_RUNTIME_CAPABILITY_KEYS = (
    "required_capabilities",
    "business_capabilities",
    "runtime_capabilities",
    "capability_ids",
)

STRATEGIST_TASK_CAPABILITY_IDS = (
    "workspace.search",
    "workspace.task",
    "workspace.knowledge",
    "workspace.governance",
    "web.safe_search",
    "skill.invoke",
    "file.read",
    "file.write",
    "sandbox.execute",
    "automation.manage",
    "manor.composite",
)


def _as_clean_capability_ids(values: Any) -> tuple[str, ...]:
    if values is None:
        raw_values: list[Any] = []
    elif isinstance(values, str):
        raw_values = [part.strip() for part in values.split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    else:
        raw_values = [values]

    out: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if isinstance(value, dict):
            value = (
                value.get("capability_id")
                or value.get("business_capability_id")
                or value.get("runtime_capability_id")
                or value.get("id")
                or value.get("name")
            )
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def task_runtime_capability_validation_errors(
    values: Any,
    *,
    profile: RuntimeProfile | None = RuntimeProfile.BACKGROUND_WORKER,
    allowed_ids: tuple[str, ...] | set[str] | None = None,
    path: str = "required_capabilities",
) -> list[dict[str, str]]:
    """Validate task-local BusinessCapability references.

    Task runtime requirements are intentionally capability ids, not tool names.
    The actual tool expansion still happens through the runtime catalog and is
    filtered by profile so a task cannot invent a new execution surface.
    """

    allowed = set(allowed_ids or ())
    errors: list[dict[str, str]] = []
    for capability_id in _as_clean_capability_ids(values):
        capability = capability_for_id(capability_id)
        if capability is None:
            errors.append({
                "path": path,
                "message": f"unknown runtime capability {capability_id!r}",
            })
            continue
        if allowed and capability_id not in allowed:
            errors.append({
                "path": path,
                "message": f"runtime capability {capability_id!r} is not allowed for strategist tasks",
            })
            continue
        if profile is not None and not tool_names_for_capability_ids({capability_id}, profile=profile):
            errors.append({
                "path": path,
                "message": f"runtime capability {capability_id!r} is not available for profile {profile.value}",
            })
    return errors


def normalize_task_runtime_capability_ids(
    values: Any,
    *,
    profile: RuntimeProfile | None = RuntimeProfile.BACKGROUND_WORKER,
    allowed_ids: tuple[str, ...] | set[str] | None = None,
    strict: bool = False,
) -> tuple[str, ...]:
    errors = task_runtime_capability_validation_errors(
        values,
        profile=profile,
        allowed_ids=allowed_ids,
    )
    if errors and strict:
        raise ValueError("; ".join(error["message"] for error in errors))
    allowed = set(allowed_ids or ())
    return tuple(
        capability_id
        for capability_id in _as_clean_capability_ids(values)
        if capability_for_id(capability_id) is not None
        and (not allowed or capability_id in allowed)
        and (
            profile is None
            or bool(tool_names_for_capability_ids({capability_id}, profile=profile))
        )
    )


def task_runtime_capabilities_from_context(
    runtime_context: Any,
    *,
    profile: RuntimeProfile | None = RuntimeProfile.BACKGROUND_WORKER,
    allowed_ids: tuple[str, ...] | set[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(runtime_context, dict):
        return ()
    for key in TASK_RUNTIME_CAPABILITY_KEYS:
        if key in runtime_context:
            normalized = normalize_task_runtime_capability_ids(
                runtime_context.get(key),
                profile=profile,
                allowed_ids=allowed_ids,
            )
            if normalized:
                return normalized
    return ()


def task_runtime_capability_tools(
    capability_ids: Any,
    *,
    profile: RuntimeProfile | None = RuntimeProfile.BACKGROUND_WORKER,
) -> set[str]:
    return tool_names_for_capability_ids(
        set(normalize_task_runtime_capability_ids(capability_ids, profile=profile)),
        profile=profile,
    )


def task_runtime_requirements_lines(runtime_context: Any) -> tuple[str, ...]:
    """Render task-local runtime context as stable prompt bullet lines."""

    if not isinstance(runtime_context, dict):
        return ()

    runtime_lines: list[str] = []
    instructions = str(runtime_context.get("instructions") or "").strip()
    if instructions:
        runtime_lines.append(instructions)

    refs = runtime_context.get("required_refs") or []
    if refs:
        runtime_lines.append("Required knowledge refs: " + ", ".join(str(ref) for ref in refs))

    knowledge_query = str(runtime_context.get("knowledge_query") or "").strip()
    if knowledge_query:
        runtime_lines.append("Knowledge query: " + knowledge_query)

    required_capabilities = runtime_context.get("required_capabilities") or []
    if required_capabilities:
        runtime_lines.append(
            "Required runtime capabilities: "
            + ", ".join(str(capability) for capability in required_capabilities)
        )

    rules = runtime_context.get("rules") or []
    if rules:
        rule_descriptions = [
            str((rule or {}).get("description") or (rule or {}).get("rule_type"))
            for rule in rules
            if isinstance(rule, dict)
        ]
        if rule_descriptions:
            runtime_lines.append("Temporary task rules: " + "; ".join(rule_descriptions))

    return tuple(runtime_lines)


def task_runtime_requirements_prompt(runtime_context: Any) -> str | None:
    """Render the Runtime Requirements task prompt section."""

    runtime_lines = task_runtime_requirements_lines(runtime_context)
    if not runtime_lines:
        return None
    return "## Runtime Requirements For This Task\n" + "\n".join(
        f"- {line}" for line in runtime_lines
    )


def merge_task_runtime_capabilities(
    runtime_context: Any,
    capability_ids: Any,
    *,
    replace: bool = False,
    profile: RuntimeProfile | None = RuntimeProfile.BACKGROUND_WORKER,
    allowed_ids: tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    runtime = {} if not isinstance(runtime_context, dict) else dict(runtime_context)
    normalized = normalize_task_runtime_capability_ids(
        capability_ids,
        profile=profile,
        allowed_ids=allowed_ids,
    )
    if not normalized:
        return runtime
    if replace:
        runtime["required_capabilities"] = list(normalized)
        return runtime
    runtime["required_capabilities"] = list(normalize_task_runtime_capability_ids(
        list(runtime.get("required_capabilities") or []) + list(normalized),
        profile=profile,
        allowed_ids=allowed_ids,
    ))
    return runtime


def strategist_task_capability_descriptors(
    *,
    profile: RuntimeProfile = RuntimeProfile.BACKGROUND_WORKER,
) -> tuple[dict[str, Any], ...]:
    descriptors: list[dict[str, Any]] = []
    for capability_id in STRATEGIST_TASK_CAPABILITY_IDS:
        capability = capability_for_id(capability_id)
        if capability is None:
            continue
        tool_names = tuple(sorted(tool_names_for_capability_ids({capability_id}, profile=profile)))
        if not tool_names:
            continue
        descriptors.append({
            "id": capability.id,
            "name": capability.name,
            "description": capability.description,
            "risk_level": capability.risk_level,
            "required_approval": capability.required_approval,
        })
    return tuple(descriptors)
