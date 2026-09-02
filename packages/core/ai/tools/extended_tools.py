"""
Extended tools — web_fetch, extract_data, generate_image, generate_video.

Ported from manor-multi-agent's runtime/extended_tools.py.
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import secrets
from html import unescape
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from PIL import Image

from packages.core.contracts.audio_generation import (
    AudioGenerationCompletedResult,
    AudioGenerationErrorCode,
    AudioGenerationErrorResult,
    AudioGenerationFormat,
    AudioGenerationProvider,
    AudioGenerationPurpose,
    AudioGenerationRole,
    AudioGenerationStatus,
    GenerateFileKind,
    NARRATION_VOICE_MODE_FIXED_PER_WORKSPACE,
    NARRATION_VOICE_MODE_RANDOM_PER_TASK,
    normalize_audio_generation_purpose,
)
from packages.core.services.workspace_layout import WorkspaceArtifactDir
from packages.core.services.workspace_audio_defaults import (
    DEFAULT_WORKSPACE_AUDIO_LANGUAGE,
    normalize_workspace_audio_language,
    workspace_audio_language,
)
from packages.core.services.model_gateway import (
    resolve_official_model_route as _resolve_official_model_route,
)
from packages.core.services.model_provider_handlers import vercel_catalog_model_type
from packages.core.services.audio_conversion import (
    SUPPORTED_AUDIO_ARTIFACT_FORMATS,
    transcode_audio_bytes,
    validate_generated_audio_bytes,
)
from packages.core.ai.runtime import (
    RUNTIME_GENERATE_AUDIO_TOOL_SOURCE,
    RUNTIME_GENERATE_IMAGE_TOOL_SOURCE,
    RUNTIME_GENERATE_VIDEO_TOOL_SOURCE,
    runtime_execute_extract_data_tool_completion,
    runtime_assert_credit_available,
)
from packages.core.ai.runtime.tool_context import (
    runtime_active_user_message_from_context,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.ai.runtime.artifacts import runtime_reference_allowed_by_artifacts
from packages.core.services.voice.speech_request import begin_speech_provider_request

logger = logging.getLogger(__name__)

# Pre-compiled regexes for HTML→markdown conversion
_RE_SCRIPT_STYLE = re.compile(
    r"<(script|style|nav|footer|header|noscript)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_RE_HEADINGS = {i: re.compile(rf"<h{i}[^>]*>(.*?)</h{i}>", re.DOTALL | re.IGNORECASE) for i in range(1, 7)}
_RE_BOLD = re.compile(r"<(b|strong)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_ITALIC = re.compile(r"<(i|em)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_CODE_BLOCK = re.compile(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", re.DOTALL | re.IGNORECASE)
_RE_CODE_INLINE = re.compile(r"<code[^>]*>(.*?)</code>", re.DOTALL | re.IGNORECASE)
_RE_LINK = re.compile(r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_RE_IMG = re.compile(r'<img[^>]+alt="([^"]*)"[^>]+src="([^"]*)"[^>]*/?\s*>', re.IGNORECASE)
_RE_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)
_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_P_OPEN = re.compile(r"<p[^>]*>", re.IGNORECASE)
_RE_P_CLOSE = re.compile(r"</p>", re.IGNORECASE)
_RE_HR = re.compile(r"<hr[^>]*/?>", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_BLANK_LINES = re.compile(r"\n{3,}")

# ── web_fetch ────────────────────────────────────────────────────────────────

WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch a URL with a static HTTP request and return clean text or markdown. "
            "Supports HTML pages (converted to markdown) and PDF files (text extracted). "
            "Does not run JavaScript; use browse_web for JavaScript-rendered sites or SPAs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "as_markdown": {
                    "type": "boolean",
                    "description": "Convert HTML to markdown (default true). False for plain text.",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Max characters to return (default 12000, hard cap 40000).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start character offset for continuation (default 0).",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional previous source_sha256; returns source_changed if page text changed.",
                },
            },
            "required": ["url"],
        },
    },
}


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _bounded_int(value: Any, default: int, maximum: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _html_to_text(html: str, as_markdown: bool = True) -> str:
    """Convert HTML to clean text or markdown using pre-compiled regexes."""
    text = _RE_SCRIPT_STYLE.sub("", html)
    text = _RE_COMMENT.sub("", text)

    if as_markdown:
        for i in range(1, 7):
            text = _RE_HEADINGS[i].sub(rf"\n{'#' * i} \1\n", text)
        text = _RE_BOLD.sub(r"**\2**", text)
        text = _RE_ITALIC.sub(r"*\2*", text)
        text = _RE_CODE_BLOCK.sub(r"\n```\n\1\n```\n", text)
        text = _RE_CODE_INLINE.sub(r"`\1`", text)
        text = _RE_LINK.sub(r"[\2](\1)", text)
        text = _RE_IMG.sub(r"![\1](\2)", text)
        text = _RE_LI.sub(r"\n- \1", text)
        text = _RE_BR.sub("\n", text)
        text = _RE_P_OPEN.sub("\n\n", text)
        text = _RE_P_CLOSE.sub("", text)
        text = _RE_HR.sub("\n---\n", text)

    text = _RE_TAG.sub("", text)
    text = unescape(text)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _looks_like_dynamic_shell(raw_html: str, extracted_text: str) -> bool:
    """Detect SPA shells where static HTML is not the real page content."""
    raw_l = raw_html[:20_000].lower()
    if not raw_l:
        return False
    script_count = raw_l.count("<script")
    has_app_mount = bool(re.search(r"<div[^>]+id=[\"'](?:app|root|__next|__nuxt)[\"']", raw_l))
    has_bundled_asset = bool(re.search(r"/assets/[^\"']+\.(?:js|mjs)|type=[\"']module[\"']", raw_l))
    text_l = (extracted_text or "").strip().lower()
    text_is_short = len(text_l) < 1_000
    mostly_bootstrap = text_is_short and any(
        marker in raw_l
        for marker in (
            "__app_config__",
            "__next_data__",
            "window.__",
            "vite",
            "vue",
            "react",
            "data-reactroot",
        )
    )
    return text_is_short and script_count > 0 and (has_app_mount or has_bundled_asset or mostly_bootstrap)


def _extract_pdf_text(content: bytes, max_pages: int = 50) -> str:
    """Extract text from PDF bytes."""
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = []
        for i, page in enumerate(reader.pages[:max_pages]):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(f"--- Page {i + 1} ---\n{page_text.strip()}")
        return "\n\n".join(pages) if pages else "(no text extracted from PDF)"
    except ImportError:
        return "(pypdf not installed — cannot extract PDF text)"
    except Exception as e:
        return f"(PDF extraction failed: {e})"


async def _web_fetch_handler(entity_id: str = "", **kwargs: Any) -> str:
    """Fetch URL content."""
    from packages.core.services.web_fetch import fetch_url

    url = kwargs.get("url", "").strip()
    if not url:
        return json.dumps({"error": "url is required"})

    as_markdown = kwargs.get("as_markdown", True)
    max_length = _bounded_int(kwargs.get("max_length"), 12_000, 40_000, 1_000)
    offset = _bounded_int(kwargs.get("offset"), 0, 10_000_000, 0)
    expected_sha256 = str(kwargs.get("expected_sha256") or "").strip()

    try:
        result = await fetch_url(url)
        content_type = result.content_type or ""
        dynamic_page_hint = None

        # PDF detection
        if "application/pdf" in content_type or url.lower().endswith(".pdf"):
            text = _extract_pdf_text(result.content)
        else:
            raw = result.content.decode("utf-8", errors="replace")
            if "<html" in raw.lower()[:500] or "<body" in raw.lower()[:500]:
                text = _html_to_text(raw, as_markdown=as_markdown)
                if _looks_like_dynamic_shell(raw, text):
                    dynamic_page_hint = (
                        "This looks like a JavaScript-rendered page or SPA shell. "
                        "Use search_tools to load browse_web, then call browse_web for rendered visible content."
                    )
            else:
                text = raw

        source_sha256 = _text_sha256(text)
        if expected_sha256 and expected_sha256 != source_sha256:
            return json.dumps(
                {
                    "error": "source_changed",
                    "url": url,
                    "expected_sha256": expected_sha256,
                    "source_sha256": source_sha256,
                    "content_type": content_type,
                    "hint": "The fetched page text changed; restart from offset=0.",
                },
                ensure_ascii=False,
            )

        total_chars = len(text)
        content_out = text[offset : offset + max_length]
        next_offset = offset + len(content_out) if offset + len(content_out) < total_chars else None
        hint = None
        if next_offset is not None:
            hint = "Call web_fetch again with offset=next_offset and expected_sha256=source_sha256 to continue."

        payload = {
            "url": url,
            "content_type": content_type,
            "source_sha256": source_sha256,
            "slice_sha256": _text_sha256(content_out),
            "offset": offset,
            "chars_returned": len(content_out),
            "total_chars": total_chars,
            "next_offset": next_offset,
            "truncated": next_offset is not None,
            "max_length": max_length,
            "hint": hint,
            "content": content_out or "(empty response)",
        }
        if dynamic_page_hint:
            payload["dynamic_page_hint"] = dynamic_page_hint
        return json.dumps(payload, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Failed to fetch {url}: {e}"})


# ── extract_data ─────────────────────────────────────────────────────────────

EXTRACT_DATA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "extract_data",
        "description": (
            "Extract structured data from text using AI. Provide a task description, "
            "source text, and optionally a JSON schema for the output format."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "What to extract (e.g. 'names and emails')"},
                "text": {"type": "string", "description": "Source text to extract from"},
                "schema": {"type": "string", "description": "Optional JSON schema for output structure"},
            },
            "required": ["task", "text"],
        },
    },
}


async def _extract_data_handler(entity_id: str = "", **kwargs: Any) -> str:
    """Extract structured data using LLM."""
    task = kwargs.get("task", "")
    text = kwargs.get("text", "")
    output_schema = kwargs.get("schema", "")

    if not task or not text:
        return json.dumps({"error": "task and text are required"})

    try:
        completion = await runtime_execute_extract_data_tool_completion(
            entity_id=entity_id or None,
            task=task,
            text=text,
            output_schema=output_schema,
        )
        content = completion.content
        return content or json.dumps({"error": "No extraction result"})
    except Exception as e:
        return json.dumps({"error": f"Extraction failed: {e}"})


# ── Shared BYOK helper for media tools ────────────────────────────────────────


async def _resolve_user_api_key(user_id: str, entity_id: str, role: str | None = None) -> tuple[str, bool]:
    """Resolve the API key for media tools.

    Returns (api_key, is_byok). Checks tenant-scoped BYOK settings first.
    Role-specific media calls only use the matching role key; a primary chat
    key may point at another provider and must not be reused for image/video/
    audio generation.
    """
    if entity_id:
        try:
            from packages.core.database import async_session
            from packages.core.services.model_resolver import resolve_llm_metadata_for_user

            async with async_session() as db:
                metadata = await resolve_llm_metadata_for_user(
                    role or "primary",
                    user_id=user_id or None,
                    entity_id=entity_id,
                    db=db,
                )
                key = (metadata or {}).get("llm_api_key")
                if key:
                    return str(key).strip(), True
        except Exception:
            logger.debug("Tenant BYOK media key lookup failed", exc_info=True)
    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() != "cloud":
        return "", False
    return "", False


async def _resolve_media_task_user_id(
    user_id: str,
    entity_id: str,
    task_id: str | None,
) -> str:
    """Fill missing media tool user context from the owning task when available."""

    user_text = str(user_id or "").strip()
    task_text = str(task_id or "").strip()
    entity_text = str(entity_id or "").strip()
    if not task_text or not entity_text:
        return user_text if user_text != "ai-agent" else ""
    try:
        from packages.core.ai.runtime import runtime_resolve_task_billable_user_id
        from packages.core.database import async_session
        from packages.core.models.task import Task
        from sqlalchemy import select

        async with async_session() as db:
            task = (
                await db.execute(
                    select(Task).where(
                        Task.id == task_text,
                        Task.entity_id == entity_text,
                    )
                )
            ).scalar_one_or_none()
            if task is None:
                from packages.core.services.task_requester_identity import (
                    TaskRequesterIdentityError,
                )

                raise TaskRequesterIdentityError(
                    task_text,
                    "the media Task does not exist in the requested entity scope",
                )
            return await runtime_resolve_task_billable_user_id(db, task) or ""
    except Exception as exc:
        from packages.core.services.task_requester_identity import (
            TaskRequesterIdentityError,
        )

        if isinstance(exc, TaskRequesterIdentityError):
            raise
        logger.debug("media task user lookup failed", exc_info=True)
        return ""


def _media_context_user_id(user_id: str, runtime_user_id: str | None) -> str:
    explicit = str(user_id or "").strip()
    if explicit and explicit != "ai-agent":
        return explicit
    runtime_user = str(runtime_user_id or "").strip()
    if runtime_user and runtime_user != "ai-agent":
        return runtime_user
    return explicit


def _platform_native_media_key(provider: str) -> str:
    """Return a platform env key only for the selected native media provider."""
    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() != "cloud":
        return ""
    envs_by_provider = {
        "openai": ("OPENAI_API_KEY",),
        "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "bytedance": (
            "VOLCENGINE_LAS_API_KEY",
            "VOLCENGINE_API_KEY",
            "SEEDANCE_API_KEY",
            "BYTEDANCE_API_KEY",
        ),
        "kwaivgi": ("KLING_API_KEY", "KLINGAI_API_KEY"),
        "zyphra": ("ZYPHRA_API_KEY",),
    }
    for env_name in envs_by_provider.get((provider or "").lower(), ()):
        value = (os.getenv(env_name) or "").strip()
        if value:
            return value
    return ""


def _native_media_base_url(provider: str, base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if provider == "google" and base.endswith("/openai"):
        return base[: -len("/openai")]
    return base


async def _platform_native_media_credential_async(provider: str) -> tuple[str, str]:
    """Return platform official key and base URL for a native media provider."""
    selected = (provider or "").lower()
    if not selected:
        return "", ""
    if os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() != "cloud":
        return "", ""
    try:
        from packages.core.services.model_gateway import resolve_gateway_credential

        credential = await resolve_gateway_credential(
            selected,
            reason=f"media.{selected}.official_provider_key",
        )
        if credential and credential.api_key:
            return credential.api_key, _native_media_base_url(selected, credential.base_url)
    except Exception:
        logger.debug("Official native media key lookup failed for %s", selected, exc_info=True)
    return _platform_native_media_key(selected), ""


async def _resolve_managed_media_route(
    model: str,
    *,
    role: str,
    provider: str,
) -> Any:
    """Resolve managed media credentials without narrowing the product catalog."""
    selected = (provider or "").strip().lower()
    # A Vercel route is valid only for catalog entries with an exact v4
    # protocol. Other entries keep their native provider/OpenRouter chain.
    if selected in {"openrouter", "sesame"} or (
        role == "voice" and not _vercel_speech_model_supported(model)
    ):
        chain = ("openrouter",)
    elif vercel_catalog_model_type(role, model):
        chain = tuple(
            dict.fromkeys(
                candidate
                for candidate in ("vercel", selected, "openrouter")
                if candidate
            )
        )
    else:
        chain = tuple(
            dict.fromkeys(
                candidate
                for candidate in (selected, "openrouter")
                if candidate
            )
        )
    route_kwargs = {
        "reason": f"media.{role}.official_provider_key",
        "vercel_reason": f"media.{role}.vercel_gateway_key",
        "openrouter_reason": f"media.{role}.openrouter_fallback_key",
    }
    if chain == ("openrouter",):
        # Keep the narrow gateway override explicit for callers/tests that
        # distinguish OpenRouter's chat/audio transport from Vercel media.
        route_kwargs["gateway_provider"] = "openrouter"
    else:
        route_kwargs["provider_chain"] = chain
    return await _resolve_official_model_route(model, **route_kwargs)


async def _platform_native_media_key_async(provider: str) -> str:
    """Return a platform official key for the selected native media provider."""
    key, _base_url = await _platform_native_media_credential_async(provider)
    return key


def _prefer_native_video_credentials(api_key: str, provider: str, is_byok: bool) -> tuple[str, bool]:
    """Prefer Manor's native official video route over platform OpenRouter defaults.

    User BYOK keys stay authoritative. Platform defaults can include an
    OpenRouter key for broad catalog fallback, but Seedance/Kling should use the
    official native adapter when Manor has that provider key configured.
    """
    if is_byok:
        return api_key, is_byok

    selected_provider = (provider or "").lower()
    if selected_provider in {"bytedance", "kwaivgi"}:
        native_key = _platform_native_media_key(selected_provider)
        if native_key:
            return native_key, False

    return api_key, is_byok


async def _resolve_user_media_credentials(
    user_id: str,
    entity_id: str,
    role: str,
) -> tuple[str, str, bool]:
    """Resolve a role-specific media key plus optional native provider base URL."""
    api_key, is_byok = await _resolve_user_api_key(user_id, entity_id, role=role)
    base_url = ""
    if is_byok and entity_id:
        try:
            from packages.core.database import async_session
            from packages.core.services.model_resolver import resolve_llm_metadata_for_user

            async with async_session() as db:
                metadata = await resolve_llm_metadata_for_user(
                    role,
                    user_id=user_id or None,
                    entity_id=entity_id,
                    db=db,
                )
                base_url = str((metadata or {}).get("llm_base_url") or "").strip().rstrip("/")
        except Exception:
            logger.debug("Tenant BYOK media base URL lookup failed", exc_info=True)

    if not base_url:
        env_key = f"{role.upper()}_BASE_URL"
        base_url = (os.getenv(env_key) or os.getenv(f"LLM_{env_key}") or "").strip().rstrip("/")

    return api_key, base_url, is_byok


async def _resolve_primary_byok_media_credentials(
    user_id: str,
    entity_id: str,
    *,
    provider: str,
) -> tuple[str, str, bool]:
    """Reuse Primary BYOK only when its key matches the selected media provider."""
    if not entity_id or not provider:
        return "", "", False
    try:
        from packages.core.database import async_session
        from packages.core.services.model_resolver import resolve_llm_metadata_for_user

        async with async_session() as db:
            metadata = await resolve_llm_metadata_for_user(
                "primary",
                user_id=user_id or None,
                entity_id=entity_id,
                db=db,
            )
        key = str((metadata or {}).get("llm_api_key") or "").strip()
        if not key or not _is_native_key_for_provider(key, provider):
            return "", "", False
        base_url = str((metadata or {}).get("llm_base_url") or "").strip().rstrip("/")
        return key, base_url, True
    except Exception:
        logger.debug("Compatible Primary BYOK media lookup failed", exc_info=True)
        return "", "", False


def _catalog_provider(model: str) -> str:
    return (model or "").split("/", 1)[0].strip().lower() if "/" in (model or "") else ""


def _is_openrouter_base_url(base_url: str) -> bool:
    hostname = (urlsplit(str(base_url or "").strip()).hostname or "").lower()
    return hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")


def _native_media_model(model: str, *, kind: str, provider: str) -> str:
    """Map Manor/OpenRouter catalog IDs to native provider model IDs."""
    raw = (model or "").split("/", 1)[1] if "/" in (model or "") else (model or "")
    image_map = {
        # Historical saved settings only. Current Catalog IDs already match
        # OpenAI's wire model IDs and therefore fall through to ``raw``.
        "openai/gpt-5-image-mini": "gpt-image-1-mini",
        "openai/gpt-5.4-image-2": "gpt-image-2",
    }
    video_map = {
        # Volcengine Ark exposes Doubao model IDs, not Manor's catalog labels.
        "bytedance/seedance-2.0": "doubao-seedance-2-0-260128",
        "bytedance/seedance-2.0-fast": "doubao-seedance-2-0-fast-260128",
        "kwaivgi/kling-v3.0-std": "kling-v3.0-std",
        "kwaivgi/kling-v3.0-pro": "kling-v3.0-pro",
    }
    if kind == "image":
        return image_map.get(model, raw)
    if kind == "video":
        try:
            from packages.core.tasks.video_adapters import native_video_model

            return native_video_model(model)
        except Exception:
            return video_map.get(model, raw)
    return raw


def _media_key_provider_mismatch(api_key: str, provider: str) -> str:
    key = (api_key or "").strip()
    selected = (provider or "").lower()
    if key.startswith("ark-") and selected and selected != "bytedance":
        target = {"kwaivgi": "Kling", "atlascloud": "Atlas Cloud"}.get(selected, selected)
        return (
            "The saved video API key looks like a Volcengine/Seedance key, "
            f"but the selected video model is {target}. Select a Seedance model, "
            "clear the video provider key to use Manor credits, or save a "
            f"{target} API key."
        )
    return ""


def _image_mime_to_ext(mime: str) -> str:
    lowered = (mime or "").lower()
    if "jpeg" in lowered or "jpg" in lowered:
        return ".jpg"
    if "webp" in lowered:
        return ".webp"
    return ".png"


def _audio_format_to_ext(fmt: str) -> str:
    lowered = (fmt or "mp3").lower().lstrip(".")
    if lowered in {"wav", "wave"}:
        return ".wav"
    if lowered in {"flac", "opus", "aac", "ogg", "mp3"}:
        return f".{lowered}"
    if lowered in {"pcm", "pcm16"}:
        return ".pcm"
    return ".mp3"


def _audio_format_to_mime(fmt: str) -> str:
    lowered = (fmt or "mp3").lower().lstrip(".")
    return {
        "wav": "audio/wav",
        "wave": "audio/wav",
        "flac": "audio/flac",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "ogg": "audio/ogg",
        "pcm": "audio/L16",
        "pcm16": "audio/L16",
        "mp3": "audio/mpeg",
    }.get(lowered, "audio/mpeg")


def _normalize_audio_format(fmt: str) -> str:
    lowered = (fmt or "").strip().lower().lstrip(".")
    if lowered == "wave":
        return "wav"
    if lowered == "pcm16":
        return "pcm"
    return lowered


def _requested_audio_artifact_format(requested_format: str, output_name: str = "") -> str:
    requested = _normalize_audio_format(requested_format)
    if not requested:
        requested = _normalize_audio_format(os.path.splitext(output_name)[1])
    if requested == "pcm":
        return "wav"
    return requested if requested in SUPPORTED_AUDIO_ARTIFACT_FORMATS else ""


def _openrouter_audio_formats(model: str, role: str, requested_format: str = "") -> tuple[str, str]:
    """Return provider request format and stored artifact format.

    Gemini TTS currently only accepts ``response_format=pcm`` through
    OpenRouter. Store that raw PCM as WAV so the generated artifact is playable
    in browsers and media tools.
    """
    model_id = (model or "").lower()
    requested = _normalize_audio_format(requested_format)
    if role == "voice" and model_id.startswith("google/") and "tts" in model_id:
        return "pcm", "wav"
    if role == "voice" and model_id.startswith("zyphra/"):
        return "mp3", "mp3"
    if role in {"audio", "sfx"} and model_id.startswith("openai/"):
        return "pcm16", "wav"
    if requested:
        storage = "wav" if requested == "pcm" else requested
        return requested, storage
    if role in {"audio", "sfx"}:
        return "wav", "wav"
    return "mp3", "mp3"


def _is_native_key_for_provider(api_key: str, provider: str) -> bool:
    """Best-effort check that a media BYOK key matches the selected provider."""
    key = (api_key or "").strip()
    if not key or key.startswith("sk-or-"):
        return False
    try:
        from packages.core.services.model_resolver import detect_llm_provider_from_key

        detected = detect_llm_provider_from_key(key)
        return not detected or detected == (provider or "").lower()
    except Exception:
        return True


def _wav_from_pcm16(pcm_bytes: bytes, *, sample_rate: int = 24000, channels: int = 1) -> bytes:
    import io
    import wave

    if pcm_bytes[:4] == b"RIFF":
        validate_generated_audio_bytes(pcm_bytes, "wav")
        return pcm_bytes
    validate_generated_audio_bytes(pcm_bytes, "pcm")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return buffer.getvalue()


def _fs_path_from_result_url(url: str, entity_id: str) -> str | None:
    prefix = f"/api/v1/fs/{entity_id}/"
    if url.startswith(prefix):
        return url[len(prefix) :]
    return None


def _image_result_payload(
    *,
    image_url: str,
    prompt: str,
    size: str,
    model: str,
    entity_id: str,
    include_fs_path: bool = False,
    saved_to_knowledge: bool | None = None,
) -> dict[str, Any]:
    payload = {"image_url": image_url, "prompt": prompt, "size": size, "model": model}
    if saved_to_knowledge is not None:
        payload["saved_to_knowledge"] = saved_to_knowledge
    # Always report where the file landed. The system — not the model —
    # chooses the path now, so withholding it leaves the next step unable to
    # find what this one produced. ``include_fs_path`` used to depend on the
    # caller having passed workspace_id in kwargs, which the runtime-context
    # path does not, so the model often never saw the location at all.
    fs_path = _fs_path_from_result_url(image_url, entity_id)
    if fs_path or include_fs_path:
        payload["fs_path"] = fs_path
    if fs_path:
        # `sandbox_write_file` deliberately names this argument
        # `workspace_path`.  Returning the same canonical entity-relative path
        # under that exact key removes the model-side translation/guess that
        # previously dropped the leading `images/` directory.
        payload["workspace_path"] = fs_path
    return payload


_WORKSPACE_REUSABLE_MEDIA_ASSETS_KEY = "reusable_media_assets"


async def _workspace_stickman_studio_profile(
    *,
    entity_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Load the Workspace policy that makes Stickman identity deterministic.

    A prompt may omit ``workspace_asset_key`` or ``narration_voice_mode``.
    Workspace identity is an operator-owned policy, so the media handlers must
    still enforce it instead of silently falling back to task-scoped identity.
    """

    if not str(entity_id or "").strip() or not str(workspace_id or "").strip():
        return {}

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace

    async with async_session() as db:
        workspace = (
            await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if workspace is None:
            return {}
        profile = (workspace.settings or {}).get("stickman_studio_profile")
        return dict(profile) if isinstance(profile, dict) else {}


async def _workspace_default_audio_language(
    *,
    entity_id: str,
    workspace_id: str,
) -> str:
    """Load the default speech language for one Workspace."""

    if not str(entity_id or "").strip() or not str(workspace_id or "").strip():
        return DEFAULT_WORKSPACE_AUDIO_LANGUAGE

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace

    async with async_session() as db:
        workspace = (
            await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if workspace is None:
            return DEFAULT_WORKSPACE_AUDIO_LANGUAGE
        return workspace_audio_language(workspace.settings)


def _voice_instructions_with_language(instructions: str, language: str) -> str:
    """Add one provider-facing language directive without changing spoken text."""

    directive = f"Speak in {normalize_workspace_audio_language(language)}."
    base = str(instructions or "").strip()
    if directive.lower() in base.lower():
        return base
    return f"{directive} {base}".strip()


def _workspace_asset_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")[:80]


async def _load_workspace_reusable_image(
    *,
    entity_id: str,
    workspace_id: str,
    asset_key: str,
) -> dict[str, Any] | None:
    """Return a reusable Workspace image only while its file still exists."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace
    from packages.core.services.entity_fs import get_entity_root

    async with async_session() as db:
        workspace = (
            await db.execute(
                select(Workspace).where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if workspace is None:
            return None
        assets = dict((workspace.settings or {}).get(_WORKSPACE_REUSABLE_MEDIA_ASSETS_KEY) or {})
        record = assets.get(asset_key)
        if not isinstance(record, dict) or record.get("kind") != "image":
            return None
        fs_path = str(record.get("fs_path") or "").strip().lstrip("/")
        result_url = str(record.get("result_url") or "").strip()
        if not fs_path or not result_url:
            return None
        if not os.path.isfile(os.path.join(get_entity_root(entity_id), fs_path)):
            return None
        return dict(record)


async def _remember_workspace_reusable_image(
    *,
    entity_id: str,
    workspace_id: str,
    asset_key: str,
    image_url: str,
    prompt: str,
    model: str,
    size: str,
) -> dict[str, Any]:
    """Persist the canonical locator for one reusable Workspace image."""

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace

    fs_path = _fs_path_from_result_url(image_url, entity_id)
    record: dict[str, Any] = {
        "version": 1,
        "kind": "image",
        "result_url": image_url,
        "fs_path": fs_path,
        "prompt": prompt,
        "model": model,
        "size": size,
    }
    async with async_session() as db:
        workspace = (
            await db.execute(
                select(Workspace)
                .where(
                    Workspace.id == workspace_id,
                    Workspace.entity_id == entity_id,
                    Workspace.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if workspace is None:
            raise RuntimeError(f"Workspace reusable asset scope could not resolve {workspace_id}")
        settings = dict(workspace.settings or {})
        assets = dict(settings.get(_WORKSPACE_REUSABLE_MEDIA_ASSETS_KEY) or {})
        assets[asset_key] = record
        settings[_WORKSPACE_REUSABLE_MEDIA_ASSETS_KEY] = assets
        workspace.settings = settings
        await db.commit()
    return record


async def _generated_image_result_payload(
    *,
    image_url: str,
    prompt: str,
    size: str,
    model: str,
    entity_id: str,
    workspace_id: str | None,
    workspace_asset_key: str,
    saved_to_knowledge: bool,
    include_fs_path: bool,
) -> dict[str, Any]:
    if workspace_asset_key and workspace_id:
        await _remember_workspace_reusable_image(
            entity_id=entity_id,
            workspace_id=workspace_id,
            asset_key=workspace_asset_key,
            image_url=image_url,
            prompt=prompt,
            model=model,
            size=size,
        )
    payload = _image_result_payload(
        image_url=image_url,
        prompt=prompt,
        size=size,
        model=model,
        entity_id=entity_id,
        include_fs_path=include_fs_path or bool(workspace_asset_key),
        saved_to_knowledge=saved_to_knowledge,
    )
    if workspace_asset_key:
        payload.update(
            workspace_asset_key=workspace_asset_key,
            reused_workspace_asset=False,
        )
    return payload


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


async def _deliver_image_to_sandbox(
    *,
    conversation_id: str | None,
    sandbox_path: str,
    image_bytes: bytes,
) -> bool:
    """Best-effort: push generated image bytes straight into the active sandbox.

    Lets a skill (e.g. pptx image mode) consume the image from inside the
    sandbox immediately, instead of waiting for the read-only entity-FS mount
    to propagate. Any failure is logged and swallowed so it never breaks image
    generation; the entity-FS copy still exists as a fallback.
    """
    if not conversation_id or not sandbox_path or not image_bytes:
        return False
    try:
        import base64 as _b64

        from packages.core.ai.runtime import runtime_load_sandbox_context
        from packages.core.config import get_settings
        from packages.core.services.sandbox_sdk import SandboxClient

        ctx = await runtime_load_sandbox_context(conversation_id)
        sandbox_id = (ctx or {}).get("sandbox_id") if isinstance(ctx, dict) else None
        if not sandbox_id:
            return False
        sandbox_url = (get_settings().SANDBOX_SERVICE_URL or "").strip()
        if not sandbox_url:
            return False
        client = SandboxClient(
            base_url=sandbox_url,
            timeout=120.0,
            api_token=get_settings().SANDBOX_API_TOKEN,
        )
        try:
            await client.write_file_base64(
                sandbox_id=sandbox_id,
                path=sandbox_path,
                content_base64=_b64.b64encode(image_bytes).decode("ascii"),
            )
        finally:
            await client.close()
        logger.info("Delivered generated image into sandbox %s at %s", sandbox_id, sandbox_path)
        return True
    except Exception as exc:  # never let delivery break generation
        logger.warning("Sandbox image delivery failed (%s): %s", sandbox_path, exc)
        return False


async def _save_generated_image_bytes(
    *,
    entity_id: str,
    user_id: str,
    prompt: str,
    model: str,
    size: str,
    image_bytes: bytes,
    mime: str,
    is_byok: bool,
    output_name: str = "",
    usage: dict | None = None,
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
    save_to_knowledge: bool = True,
    sandbox_path: str | None = None,
    workspace_shared: bool = False,
) -> str:
    """Persist an AI-generated image and optionally register it as a document."""
    import base64

    if not entity_id:
        return f"data:{mime or 'image/png'};base64,{base64.b64encode(image_bytes).decode('ascii')}"

    ext = _image_mime_to_ext(mime)

    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.services.entity_fs import get_entity_root
    from packages.core.services.generated_media_naming import (
        build_generated_media_target,
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
        workspace_artifact_default_dir,
    )

    entity_root = get_entity_root(entity_id)
    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=None if workspace_shared else task_id,
    )
    target = build_generated_media_target(
        prompt=prompt,
        desired_name=scope_workspace_artifact_path(
            output_name,
            workspace_base_dir,
            preserve_leaf_default=True,
        ),
        ext=ext,
        fallback="generated-image",
        default_dir=workspace_artifact_default_dir(workspace_base_dir, WorkspaceArtifactDir.IMAGES.value),
        entity_root=entity_root,
    )
    filename = target.filename
    filepath = runtime_write_entity_file_atomic(
        entity_id,
        target.rel_path,
        image_bytes,
        expected_size=len(image_bytes),
        allow_empty=False,
    )

    image_url = f"/api/v1/fs/{entity_id}/{target.rel_path}"
    logger.info("Generated image saved: %s (%d bytes)", filepath, len(image_bytes))

    if sandbox_path:
        await _deliver_image_to_sandbox(
            conversation_id=conversation_id,
            sandbox_path=sandbox_path,
            image_bytes=image_bytes,
        )

    prompt_toks = int((usage or {}).get("prompt_tokens") or 0)
    completion_toks = int((usage or {}).get("completion_tokens") or 0)
    cost_usd = 0.0
    if prompt_toks or completion_toks:
        try:
            from packages.core.services.billing_service import estimate_provider_cost

            cost_usd = float(estimate_provider_cost(prompt_toks, completion_toks, model))
        except Exception:
            cost_usd = 0.0
    if cost_usd <= 0:
        cost_usd = _estimate_image_cost(model, size=size)

    await _bill_media(
        entity_id=entity_id,
        user_id=user_id,
        kind="image",
        model=model,
        cost_usd=cost_usd,
        units=1,
        byok=is_byok,
    )

    if not save_to_knowledge:
        logger.info("Generated image kept out of Knowledge: %s", image_url)
        return image_url

    try:
        from packages.core.database import create_worker_session
        from packages.core.services.document_service import upsert_document_by_fs_path
        from packages.core.services.document_metadata import merge_document_metadata

        factory = create_worker_session()
        async with factory() as db:
            folder_id = await _ensure_generated_media_document_folder(
                entity_id=entity_id,
                workspace_id=workspace_id,
                rel_path=target.rel_path,
                rel_dir=target.rel_dir,
            )
            doc = await upsert_document_by_fs_path(
                db,
                entity_id,
                name=filename,
                fs_path=target.rel_path,
                file_size=len(image_bytes),
                file_type=ext.lstrip("."),
                mime_type=mime or "image/png",
                source="ai_generated",
                created_by=user_id or None,
                folder_id=folder_id,
            )
            doc.source = "ai_generated"
            if user_id:
                doc.created_by = user_id
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                artifact={"role": "final", "storage_scope": "artifact"},
                origin={
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "tool_name": "generate_image",
                },
                generation={"prompt": prompt, "model": model, "params": {"size": size}},
            )
            await db.commit()
            if workspace_id:
                from packages.core.services.knowledge_sync import bind_document_to_workspace

                await bind_document_to_workspace(
                    entity_id=entity_id,
                    document_id=doc.id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tool_name="generate_image",
                )
    except Exception:
        logger.warning("Failed to register generated image as document", exc_info=True)

    return image_url


async def _ensure_generated_media_document_folder(
    *,
    entity_id: str,
    workspace_id: str | None,
    rel_path: str,
    rel_dir: str,
) -> str | None:
    if workspace_id:
        from packages.core.services.workspace_artifacts import (
            ensure_workspace_document_folder,
        )

        return await ensure_workspace_document_folder(
            entity_id=entity_id,
            workspace_id=workspace_id,
            rel_path=rel_path,
        )

    from packages.core.services.knowledge_sync import ensure_folder_path

    return await ensure_folder_path(entity_id, rel_dir)


async def _resolve_user_audio_model(
    user_id: str,
    entity_id: str,
    *,
    purpose: AudioGenerationPurpose,
) -> tuple[str, AudioGenerationRole]:
    """Resolve the Account-selected OpenRouter audio model.

    ``voice`` is for speech/narration/dialogue. ``audio`` is for music.
    ``sfx`` is for ambience, Foley, transitions, and discrete sound effects.
    """
    from packages.core.services.model_resolver import resolve_model_for_user

    if purpose in {
        AudioGenerationPurpose.SPEECH,
        AudioGenerationPurpose.DIALOGUE,
        AudioGenerationPurpose.NARRATION,
    }:
        role = AudioGenerationRole.VOICE
        fallback = "openai/tts-1-hd"
    elif purpose in {
        AudioGenerationPurpose.SFX,
        AudioGenerationPurpose.AMBIENCE,
        AudioGenerationPurpose.SOUNDSCAPE,
        AudioGenerationPurpose.TRANSITION,
    }:
        role = AudioGenerationRole.SFX
        fallback = ""
    else:
        role = AudioGenerationRole.AUDIO
        fallback = ""
    try:
        return (
            await resolve_model_for_user(role, user_id=user_id or None, entity_id=entity_id or None)
        ) or fallback, role
    except Exception as exc:
        logger.debug("audio model resolution fell back to default: %s", exc)
        return fallback, role


def _default_openrouter_voice(model: str) -> str:
    lowered = (model or "").lower()
    if lowered.startswith("google/"):
        return "Zephyr"
    if lowered.startswith("openai/"):
        return "alloy"
    if lowered.startswith("sesame/"):
        # OpenRouter's speech endpoint requires an explicit voice for CSM.
        # ``alloy`` is the provider-documented portable default.
        return "alloy"
    if lowered.startswith("hexgrad/"):
        # Kokoro exposes many language-specific voices, while OpenRouter's
        # normalized endpoint accepts ``alloy`` as a portable default.
        return "alloy"
    if lowered.startswith("zyphra/"):
        return "american_female"
    return ""


_TASK_NARRATOR_PROFILE_KEY = "stickman_narrator_profile"
_WORKSPACE_NARRATOR_PROFILE_KEY = "stickman_narrator_profile"
_TASK_NARRATOR_CONCRETE_VOICE_CHOICES: dict[str, tuple[str, ...]] = {
    "google": (
        "Aoede",
        "Charon",
        "Fenrir",
        "Kore",
        "Puck",
        "Zephyr",
    ),
    "openai": (
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    ),
}
_WORKSPACE_NARRATOR_DEFAULT_VOICE: dict[str, str] = {
    "google": "Puck",
    "openai": "alloy",
}


def _task_narrator_model_provider(model: str) -> str:
    """Return the provider only when ``model`` supports concrete TTS voices."""
    model_id = str(model or "").strip().lower()
    provider = _catalog_provider(model_id)
    if provider == "google" and "tts" in model_id:
        return provider
    if provider == "openai" and (
        model_id.startswith("openai/tts-") or model_id.endswith("-tts")
    ):
        return provider
    return ""


class _NarrationProfileError(RuntimeError):
    """A task-scoped narrator profile cannot be created or trusted."""

    def __init__(self, *, code: AudioGenerationErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def _validated_task_narrator_profile(value: object) -> dict[str, str | int]:
    if not isinstance(value, dict):
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_INVALID,
            detail="The task narrator profile is malformed.",
        )
    version = value.get("version")
    provider = str(value.get("provider") or "").strip().lower()
    model = str(value.get("model") or "").strip()
    voice = str(value.get("voice") or "").strip()
    voice_instructions = str(value.get("voice_instructions") or "").strip()
    if version != 1 or not provider or not model or not voice or voice.lower() == "random":
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_INVALID,
            detail="The task narrator profile must contain one concrete supported model and voice.",
        )
    supported_provider = _task_narrator_model_provider(model)
    if not supported_provider:
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_UNSUPPORTED,
            detail="The task narrator profile must use a supported Google or OpenAI TTS model.",
        )
    if supported_provider != provider:
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_INVALID,
            detail="The task narrator profile provider does not match its model.",
        )
    if voice not in _TASK_NARRATOR_CONCRETE_VOICE_CHOICES.get(provider, ()):
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_INVALID,
            detail="The task narrator profile contains an unsupported concrete voice.",
        )
    return {
        "version": 1,
        "provider": provider,
        "model": model,
        "voice": voice,
        "voice_instructions": voice_instructions,
    }


def _task_narrator_audio_output_name(output_name: str, voice: str) -> str:
    """Place task-scoped narration segments under their concrete voice folder."""
    voice_slug = re.sub(r"[^a-z0-9]+", "-", str(voice or "").strip().lower()).strip("-")
    if not voice_slug:
        raise ValueError("Task narrator voice must produce a non-empty storage folder name.")
    normalized = str(output_name or "").replace("\\", "/").strip("/")
    filename = normalized.rsplit("/", 1)[-1].strip()
    if normalized.startswith("runs/") and "/" in normalized:
        parent = normalized.rsplit("/", 1)[0]
        return f"{parent}/{voice_slug}/{filename}" if filename else f"{parent}/{voice_slug}/"
    return f"audio/{voice_slug}/{filename}" if filename else f"audio/{voice_slug}/"


async def _resolve_task_narrator_profile(
    *,
    entity_id: str,
    task_id: str,
    candidate_model: str,
    voice_instructions: str,
) -> dict[str, str | int]:
    """Create or reuse the one concrete narration profile for a Stickman task."""
    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.task import Task

    task_text = str(task_id or "").strip()
    entity_text = str(entity_id or "").strip()
    if not task_text or not entity_text:
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_TASK_REQUIRED,
            detail="Task-scoped narration requires a valid task ID and entity ID.",
        )

    async with async_session() as db:
        task = (
            await db.execute(
                select(Task)
                .where(Task.id == task_text, Task.entity_id == entity_text)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            raise _NarrationProfileError(
                code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_TASK_REQUIRED,
                detail="Task-scoped narration could not find its owning task.",
            )

        details = dict(task.details or {})
        existing = details.get(_TASK_NARRATOR_PROFILE_KEY)
        if existing is not None:
            return _validated_task_narrator_profile(existing)

        model = str(candidate_model or "").strip()
        provider = _task_narrator_model_provider(model)
        voices = _TASK_NARRATOR_CONCRETE_VOICE_CHOICES.get(provider, ())
        if not model or not voices:
            raise _NarrationProfileError(
                code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_UNSUPPORTED,
                detail=(
                    f"{model or 'The selected model'} cannot select a concrete task-scoped narrator. "
                    "Choose a supported Google or OpenAI TTS model."
                ),
            )

        profile: dict[str, str | int] = {
            "version": 1,
            "provider": provider,
            "model": model,
            "voice": secrets.choice(voices),
            "voice_instructions": str(voice_instructions or "").strip(),
        }
        details[_TASK_NARRATOR_PROFILE_KEY] = profile
        task.details = details
        await db.commit()
        return profile


async def _resolve_workspace_narrator_profile(
    *,
    entity_id: str,
    workspace_id: str,
    candidate_model: str,
    voice_instructions: str,
    preferred_voice: str = "",
) -> dict[str, str | int]:
    """Create or reuse one concrete narrator profile for a Workspace.

    Unlike ``random_per_task``, this profile lives in ``Workspace.settings``.
    Every future Stickman task in that Workspace therefore receives the same
    provider, model, voice, and delivery direction until an operator edits the
    Workspace setting explicitly.
    """

    from sqlalchemy import select

    from packages.core.database import async_session
    from packages.core.models.workspace import Workspace

    workspace_text = str(workspace_id or "").strip()
    entity_text = str(entity_id or "").strip()
    if not workspace_text or not entity_text:
        raise _NarrationProfileError(
            code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_WORKSPACE_REQUIRED,
            detail="Workspace-scoped narration requires a valid workspace ID and entity ID.",
        )

    async with async_session() as db:
        workspace = (
            await db.execute(
                select(Workspace)
                .where(
                    Workspace.id == workspace_text,
                    Workspace.entity_id == entity_text,
                    Workspace.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if workspace is None:
            raise _NarrationProfileError(
                code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_WORKSPACE_REQUIRED,
                detail="Workspace-scoped narration could not find its owning workspace.",
            )

        settings = dict(workspace.settings or {})
        existing = settings.get(_WORKSPACE_NARRATOR_PROFILE_KEY)
        if existing is not None:
            return _validated_task_narrator_profile(existing)

        model = str(candidate_model or "").strip()
        provider = _task_narrator_model_provider(model)
        voices = _TASK_NARRATOR_CONCRETE_VOICE_CHOICES.get(provider, ())
        if not model or not voices:
            raise _NarrationProfileError(
                code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_UNSUPPORTED,
                detail=(
                    f"{model or 'The selected model'} cannot create a concrete "
                    "workspace-scoped narrator. Choose a supported Google or OpenAI TTS model."
                ),
            )

        requested_voice = str(preferred_voice or "").strip()
        voice = requested_voice or _WORKSPACE_NARRATOR_DEFAULT_VOICE.get(provider, "")
        if voice not in voices:
            raise _NarrationProfileError(
                code=AudioGenerationErrorCode.NARRATION_VOICE_PROFILE_INVALID,
                detail=f"Voice {voice or '(empty)'} is not supported by {model}.",
            )

        profile: dict[str, str | int] = {
            "version": 1,
            "provider": provider,
            "model": model,
            "voice": voice,
            "voice_instructions": str(voice_instructions or "").strip(),
        }
        settings[_WORKSPACE_NARRATOR_PROFILE_KEY] = profile
        workspace.settings = settings
        await db.commit()
        return profile


def _is_speech_response_audio_model(model: str) -> bool:
    """Return true for audio-output chat models that speak their response."""

    lowered = (model or "").strip().lower()
    return lowered.startswith("openai/gpt-audio") or lowered.startswith("openai/gpt-4o-audio")


def _audio_error_payload(
    *,
    code: AudioGenerationErrorCode,
    error: str,
    purpose: AudioGenerationPurpose,
    provider: AudioGenerationProvider = AudioGenerationProvider.UNKNOWN,
    retryable: bool = False,
    model: str = "",
    provider_status: int | None = None,
    attempts: int | None = None,
    format_related: bool | None = None,
    retry_advice: str = "",
    role: AudioGenerationRole | None = None,
) -> AudioGenerationErrorResult:
    payload: AudioGenerationErrorResult = {
        "kind": GenerateFileKind.AUDIO,
        "status": AudioGenerationStatus.ERROR,
        "code": code,
        "error": error,
        "purpose": purpose,
        "provider": provider,
        "retryable": retryable,
        "audio_generated": False,
    }
    if model:
        payload["model"] = model
    if provider_status is not None:
        payload["provider_status"] = provider_status
    if attempts is not None:
        payload["attempts"] = attempts
    if format_related is not None:
        payload["format_related"] = format_related
    if retry_advice:
        payload["retry_advice"] = retry_advice
    if role is not None:
        payload["role"] = role
    return payload


def _unsupported_nonvoice_audio_payload(
    model: str,
    purpose: AudioGenerationPurpose,
    role: AudioGenerationRole,
) -> AudioGenerationErrorResult:
    return _audio_error_payload(
        code=AudioGenerationErrorCode.UNSUPPORTED_NONVOICE_AUDIO_MODEL,
        error=(
            f"{model} is routed as a speech/conversational audio model here, "
            "not a reliable music, ambience, Foley, or SFX generator. "
            "Use a dedicated sound/music model or an approved uploaded/library stem; "
            "do not mix this output as non-voice audio."
        ),
        purpose=purpose,
        provider=AudioGenerationProvider.OPENROUTER,
        model=model,
        role=role,
    )


async def _save_generated_audio_bytes(
    *,
    entity_id: str,
    user_id: str,
    prompt: str,
    model: str,
    purpose: str,
    audio_bytes: bytes,
    audio_format: str,
    is_byok: bool,
    voice: str = "",
    voice_instructions: str = "",
    narration_profile: dict[str, str | int] | None = None,
    language: str = DEFAULT_WORKSPACE_AUDIO_LANGUAGE,
    output_name: str = "",
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Persist generated audio and register it as a Knowledge document."""
    import base64

    validate_generated_audio_bytes(audio_bytes, audio_format)
    mime = _audio_format_to_mime(audio_format)
    if not entity_id:
        return f"data:{mime};base64,{base64.b64encode(audio_bytes).decode('ascii')}"

    ext = _audio_format_to_ext(audio_format)

    from packages.core.ai.runtime.file_actions import runtime_write_entity_file_atomic
    from packages.core.services.entity_fs import get_entity_root
    from packages.core.services.generated_media_naming import (
        build_generated_media_target,
        resolve_workspace_artifact_base_dir,
        scope_workspace_artifact_path,
        workspace_artifact_default_dir,
    )

    entity_root = get_entity_root(entity_id)
    workspace_base_dir = await resolve_workspace_artifact_base_dir(
        entity_id=entity_id,
        workspace_id=workspace_id,
        task_id=task_id,
    )
    target = build_generated_media_target(
        prompt=prompt,
        desired_name=scope_workspace_artifact_path(
            output_name,
            workspace_base_dir,
            preserve_leaf_default=True,
        ),
        ext=ext,
        fallback="generated-audio",
        default_dir=workspace_artifact_default_dir(workspace_base_dir, WorkspaceArtifactDir.AUDIO.value),
        entity_root=entity_root,
    )
    filename = target.filename
    filepath = runtime_write_entity_file_atomic(
        entity_id,
        target.rel_path,
        audio_bytes,
        expected_size=len(audio_bytes),
        allow_empty=False,
    )

    audio_url = f"/api/v1/fs/{entity_id}/{target.rel_path}"
    logger.info("Generated audio saved: %s (%d bytes)", filepath, len(audio_bytes))

    await _bill_media(
        entity_id=entity_id,
        user_id=user_id,
        kind="audio",
        model=model,
        cost_usd=_estimate_audio_cost(model, purpose=purpose),
        units=1,
        byok=is_byok,
    )

    try:
        from packages.core.database import create_worker_session
        from packages.core.services.document_service import upsert_document_by_fs_path
        from packages.core.services.document_metadata import merge_document_metadata

        factory = create_worker_session()
        async with factory() as db:
            folder_id = await _ensure_generated_media_document_folder(
                entity_id=entity_id,
                workspace_id=workspace_id,
                rel_path=target.rel_path,
                rel_dir=target.rel_dir,
            )
            doc = await upsert_document_by_fs_path(
                db,
                entity_id,
                name=filename,
                fs_path=target.rel_path,
                file_size=len(audio_bytes),
                file_type=ext.lstrip("."),
                mime_type=mime,
                source="ai_generated",
                created_by=user_id or None,
                folder_id=folder_id,
            )
            doc.source = "ai_generated"
            if user_id:
                doc.created_by = user_id
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                artifact={"role": "final", "storage_scope": "artifact"},
                origin={
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "tool_name": "generate_audio",
                },
                generation={
                    "prompt": prompt,
                    "model": model,
                    "purpose": purpose,
                    "format": audio_format,
                    "voice": voice or None,
                    "voice_instructions": voice_instructions or None,
                    "language": normalize_workspace_audio_language(language),
                    "narration_profile": narration_profile,
                },
            )
            await db.commit()
            if workspace_id:
                from packages.core.services.knowledge_sync import bind_document_to_workspace

                await bind_document_to_workspace(
                    entity_id=entity_id,
                    document_id=doc.id,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tool_name="generate_audio",
                )
    except Exception:
        logger.warning("Failed to register generated audio as document", exc_info=True)

    return audio_url


def _directed_speech_prompt(prompt: str, voice_instructions: str) -> str:
    if not voice_instructions:
        return prompt
    return (
        f"{voice_instructions}\n\n"
        "Read aloud exactly the script below. Do not speak these directions, "
        "and do not add, remove, or paraphrase words.\n\n"
        f"SCRIPT:\n{prompt}"
    )


def _raw_audio_response_bytes(response: Any, *, operation: str) -> bytes:
    audio_bytes = bytes(getattr(response, "content", b"") or b"")
    if not audio_bytes:
        raise RuntimeError(f"{operation} response did not include audio data.")
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type == "application/json" or content_type.startswith("text/"):
        detail = str(getattr(response, "text", "") or "")[:500]
        raise RuntimeError(
            f"{operation} returned {content_type or 'non-audio content'} instead of audio"
            f"{f': {detail}' if detail else '.'}"
        )
    return audio_bytes


def _vercel_speech_endpoint(base_url: str) -> str:
    """Build the Vercel AI Gateway Speech REST endpoint from its chat base URL."""

    base = str(base_url or "https://ai-gateway.vercel.sh/v1").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if base.endswith("/v4/ai"):
        return f"{base}/speech-model"
    return f"{base}/v4/ai/speech-model"


def _vercel_speech_model_supported(model: str) -> bool:
    """Return whether the selected catalog model is in Gateway's Speech catalog.

    Vercel's Speech protocol is separate from its language-model route. Keep
    unknown/provider-native TTS models on the existing OpenRouter or native
    provider path instead of sending them to ``/speech-model``.
    """

    model_id = str(model or "").strip().lower()
    return model_id in {
        "openai/tts-1",
        "openai/tts-1-hd",
        "xai/grok-tts",
    } or model_id.startswith("fish-audio/")


def _speech_model_supports_instructions(model: str) -> bool:
    """Return whether the speech model accepts delivery instructions."""

    native_model = str(model or "").strip().lower().split("/", 1)[-1]
    return native_model not in {"tts-1", "tts-1-hd"}


async def _vercel_speech_bytes(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
    voice_instructions: str = "",
    auth_method: str = "api-key",
) -> bytes:
    """Generate speech through Vercel AI Gateway's beta Speech protocol."""

    import base64
    import binascii
    import httpx

    output_format = _normalize_audio_format(audio_format) or "mp3"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "ai-gateway-protocol-version": "0.0.1",
        "ai-gateway-auth-method": "oidc" if auth_method == "oidc" else "api-key",
        "ai-speech-model-specification-version": "4",
        # Keep the catalog id intact, matching the chat gateway route.
        "ai-model-id": str(model or "").strip(),
    }
    payload: dict[str, Any] = {
        "text": prompt,
        "voice": voice,
        "outputFormat": output_format,
    }
    if voice_instructions and _speech_model_supports_instructions(model):
        payload["instructions"] = voice_instructions

    async with httpx.AsyncClient(timeout=180.0) as client:
        begin_speech_provider_request()
        response = await client.post(
            _vercel_speech_endpoint(base_url),
            headers=headers,
            json=payload,
        )
    if response.status_code >= 300:
        raise RuntimeError(
            f"Vercel AI Gateway speech generation failed ({response.status_code}): "
            f"{str(response.text or '')[:500]}"
        )
    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError("Vercel AI Gateway speech response was not valid JSON.") from exc
    encoded = body.get("audio") if isinstance(body, dict) else None
    if not isinstance(encoded, str) or not encoded.strip():
        raise RuntimeError("Vercel AI Gateway speech response did not include audio data.")
    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("Vercel AI Gateway speech response included invalid base64 audio data.") from exc
    if not audio_bytes:
        raise RuntimeError("Vercel AI Gateway speech response decoded to empty audio data.")
    return audio_bytes


