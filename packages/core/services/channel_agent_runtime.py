from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from packages.core.ai.runtime import (
    RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME,
    runtime_build_base_prompt_for_turn,
    runtime_channel_attachment_tool_schema,
    runtime_envelope_meta,
    runtime_execute_channel_agent_loop,
    runtime_merge_prompt_appendix,
    runtime_prepare_prompt_appendix_for_turn,
    runtime_request_for_channel_turn,
)
from packages.core.database import async_session
from packages.core.services import channels as _channels_pkg  # noqa: F401
from packages.core.services.agent_subscription_service import ResolvedSubscription
from packages.core.services.channels import ADAPTERS

logger = logging.getLogger(__name__)

_MAX_AGENTIC_ROUNDS = 6


@dataclass(frozen=True)
class ChannelAgentRunResult:
    content: str
    runtime_meta: dict[str, Any] | None = None
    runtime_envelope: Any | None = None


async def run_channel_agent_turn(
    *,
    entity_id: str,
    agent_id: Optional[str],
    user_id: Optional[str],
    conversation_id: str,
    current_message: str,
    history: list[dict],
    sender_ctx: Optional[dict] = None,
    subscription: Optional[ResolvedSubscription] = None,
) -> Optional[ChannelAgentRunResult]:
    """Run the bound channel agent through the Runtime Harness."""
    from packages.core.constants.agents import is_master_agent
    from packages.core.services.workspace_runtime import resolve_workspace_runtime

    resolved_agent_id = agent_id
    is_master = is_master_agent(agent_id)

    sender_user_id = (sender_ctx or {}).get("user_id")
    sender_verified = bool((sender_ctx or {}).get("is_verified"))
    effective_user_id = sender_user_id if sender_user_id else (user_id if sender_verified else None)
    workspace_id = subscription.workspace_id if subscription else None

    try:
        async with async_session() as db:
            runtime = await resolve_workspace_runtime(
                db,
                entity_id=entity_id,
                user_id=effective_user_id,
                agent_id=resolved_agent_id,
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                is_master=is_master,
            )
    except Exception:
        logger.debug(
            "Channel workspace runtime DB resolution failed; using scoped fallback",
            exc_info=True,
        )
        runtime = await resolve_workspace_runtime(
            None,
            entity_id=entity_id,
            user_id=effective_user_id,
            agent_id=resolved_agent_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            is_master=is_master,
        )

    legacy_tool_profile = runtime.legacy_tool_profile
    if subscription and subscription.custom_prompt:
        base_prompt = subscription.custom_prompt
    else:
        base_prompt = await resolve_channel_base_prompt(
            entity_id,
            resolved_agent_id,
            is_master=is_master,
        )

    channel_cc = sender_ctx.get("_cc_obj") if sender_ctx else None
    channel_adapter_key = sender_ctx.get("channel_type") if sender_ctx else None
    channel_reply_to = sender_ctx.get("reply_to") if sender_ctx else None
    attachment_handler = None
    extra_tool_schemas: list[dict] = []
    extra_allowed_tool_names: set[str] = set()
    if channel_cc is not None and channel_reply_to:
        attachment_tool_schema, attachment_handler = build_channel_attachment_tool(
            channel_cc,
            channel_adapter_key,
            channel_reply_to,
        )
        extra_tool_schemas.append(attachment_tool_schema)
        extra_allowed_tool_names.add(RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME)

    runtime_request = runtime_request_for_channel_turn(
        entity_id=entity_id,
        user_id=effective_user_id,
        agent_id=resolved_agent_id,
        workspace_id=runtime.workspace_id,
        conversation_id=conversation_id,
        task_id=runtime.task_id,
        thread_ref_kind=runtime.thread_ref_kind,
        thread_ref_id=runtime.thread_ref_id,
        message=current_message,
        sender_context=sender_ctx,
        legacy_path="channel_agent_runtime.run_channel_agent_turn",
    )
    async with async_session() as db:
        appendix = await runtime_prepare_prompt_appendix_for_turn(
            db,
            request=runtime_request,
            legacy_runtime_profile=legacy_tool_profile,
            agent_id=resolved_agent_id,
            bound_tool_names=runtime.bound_tool_names,
            is_master=runtime.is_master,
            mcp_allowed_names=runtime.mcp_allowed_names,
            active_user_message=current_message,
            legacy_extra_context=runtime.extra_context,
            extra_tool_schemas=extra_tool_schemas,
            extra_allowed_tool_names=extra_allowed_tool_names,
        )
    tool_schemas = appendix.tool_schemas
    allowed_tool_names = appendix.allowed_tool_names
    runtime_envelope = appendix.envelope
    if RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME not in allowed_tool_names:
        attachment_handler = None

    system_prompt = runtime_merge_prompt_appendix(base_prompt, appendix)

    result = await runtime_execute_channel_agent_loop(
        runtime_envelope=runtime_envelope,
        system_prompt=system_prompt,
        user_message=current_message,
        tools=tool_schemas,
        entity_id=entity_id,
        user_id=effective_user_id or None,
        agent_id=resolved_agent_id,
        workspace_id=runtime.workspace_id,
        conversation_id=conversation_id,
        task_id=runtime.task_id,
        active_user_message=current_message,
        legacy_tool_profile=legacy_tool_profile,
        allowed_tool_names=allowed_tool_names,
        max_rounds=_MAX_AGENTIC_ROUNDS,
        initial_messages=history or None,
        dynamic_tool_handlers=(
            {RUNTIME_CHANNEL_ATTACHMENT_TOOL_NAME: attachment_handler}
            if attachment_handler is not None
            else None
        ),
    )
    content = (result.content or "").strip()
    if not content:
        return None
    return ChannelAgentRunResult(
        content=content,
        runtime_meta=runtime_envelope_meta(runtime_envelope),
        runtime_envelope=runtime_envelope,
    )


