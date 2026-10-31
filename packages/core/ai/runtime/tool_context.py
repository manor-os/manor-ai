from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.capabilities import _TOOL_CAPABILITY_ALIAS_TARGETS
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.sandbox import runtime_task_binding_key, runtime_workflow_binding_key
from packages.core.ai.runtime.surfaces import ChatSurface


# The only tools whose authorization is bound to a sandbox rather than to a
# chat thread. Derived from the capability aliases so a new ``sandbox_*`` alias
# cannot silently fall out of the set.
#
# This is narrower than the ``sandbox.execute`` capability on purpose: that
# capability also covers ``video_edit``, which keeps its own session record
# (scoped by the same binding key) and never reads a sandbox-tool binding, so
# it is authorized against the run's real conversation like any other tool.
RUNTIME_SANDBOX_BOUND_TOOL_NAMES = frozenset(
    {"sandbox"}
    | {
        name
        for name, target in _TOOL_CAPABILITY_ALIAS_TARGETS.items()
        if target == "sandbox"
    }
)


RUNTIME_TOOL_CONTEXT_KEYS = frozenset(
    {
        "_agent_id_from_context",
        "_user_id_from_context",
        "_active_user_message_from_context",
        "_runtime_artifact_urls_from_context",
        "_dependency_artifact_urls_from_context",
        "_manual_skill_selected_from_context",
        "_manual_skill_ids_from_context",
        "_manual_skill_slugs_from_context",
        "_tool_profile_from_context",
        "_runtime_envelope_from_context",
        "_allowed_tool_names_from_context",
        "_llm_metadata_from_context",
        "_llm_model_from_context",
        "_performance_run_id_from_context",
        "_runtime_run_id_from_context",
        "_runtime_tool_call_id_from_context",
        "_runtime_tool_attempt_from_context",
        "_runtime_validation_duration_ms_from_context",
        "_runtime_chrome_interaction_mode_from_context",
        "_runtime_chrome_sequence_child_from_context",
        "_runtime_chrome_expected_connection_id_from_context",
        "_runtime_human_input_from_context",
        "_runtime_parent_skill_from_context",
        "workspace_id",
        "conversation_id",
        "task_id",
        "_workflow_project_id_from_context",
        "_workflow_run_id_from_context",
        "_workflow_lineage_root_run_id_from_context",
        "_workflow_project_root_from_context",
        "_workflow_action_grant_id_from_context",
        "_workflow_step_id_from_context",
        "_workflow_scene_id_from_context",
        "_workflow_batch_capture_from_context",
        "_approved_plan_version_from_context",
        "_workflow_effect_deadline_at_from_context",
        "_workflow_effect_deadline_action_keys_from_context",
        "_media_lineage_from_context",
    }
)