class _AudioProviderUnavailable(RuntimeError):
    """A transient provider failure exhausted the tool's bounded retries."""

    def __init__(
        self,
        *,
        provider: AudioGenerationProvider | str,
        status_code: int | None,
        attempts: int,
        detail: str,
    ) -> None:
        self.provider = AudioGenerationProvider.coerce(provider)
        self.status_code = status_code
        self.attempts = attempts
        status_label = str(status_code) if status_code is not None else "network error"
        detail_suffix = f" Provider detail: {detail}" if detail else ""
        super().__init__(
            f"{self.provider.value} speech generation was temporarily unavailable after "
            f"{attempts} attempts ({status_label}). No audio bytes were generated; "
            f"changing the requested MP3/WAV format will not help.{detail_suffix}"
        )


def _audio_provider_retry_delay(response: Any | None, attempt: int) -> float:
    """Return a short, bounded retry delay, honoring numeric Retry-After."""

    headers = getattr(response, "headers", {}) or {}
    raw_retry_after = str(headers.get("retry-after") or "").strip()
    if raw_retry_after:
        try:
            return max(0.0, min(float(raw_retry_after), 8.0))
        except ValueError:
            pass
    return min(float(2 ** (attempt - 1)), 4.0)


async def _openrouter_speech_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
    voice_instructions: str = "",
) -> bytes:
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "input": prompt,
        "response_format": audio_format,
    }
    if voice:
        payload["voice"] = voice
    if voice_instructions and model.lower().startswith("google/"):
        payload["input"] = _directed_speech_prompt(prompt, voice_instructions)
    elif (
        voice_instructions
        and model.lower().startswith("openai/")
        and _speech_model_supports_instructions(model)
    ):
        payload["instructions"] = voice_instructions
    max_attempts = 3
    async with httpx.AsyncClient(timeout=180.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                begin_speech_provider_request()
                resp = await client.post(
                    "https://openrouter.ai/api/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://manor.ai",
                        "X-Title": "Manor AI",
                    },
                    json=payload,
                )
            except httpx.RequestError as exc:
                if attempt >= max_attempts:
                    raise _AudioProviderUnavailable(
                        provider=AudioGenerationProvider.OPENROUTER,
                        status_code=None,
                        attempts=attempt,
                        detail=str(exc)[:500],
                    ) from exc
                logger.warning(
                    "OpenRouter speech request failed before a response; retrying model=%s attempt=%s/%s error_type=%s",
                    model,
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                )
                await asyncio.sleep(_audio_provider_retry_delay(None, attempt))
                continue

            if resp.status_code < 400:
                if resp.content:
                    return _raw_audio_response_bytes(resp, operation="OpenRouter speech")

                detail = "successful response did not include audio data"
                generation_id = str((getattr(resp, "headers", {}) or {}).get("x-generation-id") or "").strip()
                if generation_id:
                    detail = f"{detail} (generation_id={generation_id})"
                if attempt >= max_attempts:
                    raise _AudioProviderUnavailable(
                        provider=AudioGenerationProvider.OPENROUTER,
                        status_code=resp.status_code,
                        attempts=attempt,
                        detail=detail,
                    )
                logger.warning(
                    "OpenRouter speech provider returned no audio bytes; retrying model=%s status=%s attempt=%s/%s generation_id=%s",
                    model,
                    resp.status_code,
                    attempt,
                    max_attempts,
                    generation_id or "unknown",
                )
                await asyncio.sleep(_audio_provider_retry_delay(resp, attempt))
                continue

            detail = str(resp.text or "")[:500]
            retryable = resp.status_code in {408, 409, 425, 429} or resp.status_code >= 500
            if not retryable:
                raise RuntimeError(f"OpenRouter speech generation failed ({resp.status_code}): {detail}")
            if attempt >= max_attempts:
                raise _AudioProviderUnavailable(
                    provider=AudioGenerationProvider.OPENROUTER,
                    status_code=resp.status_code,
                    attempts=attempt,
                    detail=detail,
                )
            logger.warning(
                "OpenRouter speech provider returned a retryable response; retrying model=%s status=%s attempt=%s/%s",
                model,
                resp.status_code,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(_audio_provider_retry_delay(resp, attempt))

    raise AssertionError("OpenRouter speech retry loop exited unexpectedly")


class _OpenAICompatibleSpeechEndpointUnavailable(RuntimeError):
    """Signal that an OpenAI-compatible relay does not expose Audio Speech."""


class _OpenAICompatibleAudioProviderBlocker(RuntimeError):
    """Signal that an OpenAI-compatible provider returned unusable audio."""


_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES = 50 * 1024 * 1024
_OPENAI_COMPATIBLE_CHAT_AUDIO_JSON_OVERHEAD_BYTES = 1024 * 1024
_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES = (
    4 * ((_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES + 2) // 3)
    + _OPENAI_COMPATIBLE_CHAT_AUDIO_JSON_OVERHEAD_BYTES
)


def _speech_404_means_endpoint_unavailable(response_text: str, *, model: str = "") -> bool:
    """Distinguish a missing speech route from a provider/model-level 404."""
    raw_text = str(response_text or "").strip()
    lowered_text = raw_text.lower()

    try:
        parsed = json.loads(raw_text)
    except (TypeError, ValueError):
        parsed = None

    def _json_strings(value: Any) -> list[str]:
        if isinstance(value, dict):
            strings: list[str] = []
            for key, child in value.items():
                strings.append(str(key))
                strings.extend(_json_strings(child))
            return strings
        if isinstance(value, list):
            strings = []
            for child in value:
                strings.extend(_json_strings(child))
            return strings
        if isinstance(value, str):
            return [value]
        return []

    searchable = " ".join(_json_strings(parsed)).lower() if parsed is not None else lowered_text
    model_markers = (
        "model_not_found",
        "model not found",
        "model does not exist",
        "model is not available",
        "model unavailable",
        "unknown model",
        "unsupported model",
        "invalid model",
    )
    if any(marker in searchable for marker in model_markers):
        return False
    if "model" in searchable and any(
        marker in searchable
        for marker in ("does not exist", "not found", "not available", "unavailable", "unsupported", "invalid")
    ):
        return False
    native_model = str(model or "").strip().lower()
    if native_model and native_model in searchable and any(
        marker in searchable for marker in ("does not exist", "not found", "not available", "unavailable")
    ):
        return False

    route_markers = (
        "route_not_found",
        "route not found",
        "no route",
        "endpoint_not_found",
        "endpoint not found",
        "unknown endpoint",
        "unsupported endpoint",
        "path not found",
        "cannot post",
        "method not allowed for",
        "/audio/speech not found",
    )
    if any(marker in searchable for marker in route_markers):
        return True

    plain_page_markers = ("not found", "404 not found", "404 page not found", "page not found")
    if parsed is not None:
        json_values = [value.strip().lower() for value in _json_strings(parsed)]
        return any(value in plain_page_markers for value in json_values)
    return lowered_text in plain_page_markers or (
        "404" in lowered_text and "not found" in lowered_text
    )


async def _openai_compatible_speech_bytes(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
    voice_instructions: str = "",
) -> bytes:
    import httpx

    native_model = _native_media_model(model, kind="audio", provider="openai")
    payload: dict[str, Any] = {
        "model": native_model,
        "input": prompt,
        "response_format": audio_format,
    }
    if voice:
        payload["voice"] = voice
    if voice_instructions and _speech_model_supports_instructions(model):
        payload["instructions"] = voice_instructions
    async with httpx.AsyncClient(timeout=180.0) as client:
        begin_speech_provider_request()
        resp = await client.post(
            f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        endpoint_unavailable = resp.status_code in {405, 501} or (
            resp.status_code == 404
            and _speech_404_means_endpoint_unavailable(resp.text, model=native_model)
        )
        if endpoint_unavailable:
            raise _OpenAICompatibleSpeechEndpointUnavailable(
                f"OpenAI-compatible speech generation failed ({resp.status_code}): "
                f"{resp.text[:500]}"
            )
        if resp.status_code >= 300:
            raise RuntimeError(
                f"OpenAI-compatible speech generation failed ({resp.status_code}): "
                f"{resp.text[:500]}"
            )
        return _raw_audio_response_bytes(resp, operation="OpenAI-compatible speech")


def _is_openai_chat_audio_model(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized.startswith("gpt-4o-audio") or normalized.startswith("gpt-audio")


def _openai_compatible_json_object(response: Any, *, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise _OpenAICompatibleAudioProviderBlocker(
            f"OpenAI-compatible {operation} returned invalid JSON; expected a JSON object."
        ) from exc
    if not isinstance(payload, dict):
        raise _OpenAICompatibleAudioProviderBlocker(
            f"OpenAI-compatible {operation} returned invalid JSON; expected a JSON object."
        )
    return payload


def _openai_compatible_json_bytes_object(
    response_bytes: bytes,
    *,
    operation: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(response_bytes)
    except Exception as exc:
        raise _OpenAICompatibleAudioProviderBlocker(
            f"OpenAI-compatible {operation} returned invalid JSON; expected a JSON object."
        ) from exc
    if not isinstance(payload, dict):
        raise _OpenAICompatibleAudioProviderBlocker(
            f"OpenAI-compatible {operation} returned invalid JSON; expected a JSON object."
        )
    return payload


async def _read_bounded_openai_compatible_chat_audio_response(response: Any) -> bytes:
    headers = getattr(response, "headers", None) or {}
    content_length = headers.get("content-length") or headers.get("Content-Length")
    if content_length:
        try:
            declared_length = int(str(content_length).strip())
        except (TypeError, ValueError) as exc:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat audio response included an invalid Content-Length header."
            ) from exc
        if declared_length < 0:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat audio response included an invalid Content-Length header."
            )
        if declared_length > _MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat audio response Content-Length exceeded the "
                f"{_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES}-byte limit."
            )

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        next_size = len(body) + len(chunk)
        if next_size > _MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat audio response exceeded the "
                f"{_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_RESPONSE_BYTES}-byte limit while streaming."
            )
        body.extend(chunk)
    return bytes(body)


async def _discover_openai_compatible_chat_audio_model(
    *,
    client: Any,
    api_key: str,
    base_url: str,
    requested_model: str,
) -> str:
    endpoint = (base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    begin_speech_provider_request()
    resp = await client.get(f"{endpoint}/models", headers=headers)
    if resp.status_code >= 300:
        raise _OpenAICompatibleAudioProviderBlocker(
            f"OpenAI-compatible audio model discovery failed ({resp.status_code}): "
            f"{resp.text[:500]}"
        )

    payload = _openai_compatible_json_object(resp, operation="model discovery")
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible model discovery field 'data' must be a list of model objects."
        )
    if any(not isinstance(entry, dict) for entry in entries):
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible model discovery field 'data' must contain only objects."
        )
    advertised_models = [
        str(entry.get("id") or "").strip()
        for entry in entries
        if str(entry.get("id") or "").strip()
    ]
    advertised_by_name = {model_id.lower(): model_id for model_id in advertised_models}

    native_requested = _native_media_model(
        requested_model,
        kind="audio",
        provider="openai",
    )
    preferred_models = []
    if _is_openai_chat_audio_model(native_requested):
        preferred_models.append(native_requested)
    preferred_models.append("gpt-4o-audio-preview")
    for preferred_model in preferred_models:
        advertised_model = advertised_by_name.get(preferred_model.lower())
        if advertised_model:
            return advertised_model

    for advertised_model in advertised_models:
        if _is_openai_chat_audio_model(advertised_model):
            return advertised_model
    raise _OpenAICompatibleAudioProviderBlocker(
        "OpenAI-compatible provider did not advertise a GPT audio model."
    )


def _validate_openai_compatible_chat_audio_wav(audio_bytes: bytes) -> None:
    import io
    import wave

    byte_count = len(audio_bytes)
    if byte_count > _MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES:
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible chat audio exceeded the "
            f"{_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES}-byte limit."
        )
    if byte_count < 12:
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible chat audio is too short to be a WAV file."
        )
    if audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible chat audio is missing a RIFF/WAVE header."
        )

    declared_size = int.from_bytes(audio_bytes[4:8], "little") + 8
    if declared_size > byte_count:
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible chat audio contains a truncated RIFF/WAVE structure."
        )

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise _OpenAICompatibleAudioProviderBlocker(
                    "OpenAI-compatible chat audio must contain uncompressed PCM."
                )
            frame_count = wav_file.getnframes()
            channel_count = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            if frame_count <= 0:
                raise _OpenAICompatibleAudioProviderBlocker(
                    "OpenAI-compatible chat audio contains zero audio frames."
                )
            pcm_bytes = wav_file.readframes(frame_count)
    except _OpenAICompatibleAudioProviderBlocker:
        raise
    except Exception as exc:
        raise _OpenAICompatibleAudioProviderBlocker(
            f"OpenAI-compatible chat audio has an invalid RIFF/WAVE PCM structure: {exc}"
        ) from exc

    expected_pcm_bytes = frame_count * channel_count * sample_width
    if len(pcm_bytes) != expected_pcm_bytes:
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible chat audio contains truncated PCM frame data."
        )
    if sample_width == 1:
        has_signal = any(sample != 128 for sample in pcm_bytes)
    else:
        has_signal = any(
            int.from_bytes(pcm_bytes[offset : offset + sample_width], "little", signed=True) != 0
            for offset in range(0, len(pcm_bytes), sample_width)
        )
    if not has_signal:
        raise _OpenAICompatibleAudioProviderBlocker(
            "OpenAI-compatible chat audio contains only digital silence."
        )