def _channel_base_prompt_fallback(*, is_master: bool) -> str:
    if is_master:
        return (
            "You are the Manor Master Agent, responding over a messaging "
            "channel. Be concise, helpful, and accurate."
        )
    return "You are a helpful assistant responding over a messaging channel."


async def resolve_channel_base_prompt(
    entity_id: str,
    agent_id: Optional[str],
    *,
    is_master: bool,
) -> str:
    """Build the same base identity prompt the web chat uses."""
    fallback = _channel_base_prompt_fallback(is_master=is_master)
    try:
        request = runtime_request_for_channel_turn(
            entity_id=entity_id,
            agent_id=agent_id,
            legacy_path="channel_agent_runtime.resolve_channel_base_prompt",
        )
        async with async_session() as db:
            result = await runtime_build_base_prompt_for_turn(
                db,
                request=request,
                agent_id=agent_id,
            )
            prompt = (result.prompt or "").strip()
            prompt_source = getattr(result.context, "prompt_source", None)
        if prompt and (is_master or prompt_source != "default"):
            return prompt
    except Exception:
        logger.debug("Channel base prompt load failed; using fallback", exc_info=True)
    return fallback


def build_channel_attachment_tool(cc, channel_type: str, reply_to: str):
    """Return a channel-local attachment sender tool schema and handler."""
    schema = runtime_channel_attachment_tool_schema()

    async def handler(args: dict) -> str:
        adapter = ADAPTERS.get(channel_type)
        if adapter is None:
            return json.dumps({"error": f"no adapter for {channel_type}"})
        url = args.get("url")
        kind = args.get("kind", "document")
        caption = args.get("caption")
        if not url:
            return json.dumps({"error": "url is required"})
        try:
            result = await adapter.send_attachment(
                cc,
                reply_to,
                url=url,
                kind=kind,
                caption=caption,
            )
        except NotImplementedError as exc:
            fallback = (caption + "\n\n" if caption else "") + str(url)
            try:
                await adapter.send_text(cc, reply_to, fallback)
                return json.dumps({
                    "sent_as": "text_fallback",
                    "reason": str(exc),
                })
            except Exception as fallback_exc:
                return json.dumps({
                    "error": f"attachment + fallback failed: {fallback_exc}",
                })
        except Exception as exc:
            return json.dumps({"error": f"send_attachment failed: {exc}"})
        return json.dumps({"sent_as": "attachment", "result": result}, default=str)

    return schema, handler