def _string_set(values: Iterable[Any] | None) -> set[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {str(value).strip() for value in values if str(value or "").strip()}


def _url_set(values: Iterable[Any] | None) -> set[str]:
    return _string_set(values)


def runtime_allowed_tool_names_from_context(kwargs: dict[str, Any]) -> set[str] | None:
    allowed_tool_names = kwargs.get("_allowed_tool_names_from_context")
    if isinstance(allowed_tool_names, (list, tuple, set, frozenset)):
        return _string_set(allowed_tool_names)
    return None


def runtime_manual_skill_slugs_from_context(kwargs: dict[str, Any]) -> set[str]:
    return {value.lower() for value in _string_set(kwargs.get("_manual_skill_slugs_from_context"))}


def runtime_manual_skill_ids_from_context(kwargs: dict[str, Any]) -> set[str]:
    return _string_set(kwargs.get("_manual_skill_ids_from_context"))


def runtime_manual_skill_selected_from_context(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("_manual_skill_selected_from_context"))


def runtime_active_user_message_from_context(kwargs: dict[str, Any]) -> str | None:
    message = kwargs.get("_active_user_message_from_context")
    return message if isinstance(message, str) else None


@dataclass(frozen=True)
class RuntimeToolCallContext:
    entity_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    active_user_message: str | None = None
    runtime_artifact_urls: frozenset[str] = frozenset()
    dependency_artifact_urls: frozenset[str] = frozenset()
    manual_skill_selected: bool = False
    manual_skill_ids: frozenset[str] = frozenset()
    manual_skill_slugs: frozenset[str] = frozenset()
    tool_profile: str | None = None
    runtime_envelope: Any | None = None
    allowed_tool_names: frozenset[str] | None = None
    llm_metadata: dict[str, Any] | None = None
    llm_model: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    sandbox_binding_key: str | None = None
    task_id: str | None = None
    workflow_project_id: str | None = None
    workflow_run_id: str | None = None
    workflow_lineage_root_run_id: str | None = None
    workflow_project_root: str | None = None
    workflow_action_grant_id: str | None = None
    workflow_step_id: str | None = None
    workflow_scene_id: str | None = None
    workflow_batch_capture: str | None = None
    approved_plan_version: str | None = None
    workflow_effect_deadline_at: str | None = None
    workflow_effect_deadline_action_keys: frozenset[str] = frozenset()
    performance_run_id: str | None = None
    runtime_run_id: str | None = None
    runtime_tool_call_id: str | None = None
    runtime_tool_attempt: int = 1
    runtime_validation_duration_ms: float = 0.0
    chrome_interaction_mode: str | None = None
    chrome_sequence_child: bool = False
    chrome_expected_connection_id: str | None = None
    # The prompt skill whose nested agent loop is executing this call, if any.
    # A nested loop cannot park the durable run on a checkpoint (its own
    # state has no checkpoint to resume from), so sandbox admission must not
    # queue a suspending reservation for it.
    parent_skill: str | None = None


class RuntimeToolContextConflictError(ValueError):
    """Raised when trusted runtime scope sources disagree."""


def runtime_agent_subscription_id_from_context(
    context: RuntimeToolCallContext,
) -> str | None:
    """Return the exact deployment id carried by the trusted Runtime envelope."""

    metadata = getattr(context.runtime_envelope, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    return str(metadata.get("agent_subscription_id") or "").strip() or None


def runtime_trusted_workspace_id_from_context(
    context: RuntimeToolCallContext,
) -> str | None:
    """Return Workspace scope safe for identity-free background authorization.

    A plain ``workspace_id`` tool argument is model-controlled unless it is
    corroborated by a Runtime envelope. User-scoped calls can rely on normal
    ACL checks, while userless calls may use only the envelope's Workspace.
    """

    if context.user_id:
        return context.workspace_id
    return str(
        getattr(context.runtime_envelope, "workspace_id", None) or ""
    ).strip() or None


def _runtime_scope_value(name: str, *values: Any) -> str | None:
    normalized = [str(value).strip() for value in values if str(value or "").strip()]
    if len(set(normalized)) > 1:
        raise RuntimeToolContextConflictError(f"Conflicting runtime scope for {name}.")
    return normalized[0] if normalized else None


def runtime_tool_call_context_from_kwargs(
    kwargs: dict[str, Any],
    *,
    entity_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    runtime_envelope: Any | None = None,
) -> RuntimeToolCallContext:
    """Build one tool context from explicit, injected, and envelope scope."""

    injected_envelope = kwargs.get("_runtime_envelope_from_context")
    envelope = runtime_envelope if runtime_envelope is not None else injected_envelope
    envelopes = tuple(
        candidate
        for candidate in (runtime_envelope, injected_envelope)
        if candidate is not None
    )

    def envelope_values(name: str) -> tuple[Any, ...]:
        return tuple(getattr(candidate, name, None) for candidate in envelopes)

    allowed = runtime_allowed_tool_names_from_context(kwargs)
    resolved_user_id = _runtime_scope_value(
        "user_id",
        user_id,
        kwargs.get("_user_id_from_context"),
        *envelope_values("user_id"),
    )
    resolved_conversation_id = _runtime_scope_value(
        "conversation_id",
        conversation_id,
        kwargs.get("conversation_id"),
        *envelope_values("conversation_id"),
    )
    resolved_task_id = _runtime_scope_value(
        "task_id",
        task_id,
        kwargs.get("task_id"),
        *envelope_values("task_id"),
    )
    # A task-worker run has no chat thread, so nothing would bind its sandbox:
    # admission would refuse to register, the authorization recheck would bail,
    # and every sandbox call would be denied. Bind it to a deterministic key
    # derived from the task instead. This is the one choke point for tool
    # scope, so every consumer sees the same key.
    #
    # The key is exposed ONLY as ``sandbox_binding_key``; ``conversation_id``
    # keeps the run's real conversation (None for these runs), and
    # ``runtime_injected_tool_context_args`` never carries the key. Injecting
    # it into the plain ``conversation_id`` kwarg would spread a fake
    # conversation into every handler: ``workspace_create_task`` would persist
    # it on the child task, ``manor(action="send_email")`` would stamp it on
    # message_logs, platform MCP billing would record it on credit
    # reservations, and the handlers that look a conversation up (file
    # permission HITL, workflow tools, workspace operation actions) would query
    # for a row that cannot exist and silently deny where they saw None before.
    #
    # The consumers of the key are exactly:
    #   - sandbox admission and reuse scoping (sandbox_session_service)
    #   - the sandbox tool handlers' per-call owner match and context load
    #   - the sandbox media guard in generate_file and the office skill handoff
    #   - the authorization request for the ``sandbox`` tool and its aliases,
    #     carried as ``sandbox_binding_key`` beside the real conversation
    #     (``runtime_authorization_sandbox_binding_key``)
    #
    # The key is sized to fit a ``String(26)`` conversation column, so the
    # sites that persist this scope -- the sandbox binding itself, and
    # ``hitl_requests.origin_conversation_id`` for a sandbox tool's approval --
    # store it instead of truncating. The envelope keeps its own null
    # conversation either way.
    #
    # Anti-forgery rests on ``_runtime_scope_value``, not on task_id being
    # unforgeable: ``conversation_id`` and ``task_id`` are both trusted
    # runtime keys, and a model-supplied value that disagrees with the
    # runtime's raises a conflict rather than winning. The residual case is
    # a run that carries neither a runtime task nor a conversation, where an
    # unopposed model-supplied task_id could mint a key. A binding slot is
    # keyed on (entity, user, binding key, agent, kind), so such a key can
    # only reach sandboxes bound to the same entity, user (or the same
    # user-less identity), and agent.
    #
    # A user-less run (Strategist/scheduler tasks run with user_id=None by
    # design) gets the key too. There is no identity-free path: a sandbox
    # call with no binding key is denied, so a user-less task without its
    # key could not operate the sandbox it opened, and before scoped
    # bindings it skipped every owner check instead.
    #
    # With cloud external-runner coordination these runs have no durable
    # ``RuntimeRun`` to suspend on a reservation, so the session service
    # enqueues a *task* reservation keyed on ``task_id`` and waits
    # synchronously for the scheduler to allocate a runner; the key below is
    # still what binds and re-finds the resulting sandbox.
    #
    # Ownership (see ``packages.core.ai.runtime.sandbox``): a human run's
    # sandboxes belong to the user in the run's conversation, which delegates
    # and sub-agents share because they run in their parent's conversation. A
    # user-less run (no human owner) is keyed by its TASK even when it carries
    # a conversation, so system work never shares a container with a human's
    # conversation sandbox.
    task_key = runtime_task_binding_key(resolved_task_id) if resolved_task_id else None
    workflow_run_id = str(kwargs.get("_workflow_run_id_from_context") or "").strip() or None
    workflow_key = runtime_workflow_binding_key(workflow_run_id) if workflow_run_id else None
    # Standalone Workflow agents (including nested Skills) need a work area
    # too. Only the trusted run context supplies it; Task/Conversation fields
    # stay real and existing human-chat and Task bindings keep precedence.
    if resolved_user_id:
        sandbox_binding_key = resolved_conversation_id or task_key or workflow_key
    else:
        sandbox_binding_key = task_key or workflow_key or resolved_conversation_id
    return RuntimeToolCallContext(
        entity_id=_runtime_scope_value("entity_id", entity_id, *envelope_values("entity_id")),
        agent_id=_runtime_scope_value(
            "agent_id",
            agent_id,
            kwargs.get("_agent_id_from_context"),
            *envelope_values("agent_id"),
        ),
        user_id=resolved_user_id,
        active_user_message=runtime_active_user_message_from_context(kwargs),
        runtime_artifact_urls=frozenset(_url_set(kwargs.get("_runtime_artifact_urls_from_context"))),
        dependency_artifact_urls=frozenset(_url_set(kwargs.get("_dependency_artifact_urls_from_context"))),
        manual_skill_selected=runtime_manual_skill_selected_from_context(kwargs),
        manual_skill_ids=frozenset(runtime_manual_skill_ids_from_context(kwargs)),
        manual_skill_slugs=frozenset(runtime_manual_skill_slugs_from_context(kwargs)),
        tool_profile=str(kwargs.get("_tool_profile_from_context") or "") or None,
        runtime_envelope=envelope,
        allowed_tool_names=frozenset(allowed) if allowed is not None else None,
        llm_metadata=(
            dict(kwargs["_llm_metadata_from_context"])
            if isinstance(kwargs.get("_llm_metadata_from_context"), dict)
            else None
        ),
        llm_model=str(kwargs.get("_llm_model_from_context") or "") or None,
        workspace_id=_runtime_scope_value(
            "workspace_id",
            workspace_id,
            kwargs.get("workspace_id"),
            *envelope_values("workspace_id"),
        ),
        conversation_id=resolved_conversation_id,
        sandbox_binding_key=sandbox_binding_key,
        task_id=resolved_task_id,
        workflow_project_id=str(kwargs.get("_workflow_project_id_from_context") or "") or None,
        workflow_run_id=workflow_run_id,
        workflow_lineage_root_run_id=(
            str(kwargs.get("_workflow_lineage_root_run_id_from_context") or "") or None
        ),
        workflow_project_root=(
            str(kwargs.get("_workflow_project_root_from_context") or "") or None
        ),
        workflow_action_grant_id=(
            str(kwargs.get("_workflow_action_grant_id_from_context") or "") or None
        ),
        workflow_step_id=str(kwargs.get("_workflow_step_id_from_context") or "") or None,
        workflow_scene_id=str(kwargs.get("_workflow_scene_id_from_context") or "") or None,
        workflow_batch_capture=(
            str(kwargs.get("_workflow_batch_capture_from_context") or "") or None
        ),
        approved_plan_version=(
            str(kwargs.get("_approved_plan_version_from_context") or "") or None
        ),
        workflow_effect_deadline_at=(
            str(kwargs.get("_workflow_effect_deadline_at_from_context") or "") or None
        ),
        workflow_effect_deadline_action_keys=frozenset(
            _string_set(kwargs.get("_workflow_effect_deadline_action_keys_from_context"))
        ),
        performance_run_id=(
            str(kwargs.get("_performance_run_id_from_context") or "") or None
        ),
        runtime_run_id=str(kwargs.get("_runtime_run_id_from_context") or "") or None,
        runtime_tool_call_id=(
            str(kwargs.get("_runtime_tool_call_id_from_context") or "") or None
        ),
        parent_skill=str(kwargs.get("_runtime_parent_skill_from_context") or "") or None,
        runtime_tool_attempt=max(
            1,
            int(kwargs.get("_runtime_tool_attempt_from_context") or 1),
        ),
        runtime_validation_duration_ms=(
            float(kwargs["_runtime_validation_duration_ms_from_context"])
            if isinstance(
                kwargs.get("_runtime_validation_duration_ms_from_context"),
                (int, float),
            )
            and not isinstance(
                kwargs.get("_runtime_validation_duration_ms_from_context"),
                bool,
            )
            else 0.0
        ),
        chrome_interaction_mode=(
            "fast"
            if kwargs.get("_runtime_chrome_interaction_mode_from_context") == "fast"
            else None
        ),
        chrome_sequence_child=(
            kwargs.get("_runtime_chrome_sequence_child_from_context") is True
        ),
        chrome_expected_connection_id=(
            str(kwargs.get("_runtime_chrome_expected_connection_id_from_context") or "").strip()
            or None
        ),
    )


def runtime_tool_call_context_from_handler(
    kwargs: dict[str, Any],
    *,
    user_id: str | None = None,
) -> RuntimeToolCallContext:
    """Preserve trusted direct-call identity without trusting model arguments.

    Runtime execution injects an identity key (including for an anonymous
    caller) or an envelope. In that case the injected scope wins and the
    handler's top-level ``user_id`` is ignored. Trusted internal callers that
    have no Runtime injection may still use the explicit handler parameter.
    """
    direct_user_id = (
        user_id
        if "_user_id_from_context" not in kwargs
        and "_runtime_envelope_from_context" not in kwargs
        else None
    )
    return runtime_tool_call_context_from_kwargs(kwargs, user_id=direct_user_id)


def runtime_authorization_sandbox_binding_key(
    context: RuntimeToolCallContext,
    tool_name: str | None,
) -> str | None:
    """Return the sandbox binding an authorization request should carry.

    Sandbox-bound tools are authorized against the sandbox binding, so a
    conversation-less Task or Workflow run still presents a scope the
    binding check can look up. The request keeps the real conversation
    separately; every other tool carries no binding.
    """

    if str(tool_name or "").strip() in RUNTIME_SANDBOX_BOUND_TOOL_NAMES:
        return context.sandbox_binding_key
    return None


def runtime_tool_call_context_is_external_customer(kwargs: dict[str, Any]) -> bool:
    """Return true when a tool is executing for a customer/external surface."""

    context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = context.runtime_envelope
    surface = getattr(envelope, "surface", None)
    profile = getattr(envelope, "profile", None)
    surface_value = getattr(surface, "value", surface)
    profile_value = getattr(profile, "value", profile)
    return surface_value in {
        ChatSurface.PUBLIC_CUSTOMER_CHAT.value,
        ChatSurface.EXTERNAL_CHANNEL_CHAT.value,
    } or profile_value in {
        RuntimeProfile.EXTERNAL_CUSTOMER_SAFE.value,
        RuntimeProfile.EXTERNAL_CHANNEL_SAFE.value,
    }


def runtime_tool_call_context_is_public_customer(kwargs: dict[str, Any]) -> bool:
    """Return true only for QR/public webchat customer turns."""

    context = runtime_tool_call_context_from_kwargs(kwargs)
    envelope = context.runtime_envelope
    surface = getattr(envelope, "surface", None)
    surface_value = getattr(surface, "value", surface)
    return surface_value == ChatSurface.PUBLIC_CUSTOMER_CHAT.value


def runtime_injected_tool_context_args(
    *,
    agent_id: str | None = None,
    user_id: str | None = None,
    active_user_message: str | None = None,
    runtime_artifact_urls: Iterable[str] | None = None,
    dependency_artifact_urls: Iterable[str] | None = None,
    manual_skill_selected: bool = False,
    manual_skill_ids: Iterable[str] | None = None,
    manual_skill_slugs: Iterable[str] | None = None,
    tool_profile: str | None = None,
    runtime_envelope: Any | None = None,
    allowed_tool_names: Iterable[str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
    llm_model: str | None = None,
    workspace_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    chrome_interaction_mode: str | None = None,
    chrome_sequence_child: bool = False,
    chrome_expected_connection_id: str | None = None,
    runtime_validation_duration_ms: float | None = None,
    workflow_effect_deadline_at: str | None = None,
    workflow_effect_deadline_action_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "_agent_id_from_context": agent_id,
        "_user_id_from_context": user_id,
        "_active_user_message_from_context": active_user_message,
        "_manual_skill_selected_from_context": manual_skill_selected,
        "_manual_skill_ids_from_context": list(manual_skill_ids or []),
        "_manual_skill_slugs_from_context": list(manual_skill_slugs or []),
        "_tool_profile_from_context": tool_profile,
    }
    from packages.core.ai.runtime.artifacts import (
        runtime_current_artifact_urls,
        runtime_current_dependency_artifact_urls,
    )

    merged_runtime_artifact_urls = set(runtime_current_artifact_urls()) | _url_set(runtime_artifact_urls)
    merged_dependency_artifact_urls = set(runtime_current_dependency_artifact_urls()) | _url_set(
        dependency_artifact_urls
    )

    if runtime_envelope is not None:
        args["_runtime_envelope_from_context"] = runtime_envelope
    if runtime_artifact_urls is not None or merged_runtime_artifact_urls:
        args["_runtime_artifact_urls_from_context"] = sorted(
            str(url).strip() for url in merged_runtime_artifact_urls if str(url or "").strip()
        )
    if dependency_artifact_urls is not None or merged_dependency_artifact_urls:
        args["_dependency_artifact_urls_from_context"] = sorted(
            str(url).strip() for url in merged_dependency_artifact_urls if str(url or "").strip()
        )
    if isinstance(llm_metadata, dict) and llm_metadata:
        args["_llm_metadata_from_context"] = dict(llm_metadata)
    if llm_model:
        args["_llm_model_from_context"] = str(llm_model)
    if allowed_tool_names is not None:
        args["_allowed_tool_names_from_context"] = sorted(
            str(name) for name in allowed_tool_names if str(name or "").strip()
        )
    if workspace_id:
        args["workspace_id"] = workspace_id
    if conversation_id:
        args["conversation_id"] = conversation_id
    if task_id:
        args["task_id"] = task_id
    if workflow_effect_deadline_at:
        args["_workflow_effect_deadline_at_from_context"] = workflow_effect_deadline_at
    if workflow_effect_deadline_action_keys is not None:
        args["_workflow_effect_deadline_action_keys_from_context"] = list(
            _string_set(workflow_effect_deadline_action_keys)
        )
    if chrome_interaction_mode == "fast":
        args["_runtime_chrome_interaction_mode_from_context"] = "fast"
    if chrome_sequence_child:
        args["_runtime_chrome_sequence_child_from_context"] = True
    if chrome_expected_connection_id:
        args["_runtime_chrome_expected_connection_id_from_context"] = str(
            chrome_expected_connection_id
        )
    if isinstance(runtime_validation_duration_ms, (int, float)) and not isinstance(
        runtime_validation_duration_ms,
        bool,
    ):
        args["_runtime_validation_duration_ms_from_context"] = max(
            float(runtime_validation_duration_ms),
            0.0,
        )
    return args