async def _openai_compatible_chat_audio_bytes(
    *,
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
    voice_instructions: str = "",
    render_as_speech: bool = True,
) -> bytes:
    import base64
    import binascii
    import httpx

    endpoint = (base_url or "https://api.openai.com/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        chat_audio_model = await _discover_openai_compatible_chat_audio_model(
            client=client,
            api_key=api_key,
            base_url=endpoint,
            requested_model=model,
        )
        begin_speech_provider_request()
        async with client.stream(
            "POST",
            f"{endpoint}/chat/completions",
            headers=headers,
            json={
                "model": chat_audio_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            (
                                "Speak the user's script exactly as written. Do not introduce, "
                                "remove, paraphrase, explain, or comment on it. "
                                + (voice_instructions or "Use a natural, conversational delivery.")
                            )
                            if render_as_speech
                            else (
                                "Generate the requested audio asset. Follow the user's constraints "
                                "for music, ambience, Foley, sound effects, duration, and absence of speech."
                            )
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "modalities": ["text", "audio"],
                "audio": {"voice": voice or "alloy", "format": audio_format},
                "stream": False,
            },
        ) as resp:
            response_bytes = await _read_bounded_openai_compatible_chat_audio_response(resp)
            if resp.status_code >= 300:
                response_excerpt = response_bytes.decode("utf-8", errors="replace")[:500]
                raise _OpenAICompatibleAudioProviderBlocker(
                    f"OpenAI-compatible chat audio generation failed ({resp.status_code}): "
                    f"{response_excerpt}"
                )

        payload = _openai_compatible_json_bytes_object(
            response_bytes,
            operation="chat audio generation",
        )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response field 'choices' must be a non-empty list."
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response choice must be an object."
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response message must be an object."
            )
        if "audio" not in message:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response did not include audio data."
            )
        audio = message.get("audio")
        if not isinstance(audio, dict):
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response audio must be an object."
            )
        if "data" not in audio:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response did not include audio data."
            )
        audio_data = audio.get("data")
        if not isinstance(audio_data, str):
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response audio.data must be a string."
            )
        if not audio_data.strip():
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response did not include audio data."
            )
        max_base64_chars = 4 * ((_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES + 2) // 3)
        if len(audio_data) > max_base64_chars:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response base64 audio data exceeded the encoded limit "
                f"for {_MAX_OPENAI_COMPATIBLE_CHAT_AUDIO_BYTES} decoded bytes."
            )
        try:
            decoded = base64.b64decode(audio_data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response included invalid base64 audio data."
            ) from exc
        if not decoded:
            raise _OpenAICompatibleAudioProviderBlocker(
                "OpenAI-compatible chat response did not include audio data."
            )
        _validate_openai_compatible_chat_audio_wav(decoded)
        return decoded


