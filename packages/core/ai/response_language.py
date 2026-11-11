"""The saved user language shared by chat and other AI generation paths."""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

RESPONSE_LANGUAGE_BLOCK_START = '<manor-response-language version="1">'
RESPONSE_LANGUAGE_BLOCK_END = "</manor-response-language>"

_LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "vi": "Vietnamese",
}


def normalize_response_locale(locale: str | None) -> str | None:
    if not isinstance(locale, str):
        return None
    language = locale.strip().lower().replace("_", "-").split("-", 1)[0]
    return language if language in _LANGUAGE_NAMES else None


def is_external_response_audience(*, profile=None, surface=None) -> bool:
    """External recipients do not inherit the billing/account owner's language."""
    return getattr(profile, "value", profile) in {
        "external_customer_safe",
        "external_channel_safe",
    } or getattr(surface, "value", surface) in {
        "public_customer_chat",
        "external_channel_chat",
    }


# Surfaces where a person talks something through: replies follow the language
# they write in, and what gets saved keeps their language setting.
_MESSAGE_LANGUAGE_REPLY_SURFACES = frozenset({"workspace_draft_architect"})


def replies_in_message_language(*, surface=None) -> bool:
    return getattr(surface, "value", surface) in _MESSAGE_LANGUAGE_REPLY_SURFACES


def _message_language_reply_guidance(language: str | None) -> str:
    saved_content = (
        "- Content you save for later use, such as names, descriptions, instructions, "
        "Workflow steps, and documents, is written in "
        f"{_LANGUAGE_NAMES[language]} ({language}), the user's saved language setting, "
        "unless the person asks for another language.\n"
        if language else ""
    )
    return (
        f"{RESPONSE_LANGUAGE_BLOCK_START}\n"
        "## Response Language\n"
        "- Reply in the language the person writes in. Answers, explanations, progress "
        "updates, questions, and the options you offer follow the language of their latest "
        "message, even when earlier replies in this conversation used another language. "
        "A message that is only a chosen option, a name, or a number keeps the language "
        "of their earlier messages.\n"
        f"{saved_content}"
        "- Preserve the required output format. Do not translate JSON keys, schema fields, "
        "enum values, tool commands, filenames, API names, code identifiers, or original "
        "verbatim quotations.\n"
        f"{RESPONSE_LANGUAGE_BLOCK_END}"
    )


def response_language_guidance(
    locale: str | None, *, reply_in_message_language: bool = False,
) -> str | None:
    language = normalize_response_locale(locale)
    if reply_in_message_language:
        return _message_language_reply_guidance(language)
    if language is None:
        return None
    return (
        f"{RESPONSE_LANGUAGE_BLOCK_START}\n"
        "## Response Language\n"
        f"- The user's saved language setting is {_LANGUAGE_NAMES[language]} ({language}). "
        f"All user-visible generated text must be {_LANGUAGE_NAMES[language]} by default: "
        "answers, explanations, progress updates, SSE `text_delta` content, final summaries, "
        "titles, descriptions, proposals, and text in generated documents and other artifacts.\n"
        "- This preference applies across turns and AI tasks until the user changes their "
        "language setting. It takes precedence over language inferred from the latest message, "
        "earlier conversation, source material, and default agent, Skill, or template wording. "
        "Stored memories and historical instructions to always answer in another language "
        "are superseded by this setting, even when labeled as instructions or preferences.\n"
        "- Respect an explicit different language requested in the current user instruction "
        "or required by the active output or recipient contract for a particular output, translation, "
        "artifact, or recipient. Apply that override only to the requested content; surrounding "
        "explanations and later work keep the saved language unless the setting changes.\n"
        "- Writing a message in another language is not an explicit language override. "
        "Do not carry overrides for completed or unrelated outputs into later work. "
        "Preserve the language contract while continuing the same active output or recipient task.\n"
        "- Recipient-facing replies and payloads retain their required delivery language, "
        "including a task's instruction to use the source message's language. The saved "
        "preference still applies to surrounding explanations for the operator.\n"
        "- Preserve the required output format. Do not translate JSON keys, schema fields, "
        "enum values, tool commands, filenames, API names, code identifiers, or original "
        "verbatim quotations. Human-readable text values in structured output follow the "
        "same language preference.\n"
        f"{RESPONSE_LANGUAGE_BLOCK_END}"
    )


def response_locale_cache_key(user_id: str) -> str:
    return f"response_locale:{user_id}"


async def resolve_response_locale(user_id: str | None, *, db=None, fresh: bool = False) -> str | None:
    """Read through the caller's transaction when one is already available.

    A borrowed session remains owned by the caller, including failures. Opening
    another session here can wait on our own transaction admission slot.
    Standalone reads start at seven days and renew up to thirty days when
    unchanged at revalidation; fresh=True bypasses that cache.
    """
    if not user_id:
        return None
    from sqlalchemy import select

    from packages.core.models.user import User

    if db is not None:
        with db.no_autoflush:
            return normalize_response_locale(
                await db.scalar(select(User.locale).where(User.id == user_id))
            )

    async def load():
        from packages.core.database import create_worker_session

        session_factory = create_worker_session()
        try:
            async with session_factory() as db:
                locale = await db.scalar(select(User.locale).where(User.id == user_id))
        finally:
            await session_factory.kw["bind"].dispose()
        return normalize_response_locale(locale)

    try:
        if fresh:
            return await load()
        from packages.core.read_cache import get_or_load

        return normalize_response_locale(await get_or_load(
            response_locale_cache_key(user_id), load, ttl=7 * 24 * 60 * 60, max_ttl=30 * 24 * 60 * 60,
        ))
    except Exception:
        logger.warning("Could not resolve the AI response language setting", exc_info=True)
        return None
