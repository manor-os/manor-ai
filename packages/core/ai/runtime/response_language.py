from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re
from typing import Any, Iterable

from packages.core.ai.response_language import (
    is_external_response_audience,
    RESPONSE_LANGUAGE_BLOCK_END,
    RESPONSE_LANGUAGE_BLOCK_START,
    resolve_response_locale,
    response_language_guidance,
)
from packages.core.i18n import SUPPORTED_LOCALES
from packages.core.ai.runtime.agentic_loop_prompts import runtime_is_host_feedback_message


_LANGUAGE_BLOCK_PATTERN = re.compile(
    r'(?:\n\n)?<manor-response-language version="\d+">.*?</manor-response-language>',
    re.DOTALL,
)
_LEGACY_LANGUAGE_BLOCKS = tuple(
    guidance.removeprefix(f"{RESPONSE_LANGUAGE_BLOCK_START}\n").removesuffix(f"\n{RESPONSE_LANGUAGE_BLOCK_END}")
    for locale in sorted(SUPPORTED_LOCALES)
    if (guidance := response_language_guidance(locale))
)


@dataclass(frozen=True)
class RuntimeResponseLanguage:
    entity_id: str | None
    user_id: str | None
    locale: str | None = None
    external_audience: bool = False


_response_language: ContextVar[RuntimeResponseLanguage | None] = ContextVar(
    "runtime_response_language", default=None,
)


async def resolve_runtime_response_language(
    *,
    entity_id: str | None,
    user_id: str | None,
    profile: Any = None,
    surface: Any = None,
    source: str | None = None,
    db: Any = None,
    user: Any = None,
) -> RuntimeResponseLanguage:
    """Freeze the actor's current preference for a turn and its nested calls."""
    parent = _response_language.get()
    same_scope = bool(
        parent is not None
        and entity_id == parent.entity_id
        and (not user_id or user_id == parent.user_id)
    )
    external = is_external_response_audience(profile=profile, surface=surface)
    # Legacy channel one-shots do not carry an envelope. An explicit internal
    # surface still wins for verified staff talking through a channel.
    if profile is None and surface is None and source in {"channel", "channel_hold"}:
        external = True
    if same_scope and parent is not None:
        if external and not parent.external_audience:
            return RuntimeResponseLanguage(entity_id, user_id or parent.user_id, external_audience=True)
        return parent
    if external:
        return RuntimeResponseLanguage(entity_id, user_id, external_audience=True)
    if not user_id and entity_id:
        from packages.core.ai.runtime.billing import runtime_current_billing_context

        billing = runtime_current_billing_context()
        if billing is not None and billing.entity_id == entity_id:
            user_id = billing.user_id
    # A loaded actor snapshot is fresher than the optional cross-request cache.
    # Read __dict__ to avoid lazy SQL on expired ORM attributes. Never use a
    # different actor's preference or put an AsyncSession into a ContextVar.
    loaded = getattr(user, "__dict__", {})
    if user_id and loaded.get("id") == user_id and "locale" in loaded:
        from packages.core.ai.response_language import normalize_response_locale

        locale = normalize_response_locale(loaded["locale"])
    else:
        locale = (
            await resolve_response_locale(user_id, db=db)
            if db is not None else await resolve_response_locale(user_id)
        )
    return RuntimeResponseLanguage(entity_id, user_id, locale=locale)


@contextmanager
def runtime_response_language_scope(language: RuntimeResponseLanguage):
    token = _response_language.set(language)
    try:
        yield
    finally:
        _response_language.reset(token)


def _without_response_language_guidance(prompt: str) -> str:
    """Remove only Runtime-owned blocks, including exact pre-marker checkpoint text."""
    from packages.core.ai.runtime.prompt_guidance import runtime_response_language_guidance

    prompt = _LANGUAGE_BLOCK_PATTERN.sub("", prompt)
    legacy_chinese = runtime_response_language_guidance("中文")
    for guidance in (*_LEGACY_LANGUAGE_BLOCKS, legacy_chinese):
        if guidance:
            prompt = prompt.replace(f"\n\n{guidance}", "").replace(guidance, "")
    return prompt


def response_language_prompt(prompt: str, language: RuntimeResponseLanguage) -> str:
    guidance = None if language.external_audience else response_language_guidance(language.locale)
    if not guidance and not language.external_audience:
        return prompt
    prompt = _without_response_language_guidance(prompt)
    if not guidance:
        return prompt
    return f"{prompt}\n\n{guidance}" if prompt else guidance


def response_language_messages(
    messages: Iterable[dict[str, Any]], language: RuntimeResponseLanguage,
) -> list[dict[str, Any]]:
    """Keep caller history and machine output contracts intact."""
    guidance = response_language_prompt("", language)
    if not guidance and not language.external_audience:
        return list(messages)
    result = []
    for message in messages:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            content = _without_response_language_guidance(message["content"])
            if content != message["content"]:
                if not content:
                    continue
                message = {**message, "content": content}
        result.append(message)
    if not guidance:
        return result
    # Keep the current account setting next to this turn's request. In a long
    # Workspace transcript, a preference buried in the first system prompt can
    # lose to the language established by the intervening assistant history.
    # Inserting before the last user also leaves tool-call/result groups intact.
    # Runtime feedback is user-role transport, not the request.
    insertion_index = next(
        (
            index for index in range(len(result) - 1, -1, -1)
            if result[index].get("role") == "user" and not runtime_is_host_feedback_message(result[index])
        ),
        len(result),
    )
    result.insert(insertion_index, {"role": "system", "content": guidance})
    return result


def current_response_language_messages(
    messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    language = _response_language.get()
    return response_language_messages(messages, language) if language else list(messages)