async def _google_speech_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    voice: str,
    base_url: str = "",
    voice_instructions: str = "",
) -> bytes:
    """Generate speech with Gemini's native generateContent AUDIO API.

    Gemini TTS returns raw 24 kHz PCM in inlineData, so callers should store it
    as WAV for browser playback.
    """
    import base64
    import httpx

    native_model = _native_media_model(model, kind="audio", provider="google")
    endpoint = _native_media_base_url(
        "google",
        base_url or "https://generativelanguage.googleapis.com/v1beta",
    ).rstrip("/")
    voice_name = (voice or "Zephyr").strip() or "Zephyr"
    directed_prompt = _directed_speech_prompt(prompt, voice_instructions)
    payload = {
        "contents": [{"parts": [{"text": directed_prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name},
                },
            },
        },
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        begin_speech_provider_request()
        resp = await client.post(
            f"{endpoint}/models/{native_model}:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            msg = err.get("message", "") if isinstance(err, dict) else ""
            raise RuntimeError(f"Google speech generation failed ({resp.status_code}): {msg or resp.text[:500]}")
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            b64_data = inline.get("data") or ""
            if b64_data:
                return base64.b64decode(b64_data)
    raise RuntimeError("Google speech response did not include audio data.")


def _google_music_audio_block(data: Any) -> tuple[str, str]:
    """Extract the final audio block from a Gemini Interactions response."""
    if not isinstance(data, dict):
        return "", ""
    direct = data.get("output_audio") or data.get("outputAudio")
    if isinstance(direct, dict) and direct.get("data"):
        return str(direct["data"]), str(direct.get("mime_type") or direct.get("mimeType") or "")
    steps = data.get("steps") or []
    if not isinstance(steps, list):
        return "", ""
    found_data = ""
    found_mime = ""
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "audio" or not block.get("data"):
                continue
            found_data = str(block["data"])
            found_mime = str(block.get("mime_type") or block.get("mimeType") or "")
    return found_data, found_mime


async def _google_music_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    audio_format: str = "mp3",
    base_url: str = "",
) -> tuple[bytes, str]:
    """Generate music through the native Gemini Interactions API for Lyria 3."""
    import base64
    import binascii
    import httpx

    native_model = _native_media_model(model, kind="audio", provider="google")
    endpoint_base = _native_media_base_url(
        "google",
        base_url or "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")
    endpoint = endpoint_base if endpoint_base.endswith("/interactions") else f"{endpoint_base}/interactions"
    requested_format = _normalize_audio_format(audio_format) or "mp3"
    payload: dict[str, Any] = {
        "model": native_model,
        "input": prompt,
    }
    # Lyria 3 Clip is MP3-only. Pro accepts the audio response format for WAV.
    if "pro" in native_model.lower() and requested_format == "wav":
        payload["response_format"] = {"type": "audio"}

    async with httpx.AsyncClient(timeout=420.0) as client:
        resp = await client.post(
            endpoint,
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            message = err.get("message", "") if isinstance(err, dict) else ""
            raise RuntimeError(
                f"Google Lyria music generation failed ({resp.status_code}): {message or resp.text[:500]}"
            )
        encoded, mime_type = _google_music_audio_block(data)
        if not encoded:
            raise RuntimeError("Google Lyria response did not include audio data.")
        try:
            audio_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("Google Lyria returned invalid base64 audio data.") from exc
        if not audio_bytes:
            raise RuntimeError("Google Lyria returned an empty audio file.")
        normalized_mime = mime_type.lower()
        actual_format = "wav" if "wav" in normalized_mime else "mp3"
        if not normalized_mime and "pro" in native_model.lower() and requested_format == "wav":
            actual_format = "wav"
        return audio_bytes, actual_format


def _zyphra_audio_mime(audio_format: str) -> str:
    fmt = _normalize_audio_format(audio_format)
    if fmt in {"wav", "mp3", "ogg", "webm", "mp4", "aac"}:
        return _audio_format_to_mime(fmt)
    return "audio/mpeg"


async def _zyphra_speech_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
    base_url: str = "",
) -> bytes:
    """Generate speech through Zyphra's official Zonos TTS API."""
    import httpx

    native_model = _native_media_model(model, kind="audio", provider="zyphra")
    endpoint_base = (base_url or "https://api.zyphracloud.com/api/v1").strip().rstrip("/")
    legacy_api = "api.zyphra.com" in endpoint_base.lower()
    endpoint = (
        endpoint_base
        if endpoint_base.endswith("/audio/text-to-speech") or endpoint_base.endswith("/audio/speech")
        else f"{endpoint_base}/audio/{'text-to-speech' if legacy_api else 'speech'}"
    )
    payload: dict[str, Any] = (
        {
            "text": prompt,
            "model": native_model,
            "mime_type": _zyphra_audio_mime(audio_format),
        }
        if legacy_api
        else {
            "input": prompt,
            "model": model,
            "response_format": _normalize_audio_format(audio_format) or "mp3",
        }
    )
    selected_voice = (voice or "").strip()
    if selected_voice and selected_voice.lower() != "random":
        payload["default_voice_name" if legacy_api else "voice"] = selected_voice

    async with httpx.AsyncClient(timeout=180.0) as client:
        begin_speech_provider_request()
        resp = await client.post(
            endpoint,
            headers=(
                {"X-API-Key": api_key, "Content-Type": "application/json"}
                if legacy_api
                else {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            ),
            json=payload,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Zyphra speech generation failed ({resp.status_code}): {resp.text[:500]}")
        return _raw_audio_response_bytes(resp, operation="Zyphra speech")


async def _openrouter_audio_output_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
) -> bytes:
    import base64
    import httpx

    audio_config: dict[str, Any] = {"format": audio_format}
    if voice:
        audio_config["voice"] = voice
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["text", "audio"],
        "audio": audio_config,
        "stream": True,
    }
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=360.0) as client:
        begin_speech_provider_request()
        async with client.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://manor.ai",
                "X-Title": "Manor AI",
            },
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenRouter audio generation failed ({resp.status_code}): {body[:500]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_text = line[5:].strip()
                if not data_text or data_text == "[DONE]":
                    continue
                try:
                    data = json.loads(data_text)
                except json.JSONDecodeError:
                    continue
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    audio = delta.get("audio") or {}
                    if isinstance(audio, dict) and audio.get("data"):
                        chunks.append(str(audio["data"]))
    if not chunks:
        raise RuntimeError("OpenRouter returned no audio chunks")
    return base64.b64decode("".join(chunks))


def _audio_duration_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return max(0.1, min(duration, 600.0))


def _duration_instruction(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return ""
    return f" Target duration: exactly {duration_seconds:g} seconds."


def _audio_prompt_for_purpose(prompt: str, purpose: str, duration_seconds: float | None = None) -> str:
    purpose_key = (purpose or "").strip().lower()
    clean_prompt = " ".join(str(prompt or "").split())
    duration_instruction = _duration_instruction(duration_seconds)
    if purpose_key in {"sfx", "sound_effect", "sound-effect", "foley"}:
        return (
            "Generate a standalone cinematic sound effect only. "
            "No music, no melody, no rhythm, no beat, no singing, no vocals, "
            "no spoken words, no instruments. "
            "Make it a dry, direct, realistic production SFX asset suitable for film editing. "
            f"{duration_instruction} "
            f"Sound: {clean_prompt}"
        )
    if purpose_key in {"transition"}:
        return (
            "Generate a short transition sound effect only. "
            "No music, no melody, no rhythm, no beat, no speech, no narration, "
            "no spoken words, no vocals, no instruments. "
            "Make it a concise edit accent, whoosh, hit, or stinger as requested. "
            f"{duration_instruction} "
            f"Sound: {clean_prompt}"
        )
    if purpose_key in {"ambience", "ambient", "soundscape", "background", "background_bed", "bed"}:
        return (
            "Generate one continuous environmental soundscape bed for video post-production. "
            "No music, no melody, no rhythm, no beat, no speech, no narration, "
            "no spoken words, no vocals, no lyrics, no instruments. "
            "Do not make isolated random SFX hits; blend action, movement, crowd, weather, "
            "room tone, and distant impacts into a coherent scene-length background bed. "
            "Keep it loop-friendly and cohesive, with natural foreground/background depth. "
            f"{duration_instruction} "
            f"Soundscape: {clean_prompt}"
        )
    if purpose_key in {"music", "score", "bgm"} and duration_instruction:
        return f"{clean_prompt}{duration_instruction}"
    return clean_prompt


async def _generate_audio_handler(
    entity_id: str = "",
    user_id: str = "",
    _deliver_audio: Callable[..., Awaitable[str]] | None = None,
    **kwargs: Any,
) -> str:
    """Generate an audio file through the selected managed/native route."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    user_id = await _resolve_media_task_user_id(
        _media_context_user_id(user_id, runtime_context.user_id),
        entity_id,
        kwargs.get("task_id") or runtime_context.task_id,
    )
    purpose = normalize_audio_generation_purpose(kwargs.get("purpose"))
    workspace_id = str(kwargs.get("workspace_id") or runtime_context.workspace_id or "").strip()
    explicit_language = str(kwargs.get("language") or "").strip()
    audio_language = normalize_workspace_audio_language(explicit_language) if explicit_language else ""
    if not audio_language and workspace_id:
        audio_language = await _workspace_default_audio_language(
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
    if not audio_language:
        audio_language = DEFAULT_WORKSPACE_AUDIO_LANGUAGE
    prompt = str(kwargs.get("prompt") or "").strip()
    if not prompt:
        return json.dumps(
            _audio_error_payload(
                code=AudioGenerationErrorCode.INVALID_REQUEST,
                error="prompt is required",
                purpose=purpose,
            )
        )
    voice_instructions = str(
        kwargs.get("voice_instructions") or kwargs.get("instructions") or ""
    ).strip()
    duration_seconds = _audio_duration_seconds(kwargs.get("duration_seconds") or kwargs.get("duration"))
    output_name = str(kwargs.get("name") or kwargs.get("output_name") or kwargs.get("filename") or "").strip()
    model, role = await _resolve_user_audio_model(user_id, entity_id, purpose=purpose)
    if kwargs.get("model"):
        model = str(kwargs["model"]).strip()
    if not model:
        return json.dumps(
            _audio_error_payload(
                code=AudioGenerationErrorCode.AUDIO_PROVIDER_UNAVAILABLE,
                error=(
                    "Managed music and sound-effect generation is unavailable because "
                    "Vercel AI Gateway does not currently publish a compatible model."
                ),
                purpose=purpose,
                model="",
                role=role,
            ),
            ensure_ascii=False,
        )
    narration_voice_mode = str(kwargs.get("narration_voice_mode") or "").strip()
    if purpose == AudioGenerationPurpose.NARRATION and workspace_id:
        workspace_studio_profile = await _workspace_stickman_studio_profile(
            entity_id=entity_id,
            workspace_id=workspace_id,
        )
        if (
            str(workspace_studio_profile.get("narration_voice_mode") or "").strip()
            == NARRATION_VOICE_MODE_FIXED_PER_WORKSPACE
        ):
            narration_voice_mode = NARRATION_VOICE_MODE_FIXED_PER_WORKSPACE
    narration_profile: dict[str, str | int] | None = None
    if narration_voice_mode:
        if narration_voice_mode not in {
            NARRATION_VOICE_MODE_RANDOM_PER_TASK,
            NARRATION_VOICE_MODE_FIXED_PER_WORKSPACE,
        }:
            return json.dumps(
                _audio_error_payload(
                    code=AudioGenerationErrorCode.INVALID_REQUEST,
                    error=f"Unsupported narration_voice_mode: {narration_voice_mode}",
                    purpose=purpose,
                    model=model,
                    role=role,
                ),
                ensure_ascii=False,
            )
        if purpose != AudioGenerationPurpose.NARRATION or role != AudioGenerationRole.VOICE:
            return json.dumps(
                _audio_error_payload(
                    code=AudioGenerationErrorCode.INVALID_REQUEST,
                    error=f"narration_voice_mode={narration_voice_mode} requires purpose=narration.",
                    purpose=purpose,
                    model=model,
                    role=role,
                ),
                ensure_ascii=False,
            )
        try:
            if narration_voice_mode == NARRATION_VOICE_MODE_FIXED_PER_WORKSPACE:
                narration_profile = await _resolve_workspace_narrator_profile(
                    entity_id=entity_id,
                    workspace_id=workspace_id,
                    candidate_model=model,
                    voice_instructions=voice_instructions,
                    preferred_voice=str(kwargs.get("voice") or ""),
                )
            else:
                narration_profile = await _resolve_task_narrator_profile(
                    entity_id=entity_id,
                    task_id=str(runtime_context.task_id or kwargs.get("task_id") or ""),
                    candidate_model=model,
                    voice_instructions=voice_instructions,
                )
        except _NarrationProfileError as exc:
            return json.dumps(
                _audio_error_payload(
                    code=exc.code,
                    error=str(exc),
                    purpose=purpose,
                    provider=AudioGenerationProvider.coerce(_catalog_provider(model)),
                    model=model,
                    role=role,
                ),
                ensure_ascii=False,
            )
        model = str(narration_profile["model"])
        voice_instructions = str(narration_profile["voice_instructions"])
    if purpose in {
        AudioGenerationPurpose.SPEECH,
        AudioGenerationPurpose.DIALOGUE,
        AudioGenerationPurpose.NARRATION,
    }:
        voice_instructions = _voice_instructions_with_language(
            voice_instructions,
            audio_language,
        )
    provider = _catalog_provider(model)
    requested_audio_format = str(kwargs.get("response_format") or kwargs.get("format") or "")
    request_format, storage_format = _openrouter_audio_formats(
        model,
        role,
        requested_audio_format,
    )
    call_voice = None
    if kwargs.get("_call_voice_profile"):
        # Chat calls pass a provider-neutral profile because the effective
        # media model can differ from the preliminary account selection. Map
        # it only after the final model (including narration overrides) is set.
        from packages.core.services.voice.profiles import speech_voice

        call_voice = speech_voice(kwargs["_call_voice_profile"], model)
    voice = str(
        narration_profile["voice"]
        if narration_profile is not None
        else kwargs.get("voice") or call_voice or _default_openrouter_voice(model)
    ).strip()
    native_google_music = bool(
        role == AudioGenerationRole.AUDIO
        and provider == "google"
        and "lyria-3-" in model.lower()
    )
    if native_google_music:
        requested_native_format = _normalize_audio_format(requested_audio_format)
        request_format = "wav" if "pro" in model.lower() and requested_native_format == "wav" else "mp3"
        storage_format = request_format

    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(
        user_id,
        entity_id,
        role=role,
    )
    # A Primary chat key must not silently override an Official media
    # selection. Media BYOK is role-scoped: only an explicitly configured
    # Voice/Music key may bypass Manor's managed Vercel route. This keeps the
    # Account source badge truthful and prevents a custom Primary relay from
    # hijacking TTS requests.
    native_voice_provider = ""
    native_music_provider = ""
    native_openai_output_audio = bool(
        role in {AudioGenerationRole.AUDIO, AudioGenerationRole.SFX}
        and provider == "openai"
        and api_key
        and not _is_openrouter_base_url(base_url_override)
        and (not api_key.startswith("sk-or-") or (is_byok and bool(base_url_override)))
    )
    managed_vercel_voice = False
    if (
        role == AudioGenerationRole.VOICE
        and provider in {"openai", "google"}
        and not is_byok
        and os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud"
    ):
        try:
            official_route = await _resolve_official_model_route(
                model,
                vercel_reason="media.voice.vercel_gateway_key",
                openrouter_reason="media.voice.openrouter_fallback_key",
                gateway_provider=(
                    None if _vercel_speech_model_supported(model) else "openrouter"
                ),
            )
        except Exception:
            logger.debug("Managed OpenAI TTS route lookup failed", exc_info=True)
            official_route = None
        if official_route and official_route.api_key:
            api_key = official_route.api_key
            base_url_override = official_route.base_url
            managed_vercel_voice = official_route.provider == "vercel"
            vercel_auth_method = (
                "oidc"
                if str(getattr(official_route, "source_detail", "")).strip()
                == "VERCEL_OIDC_TOKEN"
                else "api-key"
            )
        else:
            vercel_auth_method = "api-key"
    native_openai_voice = bool(
        role == AudioGenerationRole.VOICE
        and provider == "openai"
        and not managed_vercel_voice
        and api_key
        and not _is_openrouter_base_url(base_url_override)
        and (
            not api_key.startswith("sk-or-")
            or (
                is_byok
                and bool(base_url_override)
            )
        )
    )
    if role == AudioGenerationRole.VOICE and provider in {"google", "zyphra"}:
        if is_byok:
            if not _is_native_key_for_provider(api_key, provider):
                provider_label = "Google/Gemini" if provider == "google" else "Zyphra"
                return json.dumps(
                    _audio_error_payload(
                        code=AudioGenerationErrorCode.PROVIDER_KEY_REQUIRED,
                        error=(
                            f"The selected {provider_label} TTS model requires a native "
                            f"{provider_label} API key. Save a matching key for Text-to-Speech, "
                            "or choose a matching model."
                        ),
                        purpose=purpose,
                        provider=AudioGenerationProvider.coerce(provider),
                        model=model,
                    )
                )
            native_voice_provider = provider
        else:
            native_key, native_base_url = await _platform_native_media_credential_async(provider)
            if native_key:
                api_key = native_key
                base_url_override = native_base_url or base_url_override
                native_voice_provider = provider

    if native_google_music:
        if is_byok:
            if not _is_native_key_for_provider(api_key, "google"):
                return json.dumps(
                    _audio_error_payload(
                        code=AudioGenerationErrorCode.NATIVE_MUSIC_KEY_REQUIRED,
                        error=(
                            "The selected Google Lyria music model requires a native Google/Gemini API key. "
                            "Save a matching key for Music & Score or Primary, or choose another music model."
                        ),
                        purpose=purpose,
                        provider=AudioGenerationProvider.GOOGLE,
                        model=model,
                    )
                )
            native_music_provider = "google"
        else:
            native_key, native_base_url = await _platform_native_media_credential_async("google")
            if native_key:
                api_key = native_key
                base_url_override = native_base_url or base_url_override
                native_music_provider = "google"
        if not native_music_provider:
            return json.dumps(
                _audio_error_payload(
                    code=AudioGenerationErrorCode.NATIVE_MUSIC_KEY_REQUIRED,
                    error=(
                        "Google Lyria music generation requires a native Google/Gemini API key. "
                        "Configure one for Music & Score or Primary."
                    ),
                    purpose=purpose,
                    provider=AudioGenerationProvider.GOOGLE,
                    model=model,
                )
            )

    if (
        not native_voice_provider
        and not native_openai_voice
        and not native_openai_output_audio
        and not managed_vercel_voice
        and not native_music_provider
    ):
        if not api_key or not api_key.startswith("sk-or-"):
            env_openrouter_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
            if env_openrouter_key:
                api_key = env_openrouter_key
                is_byok = False
        if not api_key or not api_key.startswith("sk-or-"):
            return json.dumps(
                _audio_error_payload(
                    code=AudioGenerationErrorCode.PROVIDER_KEY_REQUIRED,
                    error="Self-hosted audio generation requires a matching provider API key.",
                    purpose=purpose,
                    provider=AudioGenerationProvider.OPENROUTER,
                    model=model,
                )
            )
    generation_provider = AudioGenerationProvider.OPENROUTER
    if native_music_provider:
        generation_provider = AudioGenerationProvider.coerce(native_music_provider)
    elif native_voice_provider:
        generation_provider = AudioGenerationProvider.coerce(native_voice_provider)
    elif native_openai_voice:
        generation_provider = AudioGenerationProvider.OPENAI
    elif native_openai_output_audio:
        generation_provider = AudioGenerationProvider.OPENAI
    elif managed_vercel_voice:
        # Vercel is the transport gateway; retain the selected speech model's
        # provider in the artifact metadata for compatibility with native routes.
        generation_provider = AudioGenerationProvider.coerce(provider)
    if entity_id and not is_byok:
        await runtime_assert_credit_available(
            entity_id,
            source=RUNTIME_GENERATE_AUDIO_TOOL_SOURCE,
        )

    try:
        if native_openai_output_audio:
            request_format = "wav"
            storage_format = "wav"
            audio_bytes = await _openai_compatible_chat_audio_bytes(
                api_key=api_key,
                base_url=base_url_override,
                model=model,
                prompt=_audio_prompt_for_purpose(prompt, purpose, duration_seconds),
                voice=voice or "alloy",
                audio_format="wav",
                voice_instructions=voice_instructions,
                render_as_speech=False,
            )
        elif native_music_provider == "google":
            lyria_prompt = _audio_prompt_for_purpose(
                prompt,
                purpose,
                duration_seconds if "pro" in model.lower() else None,
            )
            audio_bytes, storage_format = await _google_music_bytes(
                api_key=api_key,
                model=model,
                prompt=lyria_prompt,
                audio_format=request_format,
                base_url=base_url_override,
            )
            request_format = storage_format
        elif native_voice_provider == "google":
            request_format = "pcm"
            storage_format = "wav"
            audio_bytes = await _google_speech_bytes(
                api_key=api_key,
                model=model,
                prompt=prompt,
                voice=voice,
                voice_instructions=voice_instructions,
                base_url=base_url_override,
            )
            audio_bytes = _wav_from_pcm16(audio_bytes)
        elif native_voice_provider == "zyphra":
            if request_format in {"pcm", "pcm16"}:
                request_format = "wav"
                storage_format = "wav"
            audio_bytes = await _zyphra_speech_bytes(
                api_key=api_key,
                model=model,
                prompt=prompt,
                voice=voice,
                audio_format=request_format,
                base_url=base_url_override,
            )
        elif native_openai_voice:
            openai_audio_kwargs = {
                "api_key": api_key,
                "base_url": base_url_override,
                "model": model,
                "prompt": prompt,
                "voice": voice,
                "voice_instructions": voice_instructions,
                "audio_format": request_format,
            }
            try:
                audio_bytes = await _openai_compatible_speech_bytes(**openai_audio_kwargs)
            except _OpenAICompatibleSpeechEndpointUnavailable as exc:
                if narration_profile is not None:
                    raise _OpenAICompatibleAudioProviderBlocker(str(exc)) from exc
                chat_audio_kwargs = {**openai_audio_kwargs, "audio_format": "wav"}
                audio_bytes = await _openai_compatible_chat_audio_bytes(**chat_audio_kwargs)
                _validate_openai_compatible_chat_audio_wav(audio_bytes)
                request_format = "wav"
                storage_format = "wav"
            if request_format == "pcm" and storage_format == "wav":
                audio_bytes = _wav_from_pcm16(audio_bytes)
        elif managed_vercel_voice:
            try:
                audio_bytes = await _vercel_speech_bytes(
                    api_key=api_key,
                    base_url=base_url_override,
                    model=model,
                    prompt=prompt,
                    voice=voice,
                    voice_instructions=voice_instructions,
                    audio_format=request_format,
                    auth_method=vercel_auth_method,
                )
                if request_format == "pcm" and storage_format == "wav":
                    audio_bytes = _wav_from_pcm16(audio_bytes)
            except Exception as vercel_exc:
                logger.warning(
                    "Managed OpenAI TTS failed through Vercel AI Gateway; retrying through OpenRouter: %s",
                    vercel_exc,
                )
                fallback_route = await _resolve_official_model_route(
                    model,
                    openrouter_reason="media.voice.openrouter_fallback_key",
                    gateway_provider="openrouter",
                )
                if not fallback_route or not fallback_route.api_key:
                    raise vercel_exc
                audio_bytes = await _openrouter_speech_bytes(
                    api_key=fallback_route.api_key,
                    model=model,
                    prompt=prompt,
                    voice=voice,
                    voice_instructions=voice_instructions,
                    audio_format=request_format,
                )
                if request_format == "pcm" and storage_format == "wav":
                    audio_bytes = _wav_from_pcm16(audio_bytes)
                base_url_override = fallback_route.base_url
                generation_provider = AudioGenerationProvider.OPENROUTER
        elif role == AudioGenerationRole.VOICE:
            if request_format not in {"mp3", "pcm"}:
                request_format = "pcm"
                storage_format = "wav"
            audio_bytes = await _openrouter_speech_bytes(
                api_key=api_key,
                model=model,
                prompt=prompt,
                voice=voice,
                voice_instructions=voice_instructions,
                audio_format=request_format,
            )
            if request_format == "pcm" and storage_format == "wav":
                audio_bytes = _wav_from_pcm16(audio_bytes)
        else:
            audio_voice = str(kwargs.get("voice") or "").strip()
            if not audio_voice and model.lower().startswith("openai/"):
                audio_voice = _default_openrouter_voice(model)
            audio_bytes = await _openrouter_audio_output_bytes(
                api_key=api_key,
                model=model,
                prompt=_audio_prompt_for_purpose(prompt, purpose, duration_seconds),
                voice=audio_voice,
                audio_format=request_format,
            )
            if request_format in {"pcm", "pcm16"} and storage_format == "wav":
                audio_bytes = _wav_from_pcm16(audio_bytes)
        provider_response_format = request_format
        requested_artifact_format = _requested_audio_artifact_format(
            requested_audio_format,
            output_name,
        )
        if requested_artifact_format and requested_artifact_format != storage_format:
            audio_bytes = await transcode_audio_bytes(
                audio_bytes,
                source_format=storage_format,
                target_format=requested_artifact_format,
            )
            storage_format = requested_artifact_format
        if narration_profile is not None:
            output_name = _task_narrator_audio_output_name(output_name, voice)
        # Chat playback uses the same provider/credit path without creating a
        # Knowledge artifact. Only an internal Python callable can select it.
        deliver_audio = _deliver_audio if callable(_deliver_audio) else _save_generated_audio_bytes
        audio_url = await deliver_audio(
            entity_id=entity_id,
            user_id=user_id,
            prompt=prompt,
            model=model,
            purpose=purpose,
            audio_bytes=audio_bytes,
            audio_format=storage_format,
            is_byok=is_byok,
            voice=voice,
            voice_instructions=voice_instructions,
            narration_profile=narration_profile,
            language=audio_language,
            output_name=output_name,
            workspace_id=workspace_id or None,
            task_id=runtime_context.task_id,
            agent_id=runtime_context.agent_id,
            conversation_id=runtime_context.conversation_id,
        )
        payload: AudioGenerationCompletedResult = {
            "kind": GenerateFileKind.AUDIO,
            "status": AudioGenerationStatus.COMPLETED,
            "provider": generation_provider,
            "result_url": audio_url,
            "audio_url": audio_url,
            "fs_path": _fs_path_from_result_url(audio_url, entity_id),
            "prompt": prompt,
            "purpose": purpose,
            "model": model,
            "voice": voice or None,
            "voice_instructions": voice_instructions or None,
            "language": audio_language,
            "format": AudioGenerationFormat(storage_format),
            "provider_response_format": AudioGenerationFormat(provider_response_format),
            "duration_seconds": 30.0 if native_google_music and "clip" in model.lower() else duration_seconds,
            "requested_duration_seconds": duration_seconds,
            "file_size": len(audio_bytes),
        }
        if narration_profile is not None:
            payload["narration_profile"] = narration_profile
        return json.dumps(payload, ensure_ascii=False)
    except _AudioProviderUnavailable as exc:
        logger.warning(
            "Audio provider unavailable after retries: provider=%s status=%s attempts=%s model=%s",
            exc.provider,
            exc.status_code,
            exc.attempts,
            model,
        )
        return json.dumps(
            _audio_error_payload(
                code=AudioGenerationErrorCode.AUDIO_PROVIDER_UNAVAILABLE,
                error=str(exc),
                purpose=purpose,
                provider=exc.provider,
                retryable=True,
                model=model,
                provider_status=exc.status_code,
                attempts=exc.attempts,
                format_related=False,
                retry_advice="Retry the same request later; do not change the requested file format.",
            ),
            ensure_ascii=False,
        )
    except _OpenAICompatibleAudioProviderBlocker as exc:
        logger.warning("OpenAI-compatible audio provider blocker: %s", exc)
        return json.dumps(
            _audio_error_payload(
                code=AudioGenerationErrorCode.PROVIDER_BLOCKER,
                error=str(exc),
                purpose=purpose,
                provider=generation_provider,
                model=model,
            )
        )
    except Exception as exc:  # noqa: BLE001 - tool should return structured errors
        logger.exception("Audio generation failed")
        return json.dumps(
            _audio_error_payload(
                code=AudioGenerationErrorCode.AUDIO_GENERATION_FAILED,
                error=str(exc),
                purpose=purpose,
                provider=generation_provider,
                model=model,
            )
        )


# ── transcribe_audio ──────────────────────────────────────────────────────────

def _srt_timecode(seconds: float) -> str:
    """Format ``seconds`` as an SRT timecode: HH:MM:SS,mmm."""
    total_ms = int(round(max(0.0, seconds) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _srt_from_segments(segments: list[dict]) -> str:
    """Build a standard SRT document from timed transcription segments."""
    blocks: list[str] = []
    index = 1
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = _srt_timecode(float(seg.get("start") or 0.0))
        end = _srt_timecode(float(seg.get("end") or 0.0))
        blocks.append(f"{index}\n{start} --> {end}\n{text}")
        index += 1
    return ("\n\n".join(blocks) + "\n") if blocks else ""


async def _rel_path_from_document_id(document_id: str, entity_id: str) -> str | None:
    """Best-effort: resolve a Knowledge document id to its filesystem path."""
    try:
        from packages.core.database import async_session
        from packages.core.services.document_service import get_document

        async with async_session() as db:
            doc = await get_document(db, str(document_id), entity_id)
        if doc is not None:
            return getattr(doc, "fs_path", None) or None
    except Exception:
        logger.debug("audio document id resolution failed", exc_info=True)
    return None


async def _load_audio_reference_bytes(ref: str, entity_id: str) -> tuple[str, bytes, str]:
    """Load a Knowledge audio reference as ``(filename, bytes, mime)``.

    Accepts a Knowledge-relative path, a ``/api/v1/fs`` URL, a public URL, or a
    Knowledge document id — mirrors ``_load_image_reference_bytes`` but for audio.
    """
    import mimetypes

    text = str(ref or "").strip()
    if not text:
        raise ValueError("Empty audio reference")

    if text.startswith(("http://", "https://")):
        import httpx

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(text)
            resp.raise_for_status()
        mime = (resp.headers.get("content-type") or "audio/mpeg").split(";", 1)[0]
        name = os.path.basename(urlsplit(text).path) or "audio"
        return name, resp.content, mime

    if not entity_id:
        raise ValueError(f"Audio reference requires an entity filesystem: {text}")

    from packages.core.services.entity_fs import get_entity_root
    from packages.core.tasks.media_tasks import _entity_rel_path_from_reference

    entity_root = get_entity_root(entity_id)
    rel_path = _entity_rel_path_from_reference(text, entity_id, entity_root)

    # If the reference doesn't resolve to a file, try treating it as a document id.
    if not rel_path or not os.path.isfile(os.path.join(entity_root, rel_path)):
        doc_rel = await _rel_path_from_document_id(text, entity_id)
        if doc_rel:
            rel_path = doc_rel

    if not rel_path:
        raise ValueError(f"Unsupported audio reference: {text}")
    full_path = os.path.join(entity_root, rel_path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"Audio reference file not found: {rel_path}")
    mime = mimetypes.guess_type(full_path)[0] or "audio/mpeg"
    with open(full_path, "rb") as fh:
        return os.path.basename(rel_path), fh.read(), mime


async def _transcribe_audio_handler(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> str:
    """Transcribe a Knowledge audio file into timestamped segments + SRT."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    user_id = _media_context_user_id(user_id, runtime_context.user_id)
    audio_path = str(
        kwargs.get("audio_path")
        or kwargs.get("path")
        or kwargs.get("audio_url")
        or ""
    ).strip()
    if not audio_path:
        return json.dumps({"error": "audio_path is required"})

    try:
        filename, audio_bytes, mime = await _load_audio_reference_bytes(audio_path, entity_id)
    except Exception as exc:  # noqa: BLE001 - tool returns structured errors
        return json.dumps({"error": f"Could not load audio: {exc}"})

    # Resolve the STT model + optional BYOK key the same way chat audio
    # attachments do (file_context.py), so BYOK / self-hosted keys are honoured.
    stt_model = None
    user_key = None
    try:
        from packages.core.database import async_session
        from packages.core.services.model_resolver import (
            resolve_llm_metadata_for_user,
            resolve_model_for_user,
        )

        async with async_session() as db:
            stt_model = await resolve_model_for_user(
                "stt", user_id=user_id or None, entity_id=entity_id or None, db=db,
            )
            metadata = await resolve_llm_metadata_for_user(
                "stt", user_id=user_id or None, entity_id=entity_id or None, db=db,
            )
            user_key = (metadata or {}).get("llm_api_key")
    except Exception:
        logger.debug("STT model/key resolution failed", exc_info=True)
    if kwargs.get("model"):
        stt_model = str(kwargs["model"]).strip()
    language = str(kwargs.get("language") or "").strip() or None

    from packages.core.services.voice.whisper import WhisperError, transcribe_blob

    try:
        result = await transcribe_blob(
            audio_bytes,
            mime=mime or "audio/mpeg",
            filename=filename or "audio.mp3",
            language=language,
            user_api_key=user_key,
            resolved_model=stt_model,
        )
    except WhisperError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - tool returns structured errors
        logger.exception("transcribe_audio failed")
        return json.dumps({"error": str(exc)})

    segments = result.segments
    if segments:
        payload = {
            "text": result.text,
            "duration_seconds": result.duration_seconds,
            "model": result.model,
            "segments": segments,
            "srt": _srt_from_segments(segments),
            "timestamps_available": True,
        }
    else:
        payload = {
            "text": result.text,
            "duration_seconds": result.duration_seconds,
            "model": result.model,
            "segments": [],
            "srt": "",
            "timestamps_available": False,
            "note": (
                "Timestamped segments require a native Whisper/OpenAI (or Groq) "
                "speech-to-text key. This transcription used the OpenRouter "
                "chat-audio fallback, which returns text only."
            ),
        }
    return json.dumps(payload, ensure_ascii=False)


TRANSCRIBE_AUDIO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "transcribe_audio",
        "description": (
            "Transcribe a Knowledge audio file (e.g. a generated narration track) into "
            "timestamped segments plus a ready-to-use SRT subtitle string. Use it to build "
            "accurate subtitles or to time video scene-cuts against spoken narration. Returns "
            "the transcript text, duration, per-segment start/end/text timings, and an SRT "
            "document. Timestamps require a native Whisper/OpenAI (or Groq) STT key; the "
            "OpenRouter chat-audio fallback returns text only (timestamps_available=false)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audio_path": {
                    "type": "string",
                    "description": (
                        "Audio file to transcribe: a Knowledge-relative path "
                        "(e.g. project/audio/narration.mp3), a /api/v1/fs URL, or a Knowledge document id."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": "Optional STT model override (e.g. whisper-1, whisper-large-v3).",
                },
                "language": {
                    "type": "string",
                    "description": "Optional ISO language hint (e.g. en, zh) to improve accuracy.",
                },
            },
            "required": ["audio_path"],
        },
    },
}


async def _download_image_bytes(url: str) -> tuple[bytes, str]:
    import httpx

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/png")


def _extract_openrouter_image(data: dict) -> tuple[str, dict]:
    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})
    images = message.get("images", [])
    if not images:
        content = message.get("content")
        if isinstance(content, list):
            images = [c for c in content if c.get("type") == "image_url"]
    if not images:
        return "", {}
    img_item = images[0]
    if not isinstance(img_item, dict):
        return "", {}
    data_url = img_item.get("image_url", {}).get("url", "") or img_item.get("url", "")
    return data_url, data.get("usage") or {}


def _coerce_image_reference_urls(
    reference_urls: Any = None,
    reference_url: Any = None,
    image_url: Any = None,
    input_image_url: Any = None,
    input_image_urls: Any = None,
) -> list[str]:
    """Normalize image reference inputs across singular/plural aliases."""
    refs: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in refs:
            refs.append(text)

    for value in (reference_url, image_url, input_image_url):
        add(value)
    for collection in (reference_urls, input_image_urls):
        if isinstance(collection, str):
            add(collection)
        elif isinstance(collection, (list, tuple)):
            for item in collection:
                add(item)
    return refs


async def _image_reference_to_provider_url(ref: str, entity_id: str) -> str:
    """Return a provider-consumable image URL for OpenRouter-style inputs."""
    text = str(ref or "").strip()
    if text.startswith(("http://", "https://", "data:image/")):
        return text
    from packages.core.tasks.media_tasks import _ensure_public_url

    return await _ensure_public_url(text, entity_id, allow_data_uri=False)


async def _load_image_reference_bytes(ref: str, entity_id: str) -> tuple[str, bytes, str]:
    """Load a local, data URL, or remote image reference as bytes."""
    import base64
    import mimetypes
    from urllib.parse import urlsplit

    text = str(ref or "").strip()
    if not text:
        raise ValueError("Empty image reference")

    if text.startswith("data:image/"):
        header, b64_data = text.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/png"
        ext = _image_mime_to_ext(mime)
        return f"reference{ext}", base64.b64decode(b64_data), mime

    if text.startswith(("http://", "https://")):
        image_bytes, mime = await _download_image_bytes(text)
        name = os.path.basename(urlsplit(text).path) or f"reference{_image_mime_to_ext(mime)}"
        return name, image_bytes, mime

    if not entity_id:
        raise ValueError(f"Image reference requires an entity filesystem: {text}")

    from packages.core.services.entity_fs import get_entity_root
    from packages.core.tasks.media_tasks import _entity_rel_path_from_reference

    entity_root = get_entity_root(entity_id)
    rel_path = _entity_rel_path_from_reference(text, entity_id, entity_root)
    if not rel_path:
        raise ValueError(f"Unsupported image reference: {text}")
    full_path = os.path.join(entity_root, rel_path)
    if not os.path.isfile(full_path):
        raise FileNotFoundError(f"Image reference file not found: {rel_path}")
    mime = mimetypes.guess_type(full_path)[0] or "image/png"
    with open(full_path, "rb") as f:
        return os.path.basename(rel_path), f.read(), mime


async def _load_image_references_for_upload(
    refs: list[str],
    entity_id: str,
    *,
    limit: int = 16,
) -> list[tuple[str, bytes, str]]:
    """Load references as bytes for native multipart/inline image APIs.

    OpenAI image edits and Google inline image parts receive the bytes from
    Manor itself, so local entity-filesystem references must not be converted
    to provider-fetchable public URLs first. OpenRouter's URL-only chat path
    continues to use ``_image_reference_to_provider_url`` directly.
    """
    loaded: list[tuple[str, bytes, str]] = []
    for ref in refs[:limit]:
        loaded.append(await _load_image_reference_bytes(ref, entity_id))
    return loaded


# ── generate_image ───────────────────────────────────────────────────────────


async def _resolve_user_image_model(
    user_id: str,
    entity_id: str,
    is_openrouter: bool,
) -> str:
    """Resolve the image-generation model the user (or entity) picked
    in Account → AI Models. Falls back to a sane provider default."""
    from packages.core.services.model_resolver import resolve_model_for_user

    fallback = "openai/gpt-image-2"
    try:
        picked = await resolve_model_for_user(
            "image",
            user_id=user_id or None,
            entity_id=entity_id or None,
        )
        return picked or fallback
    except Exception as exc:
        logger.debug("image model resolution fell back to default: %s", exc)
        return fallback


GENERATE_IMAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image from a text description using AI (GPT-5 Image). "
            "When the current chat contains attached image markers like "
            "[Image: name → /api/v1/fs/...] or [Image from KB: name → /api/v1/fs/...], "
            "pass those URLs in reference_url/reference_urls if the user asks to use the image as a reference. "
            "Returns a URL to the generated image. Use markdown ![desc](url) to display it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed description of the image to generate"},
                "name": {
                    "type": "string",
                    "description": "Optional user-visible filename or Knowledge-relative path, e.g. cafe-scene.png or 猫咪打工人动漫/images/场景.png.",
                },
                "size": {
                    "type": "string",
                    "enum": ["1024x1024", "1536x1024", "1024x1536"],
                    "description": "Image size (default 1024x1024). 1536x1024 for landscape, 1024x1536 for portrait.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["16:9", "9:16", "1:1"],
                    "description": (
                        "Requested output aspect ratio. Manor preserves every generated pixel and pads the "
                        "short side when the provider returns a different shape; it never center-crops."
                    ),
                },
                "quality": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Image quality (default medium). Higher quality takes longer.",
                },
                "reference_url": {
                    "type": "string",
                    "description": "Optional local Knowledge path, /api/v1/fs URL, data URL, or public URL to use as an image reference.",
                },
                "reference_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional image references for composition, style transfer, or edits. Up to 16 for OpenAI/Gemini.",
                },
                "image_url": {
                    "type": "string",
                    "description": "Alias for reference_url when editing or generating from an existing image.",
                },
                "input_fidelity": {
                    "type": "string",
                    "enum": ["low", "high"],
                    "description": "OpenAI GPT Image only: how strongly to preserve input image details. Defaults to low.",
                },
                "save_to_knowledge": {
                    "type": "boolean",
                    "description": (
                        "Whether to register the generated image as a Knowledge document. "
                        "Defaults to true. Set false for temporary style references or QA previews."
                    ),
                },
                "workspace_asset_key": {
                    "type": "string",
                    "description": (
                        "Optional stable key for a reusable image shared by every task in the current Workspace, "
                        "for example stickman_character."
                    ),
                },
                "reuse_if_exists": {
                    "type": "boolean",
                    "description": (
                        "When workspace_asset_key is set, return the existing Workspace image without a paid "
                        "generation call. If it does not exist, generate it once and remember it."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}


def _image_size_for_aspect_ratio(aspect_ratio: str = "", explicit_size: Any = None) -> str:
    if explicit_size:
        return str(explicit_size)
    return {
        "16:9": "1536x1024",
        "9:16": "1024x1536",
        "1:1": "1024x1024",
    }.get(str(aspect_ratio or "").strip(), "1024x1024")


_IMAGE_ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
}


def _aspect_ratio_prompt_hint(aspect_ratio: str = "") -> str:
    """Say the aspect ratio in the prompt, because most routes cannot say it
    any other way.

    The OpenRouter image route is a chat completion — there is no size or
    aspect parameter, only the prompt. Without this the model composes to its
    own default (usually square) and the caller's requested ratio is a
    fiction. Asking up front is the only way to get a correctly *composed*
    image; reshaping afterwards can only move or destroy pixels.
    """
    target = _IMAGE_ASPECT_RATIOS.get(str(aspect_ratio or "").strip())
    if not target:
        return ""
    ratio = f"{target[0]}:{target[1]}"
    if target[0] > target[1]:
        orientation = "landscape (wider than tall)"
    elif target[0] < target[1]:
        orientation = "portrait (taller than wide)"
    else:
        orientation = "square"
    return (
        f"\n\nIMPORTANT — compose this image as {ratio} {orientation}. "
        f"Fit the entire composition, including every piece of text, inside "
        f"the {ratio} frame with margins; nothing may run past the edges."
    )


def _dominant_border_color(image: "Image.Image") -> tuple[int, int, int]:
    """The colour the image already ends in, so padding continues it.

    Black bars on a white-background line drawing read as damage. Sampling
    what the edge actually is makes the added area a continuation of the
    canvas rather than a frame around it.
    """
    from collections import Counter

    rgb = image.convert("RGB")
    width, height = rgb.size
    step = max(1, min(width, height) // 64)
    edge = Counter()
    for x in range(0, width, step):
        edge[rgb.getpixel((x, 0))] += 1
        edge[rgb.getpixel((x, height - 1))] += 1
    for y in range(0, height, step):
        edge[rgb.getpixel((0, y))] += 1
        edge[rgb.getpixel((width - 1, y))] += 1
    return edge.most_common(1)[0][0] if edge else (255, 255, 255)


def _normalize_image_bytes_for_aspect_ratio(
    image_bytes: bytes,
    mime: str,
    aspect_ratio: str = "",
) -> tuple[bytes, str, str]:
    """Deliver the requested aspect ratio by padding. Never crop.

    This once center-cropped to force the ratio, which silently destroyed
    content: a 1024x1024 poster requested as 9:16 came back 576x1024 — 43% of
    the width gone, headline and side labels sliced off both edges, nothing in
    the result saying so. That was removed, and for a while an off-ratio image
    was delivered as generated on the reasoning that bars are also a silent
    edit.

    Delivering off-ratio has its own cost, and downstream it is the larger
    one. The video pipeline animates each still with generate_video, and a
    still whose ratio differs from the clip's gets cropped by the provider —
    outside our ffmpeg, where nothing can pad it back. Fixing the ratio here,
    where the whole composition is still present, is what keeps every later
    stage from having to.

    So: pad to the requested ratio, extending the colour the image already
    ends in. Every pixel the model produced survives; the frame simply grows.
    """
    try:
        import io
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        width, height = image.size
        if width <= 0 or height <= 0:
            return image_bytes, mime, ""
        # convert() drops .format, and re-encoding line art as JPEG is its
        # own quiet damage — read it while it is still there.
        source_format = (image.format or "").upper()

        target = _IMAGE_ASPECT_RATIOS.get(str(aspect_ratio or "").strip())
        if not target:
            return image_bytes, mime, f"{width}x{height}"

        target_ratio = target[0] / target[1]
        if abs((width / height) - target_ratio) < 0.01:
            return image_bytes, mime, f"{width}x{height}"

        # Grow the short side; never reduce either one, or we would be
        # cropping by another name.
        if (width / height) > target_ratio:
            canvas_w, canvas_h = width, max(height, round(width / target_ratio))
        else:
            canvas_w, canvas_h = max(width, round(height * target_ratio)), height

        has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
        if has_alpha:
            image = image.convert("RGBA")
            canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
            out_format, out_mime = "PNG", "image/png"
        else:
            image = image.convert("RGB")
            canvas = Image.new("RGB", (canvas_w, canvas_h), _dominant_border_color(image))
            out_format = "JPEG" if source_format in ("JPEG", "JPG") else "PNG"
            out_mime = "image/png" if out_format == "PNG" else "image/jpeg"

        canvas.paste(image, ((canvas_w - width) // 2, (canvas_h - height) // 2))
        buffer = io.BytesIO()
        canvas.save(buffer, format=out_format, **({"quality": 95} if out_format == "JPEG" else {}))
        logger.info(
            "generated image was %dx%d, not the requested %s — padded to %dx%d (no crop)",
            width, height, aspect_ratio, canvas_w, canvas_h,
        )
        return buffer.getvalue(), out_mime, f"{canvas_w}x{canvas_h}"
    except Exception:
        logger.debug("image aspect normalisation failed", exc_info=True)
        return image_bytes, mime, ""



def _image_response_json(resp, *, provider: str) -> tuple[dict, str]:
    """Parse a provider image response defensively.

    Gateway outages and edge proxies return HTML or empty bodies; a raw
    ``resp.json()`` there surfaces as the useless "Expecting value: line 1
    column 1" error. Return (data, "") on success or ({}, message) with the
    HTTP status and a body snippet so the failure is actionable.
    """
    try:
        return resp.json(), ""
    except Exception:
        body = (resp.text or "")[:200].strip()
        detail = repr(body) if body else "empty body"
        return {}, (
            f"{provider} returned a non-JSON response "
            f"(HTTP {resp.status_code}, {detail})"
        )


async def _generate_image_handler(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> str:
    """Generate an image through OpenRouter or the selected model's native API.

    Model selection honours the entity-scoped Account -> Image picker.
    OpenRouter keys keep using OpenRouter; native OpenAI/Google keys are
    routed to their first-party image APIs.
    """
    import base64
    import httpx

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    user_id = await _resolve_media_task_user_id(
        _media_context_user_id(user_id, runtime_context.user_id),
        entity_id,
        kwargs.get("task_id") or runtime_context.task_id,
    )
    prompt = kwargs.get("prompt", "")
    output_name = str(kwargs.get("name") or kwargs.get("output_name") or kwargs.get("filename") or "").strip()
    aspect_ratio = str(kwargs.get("aspect_ratio") or "").strip()
    size = _image_size_for_aspect_ratio(aspect_ratio, kwargs.get("size"))
    # State the ratio in the prompt as well as the API field. The OpenRouter
    # route is a chat completion with no size parameter at all, so without
    # this the model never learns the requested shape — it composed square
    # posters that were then cropped to fit.
    if isinstance(prompt, str) and prompt.strip():
        prompt = f"{prompt}{_aspect_ratio_prompt_hint(aspect_ratio)}"
    quality = kwargs.get("quality", "medium")
    save_to_knowledge = _coerce_bool(kwargs.get("save_to_knowledge"), True)
    # Optional: deliver the generated image straight into the active sandbox
    # (used by the pptx image mode so page images bypass the laggy entity-FS
    # mount). When set, the image bytes are also written into the sandbox at
    # this path. Best-effort; never blocks generation.
    sandbox_path = str(kwargs.get("sandbox_path") or "").strip() or None
    reference_urls = _coerce_image_reference_urls(
        kwargs.get("reference_urls"),
        kwargs.get("reference_url"),
        kwargs.get("image_url"),
        kwargs.get("input_image_url"),
        kwargs.get("input_image_urls"),
    )
    input_fidelity = str(kwargs.get("input_fidelity") or "").strip().lower()
    workspace_asset_key = _workspace_asset_key(kwargs.get("workspace_asset_key"))
    reuse_if_exists = _coerce_bool(kwargs.get("reuse_if_exists"), False)
    if runtime_context.workspace_id:
        workspace_studio_profile = await _workspace_stickman_studio_profile(
            entity_id=entity_id,
            workspace_id=str(runtime_context.workspace_id),
        )
        configured_asset_path = str(
            workspace_studio_profile.get("character_asset_path") or ""
        ).strip().replace("\\", "/").lstrip("/")
        normalized_output_name = output_name.replace("\\", "/").lstrip("/")
        configured_character_target = bool(
            configured_asset_path
            and (
                normalized_output_name == configured_asset_path
                or normalized_output_name.endswith(f"/{configured_asset_path}")
            )
        )
        if configured_character_target:
            # The canonical path is an operator-owned Workspace invariant.
            # Override an omitted *or model-invented* asset key here: a skill
            # must not register a duplicate shared character merely because it
            # copied the fully scoped manifest path or derived a key from the
            # filename.
            workspace_asset_key = _workspace_asset_key(
                workspace_studio_profile.get("character_asset_key")
            )
            reuse_if_exists = bool(workspace_asset_key)
    if not prompt:
        return json.dumps({"error": "prompt is required"})

    if workspace_asset_key:
        if not entity_id or not runtime_context.workspace_id:
            return json.dumps(
                {"error": "workspace_asset_key requires a valid Workspace context."}
            )
        if reuse_if_exists:
            existing_asset = await _load_workspace_reusable_image(
                entity_id=entity_id,
                workspace_id=runtime_context.workspace_id,
                asset_key=workspace_asset_key,
            )
            if existing_asset is not None:
                payload = _image_result_payload(
                    image_url=str(existing_asset["result_url"]),
                    prompt=str(existing_asset.get("prompt") or prompt),
                    size=str(existing_asset.get("size") or size),
                    model=str(existing_asset.get("model") or "workspace-reusable-asset"),
                    entity_id=entity_id,
                    include_fs_path=True,
                    saved_to_knowledge=True,
                )
                payload.update(
                    workspace_asset_key=workspace_asset_key,
                    reused_workspace_asset=True,
                )
                return json.dumps(payload)

    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(user_id, entity_id, role="image")
    model = await _resolve_user_image_model(user_id, entity_id, api_key.startswith("sk-or-"))
    if kwargs.get("model"):
        model = str(kwargs["model"]).strip()
    provider = _catalog_provider(model)
    managed_vercel_image = False
    vercel_auth_method = "api-key"
    if (
        not is_byok
        and os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud"
    ):
        try:
            official_route = await _resolve_official_model_route(
                model,
                vercel_reason="media.image.vercel_gateway_key",
                provider_chain=("vercel", provider, "openrouter"),
            )
        except Exception:
            logger.debug("Managed Vercel image route lookup failed", exc_info=True)
            official_route = None
        if official_route and official_route.api_key:
            api_key = official_route.api_key
            base_url_override = official_route.base_url
            managed_vercel_image = official_route.provider == "vercel"
            vercel_auth_method = (
                "oidc"
                if str(getattr(official_route, "source_detail", "")).strip()
                == "VERCEL_OIDC_TOKEN"
                else "api-key"
            )
    if not managed_vercel_image and not is_byok and provider in {"openai", "google"}:
        native_key, native_base_url = await _platform_native_media_credential_async(provider)
        if native_key:
            api_key = native_key
            base_url_override = native_base_url or base_url_override
            is_byok = False
    if not api_key and not managed_vercel_image:
        api_key, native_base_url = await _platform_native_media_credential_async(provider)
        base_url_override = native_base_url or base_url_override
        is_byok = False
    if not api_key:
        return json.dumps({"error": "No image generation API key configured"})
    if entity_id and not is_byok:
        await runtime_assert_credit_available(
            entity_id,
            source=RUNTIME_GENERATE_IMAGE_TOOL_SOURCE,
        )

    is_openrouter = api_key.startswith("sk-or-")
    if not managed_vercel_image and not is_openrouter and provider in {"openai", "google"}:
        from packages.core.services.model_resolver import detect_llm_provider_from_key

        key_provider = detect_llm_provider_from_key(api_key)
        if key_provider and key_provider != provider:
            provider_label = "Google/Gemini" if provider == "google" else provider.title()
            return json.dumps(
                {
                    "error": (
                        f"The selected {provider_label} image model requires a native "
                        f"{provider_label} API key. Save a matching key for Image."
                    )
                }
            )

    try:
        if managed_vercel_image:
            from packages.core.services.vercel_ai_gateway import vercel_gateway_post

            request: dict[str, Any] = {"prompt": prompt, "n": 1}
            if aspect_ratio:
                request["aspectRatio"] = aspect_ratio
            elif size:
                request["size"] = size
            if reference_urls:
                images = await _load_image_references_for_upload(reference_urls, entity_id)
                request["files"] = [
                    {
                        "type": "file",
                        "mediaType": mime,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                    for _name, image_bytes, mime in images
                ]
            data = await vercel_gateway_post(
                api_key=api_key,
                base_url=base_url_override,
                model=model,
                protocol="image",
                payload=request,
                auth_method=vercel_auth_method,
            )
            generated = data.get("images") or []
            encoded = generated[0] if isinstance(generated, list) and generated else ""
            if not isinstance(encoded, str) or not encoded.strip():
                return json.dumps(
                    {"error": "Vercel AI Gateway image response did not include image data."}
                )
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except Exception:
                return json.dumps(
                    {"error": "Vercel AI Gateway returned invalid base64 image data."}
                )
            mime = "image/png"
            image_bytes, mime, actual_size = _normalize_image_bytes_for_aspect_ratio(
                image_bytes,
                mime,
                aspect_ratio,
            )
            if actual_size:
                size = actual_size
            image_url = await _save_generated_image_bytes(
                entity_id=entity_id,
                user_id=user_id,
                prompt=prompt,
                output_name=output_name,
                model=model,
                size=size,
                image_bytes=image_bytes,
                mime=mime,
                is_byok=False,
                usage=data.get("usage") or {},
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                agent_id=runtime_context.agent_id,
                conversation_id=runtime_context.conversation_id,
                save_to_knowledge=save_to_knowledge,
                sandbox_path=sandbox_path,
                workspace_shared=bool(workspace_asset_key),
            )
            return json.dumps(
                await _generated_image_result_payload(
                    image_url=image_url,
                    prompt=prompt,
                    size=size,
                    model=model,
                    entity_id=entity_id,
                    workspace_id=runtime_context.workspace_id,
                    workspace_asset_key=workspace_asset_key,
                    include_fs_path=bool(kwargs.get("workspace_id")),
                    saved_to_knowledge=save_to_knowledge,
                )
            )

        if is_openrouter:
            message_content: Any = prompt
            if reference_urls:
                content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                for ref_url in reference_urls[:16]:
                    content_parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": await _image_reference_to_provider_url(ref_url, entity_id)},
                        }
                    )
                message_content = content_parts
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://manor.ai",
                        "X-Title": "Manor AI",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": message_content}],
                    },
                )
                data, parse_err = _image_response_json(resp, provider="OpenRouter")
            if parse_err:
                return json.dumps({"error": f"Image generation failed: {parse_err}"})

            if resp.status_code != 200:
                err = data.get("error", {})
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                return json.dumps({"error": f"Image generation failed ({resp.status_code}): {msg}"})

            data_url, usage = _extract_openrouter_image(data)
            if not data_url:
                return json.dumps({"error": "No image generated. The model returned no images."})

            if data_url.startswith("data:"):
                header, b64_data = data_url.split(",", 1)
                mime = header[5:].split(";", 1)[0] or "image/png"
                image_bytes = base64.b64decode(b64_data)
            elif data_url.startswith("http"):
                image_bytes, mime = await _download_image_bytes(data_url)
            else:
                return json.dumps({"error": "Unexpected image format in response"})
            image_bytes, mime, actual_size = _normalize_image_bytes_for_aspect_ratio(
                image_bytes,
                mime,
                aspect_ratio,
            )
            if actual_size:
                size = actual_size

            image_url = await _save_generated_image_bytes(
                entity_id=entity_id,
                user_id=user_id,
                prompt=prompt,
                output_name=output_name,
                model=model,
                size=size,
                image_bytes=image_bytes,
                mime=mime,
                is_byok=is_byok,
                usage=usage,
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                agent_id=runtime_context.agent_id,
                conversation_id=runtime_context.conversation_id,
                save_to_knowledge=save_to_knowledge,
                sandbox_path=sandbox_path,
                workspace_shared=bool(workspace_asset_key),
            )
            return json.dumps(
                await _generated_image_result_payload(
                    image_url=image_url,
                    prompt=prompt,
                    size=size,
                    model=model,
                    entity_id=entity_id,
                    workspace_id=runtime_context.workspace_id,
                    workspace_asset_key=workspace_asset_key,
                    include_fs_path=bool(kwargs.get("workspace_id")),
                    saved_to_knowledge=save_to_knowledge,
                )
            )

        if provider == "openai":
            if not api_key.startswith("sk-") or api_key.startswith("sk-or-"):
                return json.dumps({"error": "The selected OpenAI image model requires an OpenAI API key."})
            native_model = _native_media_model(model, kind="image", provider=provider)
            native_base_url = base_url_override or "https://api.openai.com/v1"
            if reference_urls:
                if native_model == "dall-e-3":
                    return json.dumps(
                        {"error": "DALL-E 3 does not support image references. Choose a GPT Image model."}
                    )
                images = await _load_image_references_for_upload(reference_urls, entity_id)
                files = [("image[]", (name, image_bytes, mime)) for name, image_bytes, mime in images]
                form_data = {
                    "model": native_model,
                    "prompt": prompt,
                    "size": size,
                    "quality": quality,
                    "n": "1",
                }
                if input_fidelity:
                    form_data["input_fidelity"] = input_fidelity
                async with httpx.AsyncClient(timeout=180.0) as client:
                    resp = await client.post(
                        f"{native_base_url}/images/edits",
                        headers={"Authorization": f"Bearer {api_key}"},
                        data=form_data,
                        files=files,
                    )
                    data, parse_err = _image_response_json(resp, provider="OpenAI")
                if parse_err:
                    return json.dumps({"error": f"Image edit failed: {parse_err}"})
            else:
                async with httpx.AsyncClient(timeout=180.0) as client:
                    resp = await client.post(
                        f"{native_base_url}/images/generations",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": native_model,
                            "prompt": prompt,
                            "size": size,
                            "quality": quality,
                            "n": 1,
                        },
                    )
                    data, parse_err = _image_response_json(resp, provider="OpenAI")
                if parse_err:
                    return json.dumps({"error": f"Image generation failed: {parse_err}"})

            if resp.status_code != 200:
                err = data.get("error", {})
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                return json.dumps({"error": f"OpenAI image generation failed ({resp.status_code}): {msg}"})

            first = (data.get("data") or [{}])[0]
            b64_data = first.get("b64_json") or ""
            if b64_data:
                image_bytes = base64.b64decode(b64_data)
                mime = "image/png"
            elif first.get("url"):
                image_bytes, mime = await _download_image_bytes(first["url"])
            else:
                return json.dumps({"error": "OpenAI image response did not include image data."})
            image_bytes, mime, actual_size = _normalize_image_bytes_for_aspect_ratio(
                image_bytes,
                mime,
                aspect_ratio,
            )
            if actual_size:
                size = actual_size

            image_url = await _save_generated_image_bytes(
                entity_id=entity_id,
                user_id=user_id,
                prompt=prompt,
                output_name=output_name,
                model=model,
                size=size,
                image_bytes=image_bytes,
                mime=mime,
                is_byok=is_byok,
                usage=data.get("usage") or {},
                workspace_id=runtime_context.workspace_id,
                task_id=runtime_context.task_id,
                agent_id=runtime_context.agent_id,
                conversation_id=runtime_context.conversation_id,
                save_to_knowledge=save_to_knowledge,
                sandbox_path=sandbox_path,
                workspace_shared=bool(workspace_asset_key),
            )
            return json.dumps(
                await _generated_image_result_payload(
                    image_url=image_url,
                    prompt=prompt,
                    size=size,
                    model=model,
                    entity_id=entity_id,
                    workspace_id=runtime_context.workspace_id,
                    workspace_asset_key=workspace_asset_key,
                    include_fs_path=bool(kwargs.get("workspace_id")),
                    saved_to_knowledge=save_to_knowledge,
                )
            )

        if provider == "google":
            native_model = _native_media_model(model, kind="image", provider=provider)
            native_base_url = _native_media_base_url(
                "google",
                base_url_override or "https://generativelanguage.googleapis.com/v1beta",
            )
            parts_payload: list[dict[str, Any]] = [{"text": prompt}]
            if reference_urls:
                images = await _load_image_references_for_upload(reference_urls, entity_id)
                for _name, image_bytes, mime in images:
                    parts_payload.append(
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        }
                    )
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(
                    f"{native_base_url}/models/{native_model}:generateContent",
                    headers={
                        "x-goog-api-key": api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "contents": [{"parts": parts_payload}],
                        "generationConfig": {"responseModalities": ["IMAGE"]},
                    },
                )
                data, parse_err = _image_response_json(resp, provider="Google")
            if parse_err:
                return json.dumps({"error": f"Google image generation failed: {parse_err}"})

            if resp.status_code != 200:
                err = data.get("error", {})
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                return json.dumps({"error": f"Google image generation failed ({resp.status_code}): {msg}"})

            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            for part in parts:
                inline = part.get("inlineData") or part.get("inline_data") or {}
                b64_data = inline.get("data") or ""
                if not b64_data:
                    continue
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                image_bytes = base64.b64decode(b64_data)
                image_bytes, mime, actual_size = _normalize_image_bytes_for_aspect_ratio(
                    image_bytes,
                    mime,
                    aspect_ratio,
                )
                if actual_size:
                    size = actual_size
                image_url = await _save_generated_image_bytes(
                    entity_id=entity_id,
                    user_id=user_id,
                    prompt=prompt,
                    output_name=output_name,
                    model=model,
                    size=size,
                    image_bytes=image_bytes,
                    mime=mime,
                    is_byok=is_byok,
                    usage={},
                    workspace_id=runtime_context.workspace_id,
                    task_id=runtime_context.task_id,
                    agent_id=runtime_context.agent_id,
                    conversation_id=runtime_context.conversation_id,
                    save_to_knowledge=save_to_knowledge,
                    sandbox_path=sandbox_path,
                    workspace_shared=bool(workspace_asset_key),
                )
                return json.dumps(
                    await _generated_image_result_payload(
                        image_url=image_url,
                        prompt=prompt,
                        size=size,
                        model=model,
                        entity_id=entity_id,
                        workspace_id=runtime_context.workspace_id,
                        workspace_asset_key=workspace_asset_key,
                        include_fs_path=bool(kwargs.get("workspace_id")),
                        saved_to_knowledge=save_to_knowledge,
                    )
                )
            return json.dumps({"error": "Google image response did not include inline image data."})

        return json.dumps(
            {
                "error": (
                    f"No native image adapter for {provider or 'this'} model. "
                    "Use an OpenRouter key or choose an OpenAI/Google image model."
                )
            }
        )

    except httpx.TimeoutException:
        return json.dumps({"error": "Image generation timed out (180s). Try a simpler prompt."})
    except Exception as e:
        logger.error("generate_image failed: %s", e, exc_info=True)
        return json.dumps({"error": f"Image generation failed: {e}"})


# ── generate_video ───────────────────────────────────────────────────────────


async def _resolve_user_video_model(
    user_id: str,
    entity_id: str,
) -> str:
    """Resolve the video model the user picked in Account → AI Models."""
    from packages.core.services.model_resolver import resolve_model_for_user

    fallback = "bytedance/seedance-2.0"
    try:
        picked = await resolve_model_for_user(
            "video",
            user_id=user_id or None,
            entity_id=entity_id or None,
        )
        return picked or fallback
    except Exception as exc:
        logger.debug("video model resolution fell back to default: %s", exc)
        return fallback


GENERATE_VIDEO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_video",
        "description": (
            "Generate one short video clip from a text prompt using the Account-selected video model. "
            "This tool is for a single provider clip only, not a full long-form master. "
            "For requested total runtimes over 15 seconds, create multiple clip jobs whose durations "
            "sum exactly to the target, wait for them, then merge them. "
            "Supports image-to-video (first/last frame), reference images, and supported-model reference video/audio inputs. "
            "Starts generation in the background (30-90 seconds). "
            "After calling this, inform the user that generation has started. "
            "If you mention the model, copy the exact `model` value returned by this tool; "
            "do not guess, rename, or substitute another provider/model.\n\n"
            "When the user attaches images in their message, their URLs appear as "
            "[Image: filename → /api/v1/fs/...] in the text. Use these URLs as:\n"
            "- first_frame_url: if user wants to 'animate this image' or 'start from this'\n"
            "- last_frame_url: if user specifies an ending frame\n"
            "- reference_urls: if user wants style/character/scene consistency from reference photos\n"
            "Choose based on user intent. If ambiguous and only one image, use first_frame_url. "
            "If multiple images with no specific instruction, use reference_urls. "
            "Official Seedance reference limits: up to 9 image references, 3 video references, "
            "and 3 audio references per clip. Audio references must be paired with at least one "
            "image or video reference so the model has a visual subject/environment. Seedance "
            "reference_video_urls, audio_reference_urls, and generate_audio require Manor's "
            "native Volcengine/Seedance route; do not use OpenRouter for those inputs. "
            "For Vercel and OpenRouter routes, local image references may be sent inline "
            "as Base64 data, so they do not normally need PUBLIC_BASE_URL. If an image "
            "cannot fit the provider's inline size limit, Manor falls back to a signed "
            "public URL. Native Seedance routes, reference video, and reference audio "
            "always require a provider-readable HTTPS URL from PUBLIC_BASE_URL. "
            "The selected video model's capabilities are validated before generation: "
            "unsupported last frames, reference images/video/audio, or native audio requests fail "
            "fast instead of being silently ignored. For narration, dialogue, BGM, "
            "SFX, or subtitles, generate a silent video first and use audio/subtitle "
            "post-production tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed description of the video to generate"},
                "name": {
                    "type": "string",
                    "description": "Optional user-visible filename or Knowledge-relative path, e.g. mountain-storm.mp4 or 猫咪打工人动漫/videos/EP03.mp4.",
                },
                "first_frame_url": {
                    "type": "string",
                    "description": "First frame image URL. Vercel/OpenRouter can send local /api/v1/fs/... image files inline; other routes require an externally reachable https:// URL or PUBLIC_BASE_URL for a signed URL.",
                },
                "last_frame_url": {
                    "type": "string",
                    "description": "Last frame image URL. Vercel/OpenRouter can send local /api/v1/fs/... image files inline; other routes require an externally reachable https:// URL or PUBLIC_BASE_URL for a signed URL.",
                },
                "reference_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference image URLs for style/character consistency. Vercel/OpenRouter can send local /api/v1/fs/... image files inline; other routes require externally reachable https:// URLs or PUBLIC_BASE_URL. Seedance supports up to 9.",
                },
                "reference_url": {
                    "type": "string",
                    "description": "Single reference image URL. Alias for reference_urls. Vercel/OpenRouter can send local /api/v1/fs/... image files inline; other routes require externally reachable https:// URLs or PUBLIC_BASE_URL.",
                },
                "reference_video_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Official Seedance reference video URLs for motion/camera/action reference. Up to 3 provider-readable URLs. Requires native Volcengine/Seedance routing, not OpenRouter.",
                },
                "reference_video_url": {
                    "type": "string",
                    "description": "Single official Seedance reference video URL. Alias for reference_video_urls. Requires native Volcengine/Seedance routing.",
                },
                "video_reference_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for reference_video_urls.",
                },
                "video_reference_url": {
                    "type": "string",
                    "description": "Alias for reference_video_url.",
                },
                "audio_reference_url": {
                    "type": "string",
                    "description": "Official Seedance audio reference URL for music/dialogue/timing/lip-sync conditioning. Up to 3 audio refs total; pair with at least one image/video reference. Requires native Volcengine/Seedance routing, not OpenRouter.",
                },
                "audio_reference_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Official Seedance audio reference URLs. Up to 3 provider-readable URLs; must be paired with at least one image/video reference. Requires native Volcengine/Seedance routing.",
                },
                "reference_audio_url": {
                    "type": "string",
                    "description": "Alias for audio_reference_url.",
                },
                "reference_audio_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for audio_reference_urls.",
                },
                "audio_url": {
                    "type": "string",
                    "description": "Alias for audio_reference_url when requesting audio-driven video generation.",
                },
                "duration": {
                    "type": "integer",
                    "enum": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
                    "default": 5,
                    "description": "Single-clip duration in seconds. This is not total project duration. Choose one supported value; default 5.",
                },
                "frames": {
                    "type": "integer",
                    "description": "Seedance-only frame count. When provided, Seedance uses frames instead of duration.",
                },
                "resolution": {
                    "type": "string",
                    "enum": ["480p", "720p", "1080p"],
                    "description": "Video resolution (default 720p). Seedance 2.0 Fast supports 480p/720p only; unsupported 1080p is downgraded to 720p.",
                },
                "route_provider": {
                    "type": "string",
                    "enum": ["auto", "vercel", "native"],
                    "default": "auto",
                    "description": "Routing policy. auto preserves Account BYOK preference; vercel explicitly uses Vercel AI Gateway; native explicitly uses the model provider credential.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": ["adaptive", "21:9", "16:9", "4:3", "3:4", "1:1", "9:16"],
                    "description": "Video aspect ratio. Default 16:9. Seedance also supports adaptive.",
                },
                "seed": {
                    "type": "integer",
                    "description": "Random seed for reproducible results.",
                },
                "generate_audio": {
                    "type": "boolean",
                    "default": True,
                    "description": "Seedance-only native provider audio when supported. Defaults true; set false for a silent clean picture. audio_reference_urls/audio_url can still be supplied for timing/performance reference.",
                },
                "requires_reference_media": {
                    "type": "boolean",
                    "default": False,
                    "description": "Set true only when the request cannot be satisfied without explicit reference media URLs. Leave false for text-to-video.",
                },
                "return_last_frame": {
                    "type": "boolean",
                    "description": "Seedance-only: ask the API to return the final frame when supported.",
                },
                "camera_fixed": {
                    "type": "boolean",
                    "description": "Seedance-only: keep the camera fixed when supported.",
                },
                "watermark": {
                    "type": "boolean",
                    "description": "Seedance-only: include a provider watermark. Defaults to false.",
                },
                "draft": {
                    "type": "boolean",
                    "description": "Seedance-only: request draft/preview generation when supported.",
                },
            },
            "required": ["prompt"],
        },
    },
}


def _coerce_video_reference_urls(
    reference_urls: Any = None,
    reference_url: Any = None,
    *extra_reference_inputs: Any,
) -> list[str]:
    """Normalize singular/plural video reference URL inputs."""
    refs: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in refs:
            refs.append(text)

    for collection in (reference_url, reference_urls, *extra_reference_inputs):
        if isinstance(collection, str):
            add(collection)
        elif isinstance(collection, (list, tuple)):
            for item in collection:
                add(item)
        else:
            add(collection)
    return refs


_INLINE_IMAGE_REFERENCE_RE = re.compile(
    r"\[(?:Image|Image from KB):[^\]\n]*?(?:→|->)\s*"
    r"(?P<url>(?:/api/v1/fs/[^\]\s]+|https?://[^\]\s]+))\]",
    re.IGNORECASE,
)


def _extract_inline_image_reference_urls(text: Any) -> list[str]:
    """Extract stable image URLs from chat inline attachment markers."""
    refs: list[str] = []
    for match in _INLINE_IMAGE_REFERENCE_RE.finditer(str(text or "")):
        url = match.group("url").strip().rstrip(".,;")
        if url and url not in refs:
            refs.append(url)
    return refs


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _video_start_frame_intent(text: str) -> bool:
    terms = (
        "first frame",
        "start frame",
        "starting frame",
        "opening frame",
        "initial frame",
        "animate this image",
        "image-to-video",
        "i2v",
        "首帧",
        "起始帧",
        "开始帧",
        "开头帧",
        "第一帧",
        "图生视频",
        "从这张开始",
        "从第一张开始",
    )
    return _contains_any(text, terms)


def _video_end_frame_intent(text: str) -> bool:
    terms = (
        "last frame",
        "end frame",
        "ending frame",
        "final frame",
        "closing frame",
        "收尾帧",
        "尾帧",
        "结束帧",
        "结尾帧",
        "最后一帧",
        "到这张结束",
        "以这张结束",
    )
    return _contains_any(text, terms)


def _video_start_end_frame_intent(text: str) -> bool:
    paired_terms = (
        "first and last frame",
        "first/last frame",
        "start and end frame",
        "start/end frame",
        "opening and closing frame",
        "首尾帧",
        "首帧和尾帧",
        "首帧尾帧",
        "开始和结束帧",
        "开头和结尾帧",
    )
    return _contains_any(text, paired_terms) or (_video_start_frame_intent(text) and _video_end_frame_intent(text))


def _video_reference_intent(text: str) -> bool:
    # Kept for back-compat imports. Tool execution no longer infers required
    # media from prompt keywords; callers must pass fixed reference parameters.
    del text
    return False


_REFERENCE_MARKERS = (
    "[image:",
    "[image from kb:",
    "[video:",
    "[video from kb:",
    "[audio:",
    "[audio from kb:",
)


def _reference_url_variants(ref_url: Any) -> set[str]:
    raw = str(ref_url or "").strip()
    if not raw:
        return set()
    decoded = unescape(raw)
    try:
        decoded = unquote(decoded)
    except Exception:
        pass
    path = decoded
    try:
        path = urlsplit(decoded).path or decoded
    except Exception:
        pass
    basename = os.path.basename(path)
    variants = {raw, decoded, path, basename}
    return {variant for variant in variants if variant}


def _reference_allowed_by_runtime(
    allowed_reference_urls: Iterable[Any] | None,
    ref_url: Any,
) -> bool:
    return runtime_reference_allowed_by_artifacts(allowed_reference_urls, ref_url)


def _reference_selected_by_user(source_text: Any, ref_url: Any) -> bool:
    """Return True for user-attached, KB-selected, or #selected files."""
    text = str(source_text or "")
    if not text:
        return False
    lowered = text.lower()
    variants = _reference_url_variants(ref_url)
    for line in lowered.splitlines():
        if any(marker in line for marker in _REFERENCE_MARKERS):
            for variant in variants:
                needle = variant.lower()
                if needle and needle in line:
                    return True
    for variant in variants:
        needle = variant.strip()
        if not needle:
            continue
        if (
            needle.startswith("/api/v1/fs/") or needle.startswith("http://") or needle.startswith("https://")
        ) and needle.lower() in lowered:
            return True
        if re.search(rf"#\s*{re.escape(needle)}(?=$|[\s,，。；;])", text, re.IGNORECASE):
            return True
    return False


def _media_reference_explicitly_requested(source_text: Any, ref_url: Any, *, media_kind: str) -> bool:
    del media_kind
    return _reference_selected_by_user(source_text, ref_url)


def _filter_unrequested_media_references(
    *,
    source_text: Any,
    reference_video_urls: list[str] | None = None,
    audio_reference_urls: list[str] | None = None,
    allowed_reference_urls: Iterable[Any] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Drop video/audio references that are not explicit in the user request."""
    kept_video: list[str] = []
    kept_audio: list[str] = []
    omitted: list[str] = []

    for ref in reference_video_urls or []:
        if _media_reference_explicitly_requested(source_text, ref, media_kind="video") or _reference_allowed_by_runtime(
            allowed_reference_urls, ref
        ):
            kept_video.append(ref)
        else:
            omitted.append("reference_video_urls")

    for ref in audio_reference_urls or []:
        if _media_reference_explicitly_requested(source_text, ref, media_kind="audio") or _reference_allowed_by_runtime(
            allowed_reference_urls, ref
        ):
            kept_audio.append(ref)
        else:
            omitted.append("audio_reference_urls")

    return kept_video, kept_audio, sorted(set(omitted))


def _filter_unmentioned_visual_references(
    *,
    source_text: Any,
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_urls: list[str] | None = None,
    allowed_reference_urls: Iterable[Any] | None = None,
) -> tuple[str, str, list[str], list[str]]:
    """Drop visual references that were not attached, URL-selected, or #selected."""
    omitted: list[str] = []
    kept_first = first_frame_url
    kept_last = last_frame_url
    kept_refs: list[str] = []

    if kept_first and not (
        _reference_selected_by_user(source_text, kept_first)
        or _reference_allowed_by_runtime(allowed_reference_urls, kept_first)
    ):
        kept_first = ""
        omitted.append("first_frame_url")
    if kept_last and not (
        _reference_selected_by_user(source_text, kept_last)
        or _reference_allowed_by_runtime(allowed_reference_urls, kept_last)
    ):
        kept_last = ""
        omitted.append("last_frame_url")

    for ref in reference_urls or []:
        if _reference_selected_by_user(source_text, ref) or _reference_allowed_by_runtime(allowed_reference_urls, ref):
            kept_refs.append(ref)
        else:
            omitted.append("reference_urls")

    return kept_first, kept_last, kept_refs, sorted(set(omitted))


def _apply_inline_video_references(
    *,
    prompt: Any,
    active_user_message: Any,
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_urls: list[str] | None = None,
) -> tuple[str, str, list[str], dict[str, Any] | None]:
    """Recover video reference URLs from inline chat attachment markers.

    The LLM sees uploaded/Knowledge images as both multimodal image blocks and
    text markers. This helper is a tool-level safety net: if the model calls
    ``generate_video`` or ``generate_file(kind="video")`` without copying the
    marker URLs into args, we infer the safest mapping from the user's wording.
    """
    reference_source_text = str(active_user_message or prompt or "")
    intent_text = "\n".join(str(part or "") for part in (active_user_message, prompt) if str(part or "").strip())
    inline_refs = _extract_inline_image_reference_urls(reference_source_text)
    if not inline_refs:
        return first_frame_url, last_frame_url, list(reference_urls or []), None

    refs = list(reference_urls or [])

    def add_ref(url: str) -> None:
        used = {first_frame_url, last_frame_url, *refs}
        if url and url not in used:
            refs.append(url)

    normalized_text = " ".join(intent_text.lower().split())
    wants_start_end = _video_start_end_frame_intent(normalized_text)
    wants_start = _video_start_frame_intent(normalized_text)
    wants_end = _video_end_frame_intent(normalized_text)
    wants_reference = _video_reference_intent(normalized_text)
    original = {
        "first_frame_url": first_frame_url,
        "last_frame_url": last_frame_url,
        "reference_urls": list(refs),
    }

    if not first_frame_url and not last_frame_url and not refs:
        if wants_start_end and len(inline_refs) >= 2:
            first_frame_url = inline_refs[0]
            last_frame_url = inline_refs[1]
            for url in inline_refs[2:]:
                add_ref(url)
        elif wants_end and not wants_start and len(inline_refs) == 1:
            last_frame_url = inline_refs[0]
        elif wants_reference:
            for url in inline_refs:
                add_ref(url)
        elif len(inline_refs) == 1:
            first_frame_url = inline_refs[0]
        else:
            for url in inline_refs:
                add_ref(url)
    else:
        remaining = [url for url in inline_refs if url not in {first_frame_url, last_frame_url, *refs}]
        if not first_frame_url and wants_start and remaining:
            first_frame_url = remaining.pop(0)
        if not last_frame_url and (wants_end or wants_start_end) and remaining:
            last_frame_url = remaining.pop(0)
        for url in remaining:
            add_ref(url)

    inferred: dict[str, Any] = {
        "source": "active_user_message_inline_images",
        "inline_urls": inline_refs,
    }
    if first_frame_url and first_frame_url != original["first_frame_url"]:
        inferred["first_frame_url"] = first_frame_url
    if last_frame_url and last_frame_url != original["last_frame_url"]:
        inferred["last_frame_url"] = last_frame_url
    added_refs = [url for url in refs if url not in original["reference_urls"]]
    if added_refs:
        inferred["reference_urls"] = added_refs

    if len(inferred) == 2:
        return first_frame_url, last_frame_url, refs, None
    return first_frame_url, last_frame_url, refs, inferred


def _video_error_result(message: str, *, prompt: str = "", model: str = "") -> str:
    result: dict[str, Any] = {
        "kind": "video",
        "status": "failed",
        "error": message,
    }
    if prompt:
        result["prompt"] = prompt
    if model:
        result["model"] = model
    return json.dumps(result, ensure_ascii=False)


def _truthy_video_option(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _seedance_openrouter_native_only_inputs(
    *,
    provider: str,
    api_key: str,
    reference_video_urls: list[str] | None = None,
    audio_reference_urls: list[str] | None = None,
    audio_reference_url: str = "",
    generate_audio: Any = None,
) -> list[str]:
    """Return Seedance-native inputs that cannot be sent through OpenRouter."""

    if (provider or "").lower() != "bytedance" or not (api_key or "").startswith("sk-or-"):
        return []

    unsupported: list[str] = []
    if any(str(ref or "").strip() for ref in (reference_video_urls or [])):
        unsupported.append("reference_video_urls")
    if any(str(ref or "").strip() for ref in (audio_reference_urls or [])) or str(audio_reference_url or "").strip():
        unsupported.append("audio_reference_urls")
    if _truthy_video_option(generate_audio):
        unsupported.append("generate_audio")
    return unsupported


def _seedance_openrouter_downgrade_warning(unsupported_inputs: list[str]) -> str:
    if not unsupported_inputs:
        return ""
    unsupported_text = ", ".join(unsupported_inputs)
    return (
        "Seedance video/audio references and native generated audio are only available "
        "through Manor's official Volcengine/Seedance route. Current credentials resolve "
        f"to OpenRouter, so {unsupported_text} were omitted and the clip was generated "
        "as a silent picture clip with any supported image, first-frame, or last-frame "
        "references that remain."
    )


def _prompt_requests_video_post_asset(prompt: str) -> str:
    """Detect requests that should be handled after silent video generation."""
    text = " ".join(str(prompt or "").lower().split())
    if not text:
        return ""

    checks = (
        ("narration", ("narration", "voiceover", "voice-over", "旁白", "配音")),
        ("dialogue", ("dialogue", "spoken line", "spoken audio", "speech", "对白", "台词", "说话")),
        ("music", ("music", "bgm", "score", "soundtrack", "音乐", "背景音乐", "配乐")),
        (
            "sfx",
            (
                "sound effect",
                "sound effects",
                "sfx",
                "foley",
                "ambience",
                "ambient sound",
                "音效",
                "拟音",
                "环境声",
                "背景音",
            ),
        ),
        ("subtitles", ("subtitle", "subtitles", "caption", "captions", "字幕")),
    )
    intent_verbs = (
        "add",
        "include",
        "generate",
        "create",
        "with",
        "has",
        "needs",
        "produce",
        "overlay",
        "burn in",
        "添加",
        "加入",
        "生成",
        "加上",
        "配上",
        "带有",
        "需要",
        "要有",
        "烧录",
    )
    for asset, terms in checks:
        for term in terms:
            idx = text.find(term)
            if idx == -1:
                continue
            before = text[max(0, idx - 24) : idx]
            if re.search(r"(?:\bno\b|\bwithout\b|\bnot\b|\bnever\b)[\w\s-]{0,20}$", before):
                continue
            if any(cue in before[-8:] for cue in ("不要", "不加", "无", "没有", "禁止")):
                continue
            window = text[max(0, idx - 24) : idx + len(term) + 24]
            if any(verb in window for verb in intent_verbs):
                return asset
    return ""


_VIDEO_SILENT_AUDIO_POLICY = (
    "Audio/output constraints: silent picture only. Do not generate or include "
    "music, background music, score, ambience, sound effects, narration, "
    "voiceover, audible dialogue, vocals, lyrics, subtitles, captions, "
    "on-screen text, or lettering. Final dialogue, BGM, ambience, SFX, and "
    "subtitles will be generated and mixed as separate post-production tracks."
)

_VIDEO_NATIVE_DIALOGUE_AUDIO_POLICY = (
    "Audio/output constraints: native video audio is enabled. Generate only "
    "audio that matches the prompt and visible action; when audio references "
    "are supplied, follow their timing/performance. Do not generate or include "
    "subtitles, captions, on-screen text, or lettering. Prefer restrained "
    "in-scene sound/dialogue over unrelated BGM, vocals, or lyric fragments."
)


def _video_post_production_warning(post_asset: str) -> str:
    """Return a user-facing warning for media that belongs in post."""

    if not post_asset:
        return ""
    if post_asset == "subtitles":
        return (
            "The prompt asks for subtitles/captions. generate_video will create the clean picture clip only; "
            "use align_subtitles and compose_video_timeline to burn subtitles afterward."
        )
    if post_asset in {"music", "sfx"}:
        return (
            "The prompt asks for music, ambience, or sound effects. generate_video will create the clean "
            'picture clip only; create BGM/ambience/SFX as separate generate_file(kind="audio") tracks '
            "and mix them with compose_video_timeline."
        )
    return (
        f"The prompt asks for {post_asset}. generate_video will create the clean picture clip only; "
        'create dialogue/narration audio separately with generate_file(kind="audio") and mix it with '
        "compose_video_timeline."
    )


def _video_post_production_prompt_note(post_asset: str) -> str:
    """Return a provider-facing note that keeps the video clip visual-only."""

    if not post_asset:
        return ""
    if post_asset == "subtitles":
        label = "subtitles/captions"
    elif post_asset in {"music", "sfx"}:
        label = "music, ambience, or sound effects"
    else:
        label = post_asset
    return (
        f"Post-production note: the user also requested {label}; do not create it in this provider clip. "
        "Generate only the clean visual motion. Audio, subtitles, captions, and final soundtrack will be "
        "added later in post-production."
    )


def _apply_video_audio_policy_to_prompt(
    prompt: str,
    *,
    generate_audio: Any = None,
    audio_reference_urls: list[str] | None = None,
) -> str:
    """Append provider-facing audio constraints to every video prompt."""

    text = str(prompt or "").strip()
    if not text or "Audio/output constraints:" in text:
        return text
    uses_native_audio_flow = _truthy_video_option(generate_audio) or bool(audio_reference_urls)
    policy = _VIDEO_NATIVE_DIALOGUE_AUDIO_POLICY if uses_native_audio_flow else _VIDEO_SILENT_AUDIO_POLICY
    return f"{text}\n\n{policy}"


def _video_missing_reference_error(
    *,
    prompt: Any,
    active_user_message: Any = "",
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    audio_reference_urls: list[str] | None = None,
    requires_reference_media: Any = False,
) -> str | None:
    del prompt, active_user_message
    if any(
        (
            first_frame_url,
            last_frame_url,
            reference_urls,
            reference_video_urls,
            audio_reference_urls,
        )
    ):
        return None
    if not _truthy_video_option(requires_reference_media):
        return None
    return (
        "The video request marked reference media as required, but no media "
        "reference URL was passed to generate_video. Pass the actual "
        "file as first_frame_url, last_frame_url, reference_urls, "
        "reference_video_urls, or audio_reference_urls, or set "
        "requires_reference_media=false for text-to-video."
    )


def _video_native_audio_downgrade_warning(
    *,
    model: str,
    generate_audio: Any = None,
    audio_reference_urls: list[str] | None = None,
) -> str | None:
    """Silent video with a note, not a failure, when the model cannot speak.

    Native audio is opportunistic: the schema promises "provider audio when
    supported" and the chat composer turns it on for every video send. On a
    model without native audio, that DEFAULT used to fail the whole request
    with adapter jargon — "生成一个stickman视频" answered by "capability
    mismatch". Dropping the flag and saying so is the useful behavior.

    ``audio_reference_urls`` block the downgrade: those are files the user
    actually attached, so the capability validator's hard error (with its
    lip-sync guidance) is the honest answer there.
    """
    from packages.core.constants.models import video_model_capabilities

    if not _truthy_video_option(generate_audio):
        return None
    if audio_reference_urls:
        return None
    if video_model_capabilities(model).get("native_audio"):
        return None
    return (
        f"{model} cannot generate audio natively; producing a silent video instead. "
        "If the video needs sound, generate narration or music with generate_file "
        "kind=\"audio\" and combine them with compose_video_timeline."
    )


def _video_capability_error(
    *,
    model: str,
    prompt: str,
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    audio_reference_urls: list[str] | None = None,
    audio_reference_url: str = "",
    generate_audio: Any = None,
) -> str | None:
    from packages.core.constants.models import video_model_capabilities

    caps = video_model_capabilities(model)
    refs = [ref for ref in (reference_urls or []) if str(ref or "").strip()]
    video_refs = [ref for ref in (reference_video_urls or []) if str(ref or "").strip()]
    audio_refs = [ref for ref in (audio_reference_urls or []) if str(ref or "").strip()]
    if audio_reference_url and audio_reference_url not in audio_refs:
        audio_refs.append(audio_reference_url)
    problems: list[str] = []
    if caps.get("requires_first_frame") and not first_frame_url:
        problems.append(
            f"{model} is image-to-video only and requires a source image in "
            "first_frame_url. Generate or attach an image first and pass it as "
            "the first frame, or switch to a text-to-video model."
        )
    if first_frame_url and not caps.get("first_frame"):
        problems.append(f"{model} does not support first_frame_url.")
    if last_frame_url and not caps.get("last_frame"):
        problems.append(f"{model} does not support last_frame_url/end-frame control.")
    if refs and not caps.get("reference_images"):
        problems.append(f"{model} does not support reference_urls/reference images.")
    max_refs = int(caps.get("max_reference_images") or 0)
    if refs and caps.get("reference_images") and len(refs) > max_refs:
        problems.append(f"{model} supports at most {max_refs} reference image(s); received {len(refs)}.")
    if video_refs and not caps.get("reference_videos"):
        problems.append(f"{model} does not support reference_video_urls/reference videos.")
    max_video_refs = int(caps.get("max_reference_videos") or 0)
    if video_refs and caps.get("reference_videos") and len(video_refs) > max_video_refs:
        problems.append(f"{model} supports at most {max_video_refs} reference video(s); received {len(video_refs)}.")
    if audio_refs and not caps.get("audio_reference"):
        problems.append(
            f"{model} does not support audio_reference_url/audio_url conditioning "
            "through this adapter. Generate or reuse dialogue audio as a timing "
            "reference, prompt visible mouth movement, then compose the audio in post; "
            "use a dedicated lip-sync/audio-driven video route when exact sync is required."
        )
    max_audio_refs = int(caps.get("max_audio_references") or 0)
    if audio_refs and caps.get("audio_reference") and len(audio_refs) > max_audio_refs:
        problems.append(
            f"{model} supports at most {max_audio_refs} reference audio file(s); received {len(audio_refs)}."
        )
    if audio_refs and caps.get("audio_reference") and not (first_frame_url or last_frame_url or refs or video_refs):
        problems.append(
            f"{model} audio references should be paired with at least one image or video reference "
            "so the model has a visual subject/environment to condition."
        )
    native_audio_requested = generate_audio is not None and _truthy_video_option(generate_audio)
    if native_audio_requested and not caps.get("native_audio"):
        problems.append(f"{model} does not support native video audio through this adapter.")

    if not problems:
        return None

    supported = []
    if caps.get("first_frame"):
        supported.append("first_frame_url")
    if caps.get("last_frame"):
        supported.append("last_frame_url")
    if caps.get("reference_images"):
        supported.append(f"reference_urls up to {max_refs}")
    if caps.get("reference_videos"):
        supported.append(f"reference_video_urls up to {max_video_refs}")
    if caps.get("audio_reference"):
        supported.append(f"audio_reference_urls up to {max_audio_refs}")
    if caps.get("native_audio"):
        supported.append("native_audio")
    supported_text = ", ".join(supported) if supported else "text-to-video only"
    return (
        "Selected video model capability mismatch: " + " ".join(problems) + f" Supported by {model}: {supported_text}. "
        "Switch the video model, remove unsupported parameters, or split the request into "
        "silent video generation plus audio/subtitle composition."
    )


def _runtime_https_public_base_url() -> str:
    try:
        from packages.core.config import get_settings

        public_base = (get_settings().PUBLIC_BASE_URL or "").rstrip("/")
    except Exception:
        logger.debug("Video public base URL lookup failed", exc_info=True)
        return ""
    return public_base if public_base.startswith("https://") else ""


def _video_reference_public_base_error(
    references: list[str],
    entity_id: str,
) -> str | None:
    """Return an actionable error when local video references cannot be signed.

    Text-to-video and already-public URLs do not need Manor to publish a
    temporary signed file URL. Local Knowledge/upload references do.
    """
    refs = [str(ref or "").strip() for ref in references if str(ref or "").strip()]
    if not refs or not entity_id:
        return None

    try:
        from packages.core.services.entity_fs import get_entity_root
        from packages.core.tasks.media_tasks import _entity_rel_path_from_reference
    except Exception:
        logger.debug("Video reference public URL preflight import failed", exc_info=True)
        return None

    try:
        entity_root = get_entity_root(entity_id)
        has_local_reference = any(_entity_rel_path_from_reference(ref, entity_id, entity_root) for ref in refs)
    except Exception:
        logger.debug("Video reference public URL preflight skipped", exc_info=True)
        return None

    if not has_local_reference:
        return None

    if _runtime_https_public_base_url():
        return None

    return (
        "Media references require a provider-readable HTTPS URL. "
        "Set PUBLIC_BASE_URL to an externally reachable HTTPS base URL and "
        "restart both the API and worker so Manor can create "
        "/api/v1/fs/public/{token} signed media URLs. "
        "For local development, use text-to-video, use an already public "
        "https:// reference, or expose this Manor API through an HTTPS tunnel."
    )


async def _validate_video_reference_urls_fetchable(
    *,
    entity_id: str,
    references: list[str],
    public_base_url: str,
) -> None:
    """Fail before job creation when local video references cannot be fetched."""
    refs = [str(ref or "").strip() for ref in references if str(ref or "").strip()]
    if not refs:
        return
    from packages.core.tasks.media_tasks import (
        MEDIA_REFERENCE_URL_EXPIRES_SECONDS,
        _ensure_public_url,
    )

    for ref in refs:
        await _ensure_public_url(
            ref,
            entity_id,
            allow_data_uri=False,
            public_base_url=public_base_url,
            expires_in_seconds=MEDIA_REFERENCE_URL_EXPIRES_SECONDS,
        )


def _video_references_requiring_public_urls(
    adapter: Any,
    *,
    model: str,
    first_frame_url: str = "",
    last_frame_url: str = "",
    reference_urls: list[str] | None = None,
    reference_video_urls: list[str] | None = None,
    audio_reference_urls: list[str] | None = None,
) -> list[str]:
    """Return only media kinds that the selected route cannot inline."""
    refs = [
        first_frame_url,
        last_frame_url,
        *(reference_urls or []),
        *(reference_video_urls or []),
        *(audio_reference_urls or []),
    ]
    resolver = getattr(adapter, "references_requiring_public_urls", None)
    if callable(resolver):
        return resolver(
            model,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            reference_urls=reference_urls,
            reference_video_urls=reference_video_urls,
            audio_reference_urls=audio_reference_urls,
        )

    # Compatibility for test doubles and third-party adapters that implement
    # only the older all-or-nothing inline-reference contract.
    supports_inline = getattr(adapter, "supports_inline_local_references", None)
    if callable(supports_inline) and supports_inline():
        return []
    return [str(ref or "") for ref in refs if str(ref or "").strip()]


async def _generate_video_handler(
    entity_id: str = "",
    user_id: str = "",
    **kwargs: Any,
) -> str:
    """Generate video asynchronously via a background job.

    Creates a MediaJob, schedules background processing, and returns
    immediately with a job_id placeholder. The frontend shows a pending
    card and receives a ``video_ready`` WebSocket event when done.
    """
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    user_id = await _resolve_media_task_user_id(
        _media_context_user_id(user_id, runtime_context.user_id),
        entity_id,
        kwargs.get("task_id") or runtime_context.task_id,
    )
    active_user_message = runtime_active_user_message_from_context(kwargs)
    raw_prompt = str(kwargs.get("prompt", "") or "").strip()
    prompt = raw_prompt
    output_name = str(kwargs.get("name") or kwargs.get("output_name") or kwargs.get("filename") or "").strip()
    first_frame_url = kwargs.get("first_frame_url", "") or kwargs.get("image_url", "")
    last_frame_url = kwargs.get("last_frame_url", "")
    reference_urls = _coerce_video_reference_urls(kwargs.get("reference_urls"), kwargs.get("reference_url"))
    reference_video_urls = _coerce_video_reference_urls(
        kwargs.get("reference_video_urls"),
        kwargs.get("reference_video_url"),
        kwargs.get("video_reference_urls"),
        kwargs.get("video_reference_url"),
        kwargs.get("video_url"),
    )
    audio_reference_urls = _coerce_video_reference_urls(
        kwargs.get("audio_reference_urls"),
        kwargs.get("audio_reference_url") or kwargs.get("reference_audio_url") or kwargs.get("audio_url"),
        kwargs.get("reference_audio_urls"),
    )
    audio_reference_url = audio_reference_urls[0] if audio_reference_urls else ""
    (
        first_frame_url,
        last_frame_url,
        reference_urls,
        inferred_inline_references,
    ) = _apply_inline_video_references(
        prompt=prompt,
        active_user_message=active_user_message,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_urls=reference_urls,
    )
    runtime_allowed_reference_urls = set(runtime_context.runtime_artifact_urls) | set(
        runtime_context.dependency_artifact_urls
    )
    source_text_for_reference_policy = str(active_user_message or "")
    unmentioned_visual_refs: list[str] = []
    unrequested_media_refs: list[str] = []
    if source_text_for_reference_policy.strip():
        (
            first_frame_url,
            last_frame_url,
            reference_urls,
            unmentioned_visual_refs,
        ) = _filter_unmentioned_visual_references(
            source_text=source_text_for_reference_policy,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            reference_urls=reference_urls,
            allowed_reference_urls=runtime_allowed_reference_urls,
        )
        (
            reference_video_urls,
            audio_reference_urls,
            unrequested_media_refs,
        ) = _filter_unrequested_media_references(
            source_text=source_text_for_reference_policy,
            reference_video_urls=reference_video_urls,
            audio_reference_urls=audio_reference_urls,
            allowed_reference_urls=runtime_allowed_reference_urls,
        )
    audio_reference_url = audio_reference_urls[0] if audio_reference_urls else ""
    raw_resolution = kwargs.get("resolution", "720p")
    aspect_ratio = kwargs.get("aspect_ratio", "16:9")
    seed = kwargs.get("seed")
    frames = kwargs.get("frames")
    generate_audio = kwargs.get("generate_audio")
    if generate_audio is None:
        generate_audio = True
    requires_reference_media = kwargs.get("requires_reference_media")
    if requires_reference_media is None:
        requires_reference_media = kwargs.get("reference_media_required")
    return_last_frame = kwargs.get("return_last_frame")
    camera_fixed = kwargs.get("camera_fixed")
    watermark = kwargs.get("watermark")
    draft = kwargs.get("draft")

    if not raw_prompt:
        return _video_error_result("prompt is required")

    model = await _resolve_user_video_model(user_id, entity_id)
    if kwargs.get("model"):
        model = str(kwargs["model"]).strip()
    provider = _catalog_provider(model)
    from packages.core.tasks.media_tasks import (
        VIDEO_DURATION_MAX_SECONDS,
        VIDEO_DURATION_MIN_SECONDS,
        normalize_video_resolution,
        normalize_video_duration,
        parse_video_duration,
        snapshot_video_reference_urls,
    )

    raw_duration = kwargs.get("duration", 5)
    requested_duration = parse_video_duration(raw_duration)
    duration = normalize_video_duration(raw_duration)
    duration_adjusted = requested_duration is not None and requested_duration != duration
    if requested_duration is not None and requested_duration > VIDEO_DURATION_MAX_SECONDS:
        return _video_error_result(
            (
                f"Single video generation supports only {VIDEO_DURATION_MIN_SECONDS}-"
                f"{VIDEO_DURATION_MAX_SECONDS}s clips. Requested {requested_duration}s. "
                "Do not start a shortened clip. Segment the total runtime into multiple "
                "clip jobs whose durations sum exactly to the requested duration, call "
                "wait_media_jobs, then merge_videos into one clean master."
            ),
            prompt=prompt,
            model=model,
        )
    requested_resolution = normalize_video_resolution(None, raw_resolution)
    resolution = normalize_video_resolution(model, raw_resolution)
    resolution_adjusted = requested_resolution != resolution

    missing_reference_error = _video_missing_reference_error(
        prompt=raw_prompt,
        active_user_message=active_user_message,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_urls=reference_urls,
        reference_video_urls=reference_video_urls,
        audio_reference_urls=audio_reference_urls,
        requires_reference_media=requires_reference_media,
    )
    if missing_reference_error:
        return _video_error_result(missing_reference_error, prompt=raw_prompt, model=model)

    route_preference = str(kwargs.get("route_provider") or "auto").strip().lower()
    if route_preference not in {"auto", "vercel", "native"}:
        return _video_error_result(
            f"Unsupported route_provider: {route_preference}",
            prompt=raw_prompt,
            model=model,
        )

    # Validate API key before reference preflight so OpenRouter fallback can
    # omit native-only Seedance reference inputs instead of failing on URLs the
    # selected route cannot use.
    api_key, _base_url_override, is_byok = await _resolve_user_media_credentials(user_id, entity_id, role="video")
    route_provider = provider
    vercel_auth_method = "api-key"
    if route_preference == "vercel" or (
        route_preference == "auto"
        and not is_byok
        and os.getenv("DEPLOYMENT_MODE", "oss").strip().lower() == "cloud"
    ):
        try:
            official_route = await _resolve_official_model_route(
                model,
                vercel_reason="media.video.vercel_gateway_key",
                gateway_provider="vercel",
            )
        except Exception:
            logger.debug("Managed Vercel video route lookup failed", exc_info=True)
            official_route = None
        if official_route and official_route.api_key:
            api_key = official_route.api_key
            _base_url_override = official_route.base_url
            route_provider = "vercel"
            is_byok = False
            route_provider = official_route.provider or "vercel"
            vercel_auth_method = (
                "oidc"
                if str(getattr(official_route, "source_detail", "")).strip()
                == "VERCEL_OIDC_TOKEN"
                else "api-key"
            )
        elif route_preference == "vercel":
            return _video_error_result(
                "Vercel AI Gateway credentials are unavailable for explicit routing.",
                prompt=raw_prompt,
                model=model,
            )
    if route_provider != "vercel":
        if not is_byok and provider in {"bytedance", "kwaivgi"}:
            native_key = await _platform_native_media_key_async(provider)
            if native_key:
                api_key = native_key
                is_byok = False
        else:
            api_key, is_byok = _prefer_native_video_credentials(api_key, provider, is_byok)
    if not api_key and route_provider != "vercel":
        api_key = await _platform_native_media_key_async(provider)
        is_byok = False
    if not api_key:
        return _video_error_result("No video generation API key configured", prompt=raw_prompt, model=model)
    if entity_id and not is_byok:
        await runtime_assert_credit_available(
            entity_id,
            source=RUNTIME_GENERATE_VIDEO_TOOL_SOURCE,
        )
    # Atlas Cloud models are BYOK-only: Manor holds no platform key and does
    # not proxy or bill these calls. Without a user-supplied Atlas key the
    # resolved credential would be Manor's OpenRouter default, which cannot
    # serve Atlas-hosted models.
    if provider == "atlascloud" and not is_byok:
        return _video_error_result(
            "This model requires your own Atlas Cloud API key. Save it as your "
            "video provider key in Settings → AI Models, or pick another video model.",
            prompt=raw_prompt,
            model=model,
        )
    mismatch = None if route_provider == "vercel" else _media_key_provider_mismatch(api_key, provider)
    if mismatch:
        return _video_error_result(mismatch, prompt=raw_prompt, model=model)

    route_warnings: list[str] = []
    if unmentioned_visual_refs or unrequested_media_refs:
        route_warnings.append("Ignored reference media that was not mentioned or explicitly requested by the user.")
    omitted_seedance_inputs = _seedance_openrouter_native_only_inputs(
        provider=provider,
        api_key=api_key,
        reference_video_urls=reference_video_urls,
        audio_reference_urls=audio_reference_urls,
        audio_reference_url=audio_reference_url,
        generate_audio=generate_audio,
    )
    if omitted_seedance_inputs:
        route_warnings.append(_seedance_openrouter_downgrade_warning(omitted_seedance_inputs))
        reference_video_urls = []
        audio_reference_urls = []
        audio_reference_url = ""
        generate_audio = False
    if route_provider == "vercel" and audio_reference_urls:
        route_warnings.append(
            "Vercel's v4 video protocol does not accept audio reference files; "
            "the references were omitted while native generate_audio remains enabled."
        )
        audio_reference_urls = []
        audio_reference_url = ""

    native_audio_downgrade = _video_native_audio_downgrade_warning(
        model=model,
        generate_audio=generate_audio,
        audio_reference_urls=audio_reference_urls,
    )
    if native_audio_downgrade:
        route_warnings.append(native_audio_downgrade)
        generate_audio = False

    from packages.core.tasks.video_adapters import (
        select_video_generation_adapter,
        video_adapter_metadata,
    )

    adapter = select_video_generation_adapter(
        model=model,
        provider=route_provider,
        api_key=api_key,
    )
    if not adapter:
        return _video_error_result(
            (
                f"No video adapter for {provider or 'this'} model. "
                "Use an OpenRouter key or choose a Seedance/Kling video model."
            ),
            prompt=raw_prompt,
            model=model,
        )
    adapter_meta = video_adapter_metadata(model, route_provider, api_key)

    public_url_references = _video_references_requiring_public_urls(
        adapter,
        model=model,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_urls=reference_urls,
        reference_video_urls=reference_video_urls,
        audio_reference_urls=audio_reference_urls,
    )
    if public_url_references:
        reference_error = _video_reference_public_base_error(
            public_url_references,
            entity_id,
        )
        if reference_error:
            return _video_error_result(reference_error, prompt=raw_prompt, model=model)

    capability_error = _video_capability_error(
        model=model,
        prompt=raw_prompt,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_urls=reference_urls,
        reference_video_urls=reference_video_urls,
        audio_reference_urls=audio_reference_urls,
        generate_audio=generate_audio,
    )
    if capability_error:
        return _video_error_result(capability_error, prompt=raw_prompt, model=model)

    workflow_notes: list[str] = []
    post_asset = _prompt_requests_video_post_asset(raw_prompt)
    post_warning = _video_post_production_warning(post_asset)
    if post_warning:
        route_warnings.append(post_warning)
        workflow_notes.append(_video_post_production_prompt_note(post_asset))
    if route_warnings:
        if omitted_seedance_inputs:
            workflow_notes.append(_seedance_openrouter_downgrade_warning(omitted_seedance_inputs))
        workflow_notes = [note for note in workflow_notes if note]
        prompt = raw_prompt
        if workflow_notes:
            prompt = f"{raw_prompt}\n\n" + "\n\n".join(workflow_notes)
    prompt = _apply_video_audio_policy_to_prompt(
        prompt,
        generate_audio=generate_audio,
        audio_reference_urls=audio_reference_urls,
    )

    # Estimate credits only for platform-routed calls. BYOK is billed by the
    # vendor directly and should show zero Manor credits.
    credits_estimate = 0
    if not is_byok:
        try:
            from packages.core.services.billing_service import video_to_credits

            credits_estimate = video_to_credits(
                model, duration, resolution,
                with_audio=bool(generate_audio),
                has_video_input=bool(reference_video_urls),
            )
        except Exception:
            pass

    # Prefer trusted runtime chat context, then fall back to billing context.
    conversation_id = runtime_context.conversation_id
    try:
        from packages.core.ai.runtime import runtime_current_billing_context

        billing = runtime_current_billing_context()
        if billing and billing.conversation_id:
            conversation_id = billing.conversation_id
    except Exception:
        pass

    # Create the job
    from packages.core.database import async_session
    from packages.core.models.media_job import MediaJob
    from packages.core.models.base import generate_ulid

    job_id = generate_ulid()
    try:
        snap = snapshot_video_reference_urls(
            entity_id=entity_id,
            job_id=job_id,
            first_frame_url=first_frame_url or "",
            last_frame_url=last_frame_url or "",
            reference_urls=reference_urls,
            reference_video_urls=reference_video_urls,
            audio_reference_urls=audio_reference_urls,
        )
        first_frame_url = snap["first_frame_url"]
        last_frame_url = snap["last_frame_url"]
        reference_urls = snap["reference_urls"]
        reference_video_urls = snap["reference_video_urls"]
        audio_reference_urls = snap["audio_reference_urls"]
        audio_reference_url = audio_reference_urls[0] if audio_reference_urls else ""
    except Exception as exc:
        logger.warning("generate_video reference snapshot failed: %s", exc, exc_info=True)
        return _video_error_result(str(exc), prompt=raw_prompt, model=model)

    public_base_url = _runtime_https_public_base_url()
    public_url_references = _video_references_requiring_public_urls(
        adapter,
        model=model,
        first_frame_url=first_frame_url,
        last_frame_url=last_frame_url,
        reference_urls=reference_urls,
        reference_video_urls=reference_video_urls,
        audio_reference_urls=audio_reference_urls,
    )
    if public_url_references:
        try:
            await _validate_video_reference_urls_fetchable(
                entity_id=entity_id,
                references=public_url_references,
                public_base_url=public_base_url,
            )
        except Exception as exc:
            logger.warning("generate_video reference URL validation failed: %s", exc, exc_info=True)
            return _video_error_result(str(exc), prompt=raw_prompt, model=model)

    async with async_session() as db:
        video_params = {
            "duration": duration,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            **adapter_meta,
            "billing_mode": "byok" if is_byok else "platform",
            "credential_source": "byok" if is_byok else "platform",
            "route_preference": route_preference,
            "first_frame_url": first_frame_url or None,
            "last_frame_url": last_frame_url or None,
            "reference_urls": reference_urls[:9] if reference_urls else None,
            "reference_video_urls": reference_video_urls[:3] if reference_video_urls else None,
            "audio_reference_urls": audio_reference_urls[:3] if audio_reference_urls else None,
            "audio_reference_url": audio_reference_url or None,
            "seed": seed,
        }
        if route_provider == "vercel":
            video_params["vercel_auth_method"] = vercel_auth_method
        if duration_adjusted:
            video_params["requested_duration"] = requested_duration
        if resolution_adjusted:
            video_params["requested_resolution"] = requested_resolution
        if output_name:
            video_params["output_name"] = output_name
        if runtime_context.workspace_id:
            video_params["workspace_id"] = runtime_context.workspace_id
        if runtime_context.task_id:
            video_params["task_id"] = runtime_context.task_id
        if inferred_inline_references:
            video_params["inferred_inline_references"] = inferred_inline_references
        if prompt != raw_prompt:
            video_params["source_prompt"] = raw_prompt
            video_params["audio_policy"] = (
                "native_dialogue_reference_only"
                if (_truthy_video_option(generate_audio) or audio_reference_urls)
                else "silent_picture_only"
            )
        if route_warnings:
            video_params["route_warnings"] = route_warnings
            if omitted_seedance_inputs:
                video_params["omitted_seedance_inputs"] = omitted_seedance_inputs
            if post_asset:
                video_params["post_production_intent"] = post_asset
        for key, value in (
            ("frames", frames),
            ("generate_audio", generate_audio),
            ("return_last_frame", return_last_frame),
            ("camera_fixed", camera_fixed),
            ("watermark", watermark),
            ("draft", draft),
        ):
            if value is not None:
                video_params[key] = value
        if public_base_url:
            video_params["public_base_url"] = public_base_url

        job = MediaJob(
            id=job_id,
            entity_id=entity_id,
            user_id=user_id or None,
            agent_id=runtime_context.agent_id,
            conversation_id=conversation_id,
            kind="video",
            status="pending",
            prompt=prompt,
            model=model,
            params=video_params,
            duration_seconds=duration,
            credits=0 if is_byok else int(credits_estimate or 0),
            byok=is_byok,
        )
        db.add(job)
        await db.flush()
        if entity_id and not is_byok:
            try:
                from packages.core.services.credit_reservations import (
                    CreditReservationError,
                    reserve_credits,
                )

                await reserve_credits(
                    db,
                    entity_id=entity_id,
                    amount_credits=int(credits_estimate or 0),
                    source_kind="media_job",
                    source_id=job_id,
                    reason="video generation estimate",
                    workspace_id=runtime_context.workspace_id,
                    agent_id=runtime_context.agent_id,
                    conversation_id=conversation_id,
                    user_id=user_id or None,
                    metadata={
                        "model": model,
                        "duration": duration,
                        "resolution": resolution,
                        "tool_name": "generate_video",
                        "billing_mode": "platform",
                        "credential_source": "platform",
                    },
                )
            except CreditReservationError as exc:
                await db.rollback()
                return _video_error_result(str(exc), prompt=raw_prompt, model=model)
        await db.commit()

    # Schedule background processing
    from packages.core.tasks.media_tasks import schedule_video_job

    schedule_video_job(job_id)

    message = f"Video generation started. {duration}s {resolution} video will be ready in 30-90 seconds."
    if duration_adjusted or resolution_adjusted:
        requested_bits = []
        if duration_adjusted:
            requested_bits.append(f"{requested_duration}s")
        if resolution_adjusted:
            requested_bits.append(requested_resolution)
        message = (
            f"Requested {' '.join(requested_bits)}, but the selected video model supports "
            f"{VIDEO_DURATION_MIN_SECONDS}-{VIDEO_DURATION_MAX_SECONDS}s and not every resolution. "
            f"Starting a {duration}s {resolution} video instead."
        )

    # Return immediately with placeholder — frontend renders a pending VideoCard
    result = {
        "kind": "video",
        "status": "pending",
        "job_id": job_id,
        "prompt": prompt,
        "name": output_name,
        "duration": duration,
        "resolution": resolution,
        "model": model,
        "credits_estimate": credits_estimate,
        "message": message,
    }
    if first_frame_url:
        result["first_frame_url"] = first_frame_url
    if last_frame_url:
        result["last_frame_url"] = last_frame_url
    if reference_urls:
        result["reference_urls"] = reference_urls[:9]
    if reference_video_urls:
        result["reference_video_urls"] = reference_video_urls[:3]
    if audio_reference_urls:
        result["audio_reference_urls"] = audio_reference_urls[:3]
        result["audio_reference_url"] = audio_reference_urls[0]
    if inferred_inline_references:
        result["inferred_inline_references"] = inferred_inline_references
    if duration_adjusted:
        result["requested_duration"] = requested_duration
    if resolution_adjusted:
        result["requested_resolution"] = requested_resolution
    if route_warnings:
        result["warnings"] = route_warnings
        if omitted_seedance_inputs:
            result["omitted_seedance_inputs"] = omitted_seedance_inputs
        if post_asset:
            result["post_production_intent"] = post_asset
    return json.dumps(result)


# ── Registration ─────────────────────────────────────────────────────────────


def get_tools():
    return [
        (WEB_FETCH_SCHEMA, _web_fetch_handler),
        (EXTRACT_DATA_SCHEMA, _extract_data_handler),
        (GENERATE_IMAGE_SCHEMA, _generate_image_handler),
        (GENERATE_VIDEO_SCHEMA, _generate_video_handler),
        (TRANSCRIBE_AUDIO_SCHEMA, _transcribe_audio_handler),
    ]


# ── Billing for media generation ────────────────────────────────────


def _estimate_image_cost(model: str, size: str = "1024x1024") -> float:
    """USD cost for one image. Falls back to $0.04 (mid-tier rate)."""
    from packages.core.services.model_pricing_gateway import estimate_image_cost_usd

    return estimate_image_cost_usd(model, size=size)


def _estimate_audio_cost(model: str, *, purpose: str = "") -> float:
    """Best-effort USD cost for one generated audio asset."""
    from packages.core.services.model_pricing_gateway import estimate_audio_cost_usd

    return estimate_audio_cost_usd(model, purpose=purpose)


async def _bill_media(
    *,
    entity_id: str,
    user_id: str,
    kind: str,
    model: str,
    cost_usd: float,
    units: int,
    byok: bool = False,
) -> None:
    """Record a media-generation call (image / video) against the
    entity. Unlike embedding/TTS this path doesn't depend on the LLM
    billing context — the tool already has ``entity_id`` from the Runtime
    Harness tool execution context.
    """
    if not entity_id or cost_usd <= 0:
        return
    try:
        from packages.core.database import async_session
        from packages.core.services.usage_service import record_media_usage

        async with async_session() as db:
            await record_media_usage(
                db,
                entity_id=entity_id,
                kind=kind,
                model=model,
                cost_usd=float(cost_usd),
                units=units,
                user_id=user_id or None,
                source=f"tool:{kind}",
                byok=byok,
            )
            await db.commit()
    except Exception:
        logger.debug("media billing failed (best-effort)", exc_info=True)
