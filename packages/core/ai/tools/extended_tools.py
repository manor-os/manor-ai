"""
Extended tools — web_fetch, extract_data, generate_image, generate_video.

Ported from manor-multi-agent's runtime/extended_tools.py.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from html import unescape
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote, urlsplit

from packages.core.ai.runtime import (
    RUNTIME_GENERATE_AUDIO_TOOL_SOURCE,
    RUNTIME_GENERATE_IMAGE_TOOL_SOURCE,
    runtime_execute_extract_data_tool_completion,
    runtime_assert_credit_available,
)
from packages.core.ai.runtime.tool_context import (
    runtime_active_user_message_from_context,
    runtime_tool_call_context_from_kwargs,
)
from packages.core.ai.runtime.artifacts import runtime_reference_allowed_by_artifacts

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
    if user_text and user_text != "ai-agent":
        return user_text
    task_text = str(task_id or "").strip()
    entity_text = str(entity_id or "").strip()
    if not task_text or not entity_text:
        return ""
    try:
        from packages.core.ai.runtime import runtime_task_billable_user_id
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
            return runtime_task_billable_user_id(task) or ""
    except Exception:
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


def _catalog_provider(model: str) -> str:
    return (model or "").split("/", 1)[0].strip().lower() if "/" in (model or "") else ""


def _native_media_model(model: str, *, kind: str, provider: str) -> str:
    """Map Manor/OpenRouter catalog IDs to native provider model IDs."""
    raw = (model or "").split("/", 1)[1] if "/" in (model or "") else (model or "")
    image_map = {
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
        return (
            "The saved video API key looks like a Volcengine/Seedance key, "
            "but the selected video model is Kling. Select a Seedance model, "
            "clear the video provider key to use Manor credits, or save a Kling API key."
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
        return pcm_bytes
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
    if include_fs_path:
        payload["fs_path"] = _fs_path_from_result_url(image_url, entity_id)
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
        client = SandboxClient(base_url=sandbox_url, timeout=120.0)
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
) -> str:
    """Persist an AI-generated image and optionally register it as a document."""
    import base64

    if not entity_id:
        return f"data:{mime or 'image/png'};base64,{base64.b64encode(image_bytes).decode('ascii')}"

    ext = _image_mime_to_ext(mime)

    from packages.core.services.entity_fs import get_entity_root, write_entity_file_atomic
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
        default_dir=workspace_artifact_default_dir(workspace_base_dir, "images"),
        entity_root=entity_root,
    )
    filename = target.filename
    filepath = write_entity_file_atomic(
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
        from packages.core.services.knowledge_sync import ensure_folder_path

        factory = create_worker_session()
        async with factory() as db:
            folder_id = await ensure_folder_path(entity_id, target.rel_dir)
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


async def _resolve_user_audio_model(
    user_id: str,
    entity_id: str,
    *,
    purpose: str,
) -> tuple[str, str]:
    """Resolve the Account-selected OpenRouter audio model.

    ``voice`` is for speech/narration/dialogue. ``audio`` is for music.
    ``sfx`` is for ambience, Foley, transitions, and discrete sound effects.
    """
    from packages.core.services.model_resolver import resolve_model_for_user

    normalized = (purpose or "speech").strip().lower()
    if normalized in {"speech", "voice", "tts", "dialogue", "narration"}:
        role = "voice"
        fallback = "google/gemini-3.1-flash-tts-preview"
    elif normalized in {
        "sfx",
        "sound_effect",
        "sound-effect",
        "foley",
        "ambience",
        "ambient",
        "soundscape",
        "background",
        "background_bed",
        "bed",
        "transition",
    }:
        role = "sfx"
        fallback = "openai/gpt-audio-mini"
    else:
        role = "audio"
        fallback = "google/lyria-3-clip-preview"
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
    if lowered.startswith("zyphra/"):
        return "random"
    return ""


def _is_speech_response_audio_model(model: str) -> bool:
    """Return true for audio-output chat models that speak their response."""

    lowered = (model or "").strip().lower()
    return lowered.startswith("openai/gpt-audio") or lowered.startswith("openai/gpt-4o-audio")


def _unsupported_nonvoice_audio_payload(model: str, purpose: str, role: str) -> dict[str, Any]:
    return {
        "kind": "audio",
        "status": "error",
        "code": "unsupported_nonvoice_audio_model",
        "model": model,
        "purpose": purpose,
        "role": role,
        "error": (
            f"{model} is routed as a speech/conversational audio model here, "
            "not a reliable music, ambience, Foley, or SFX generator. "
            "Use a dedicated sound/music model or an approved uploaded/library stem; "
            "do not mix this output as non-voice audio."
        ),
    }


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
    output_name: str = "",
    workspace_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Persist generated audio and register it as a Knowledge document."""
    import base64

    mime = _audio_format_to_mime(audio_format)
    if not entity_id:
        return f"data:{mime};base64,{base64.b64encode(audio_bytes).decode('ascii')}"

    ext = _audio_format_to_ext(audio_format)

    from packages.core.services.entity_fs import get_entity_root, write_entity_file_atomic
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
        default_dir=workspace_artifact_default_dir(workspace_base_dir, "audio"),
        entity_root=entity_root,
    )
    filename = target.filename
    filepath = write_entity_file_atomic(
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
        from packages.core.services.knowledge_sync import ensure_folder_path

        factory = create_worker_session()
        async with factory() as db:
            folder_id = await ensure_folder_path(entity_id, target.rel_dir)
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


async def _openrouter_speech_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    voice: str,
    audio_format: str,
) -> bytes:
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "input": prompt,
        "response_format": audio_format,
    }
    if voice:
        payload["voice"] = voice
    async with httpx.AsyncClient(timeout=180.0) as client:
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
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenRouter speech generation failed ({resp.status_code}): {resp.text[:500]}")
        return resp.content


async def _google_speech_bytes(
    *,
    api_key: str,
    model: str,
    prompt: str,
    voice: str,
    base_url: str = "",
) -> bytes:
    """Generate speech with Gemini's native generateContent AUDIO API.

    Gemini TTS returns raw 24 kHz PCM in inlineData, so callers should store it
    as WAV for browser playback.
    """
    import base64
    import httpx

    native_model = _native_media_model(model, kind="audio", provider="google")
    endpoint = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    voice_name = (voice or "Zephyr").strip() or "Zephyr"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
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
    endpoint_base = (base_url or "https://api.zyphra.com/v1").strip().rstrip("/")
    endpoint = (
        endpoint_base if endpoint_base.endswith("/audio/text-to-speech") else f"{endpoint_base}/audio/text-to-speech"
    )
    payload: dict[str, Any] = {
        "text": prompt,
        "model": native_model,
        "mime_type": _zyphra_audio_mime(audio_format),
    }
    selected_voice = (voice or "").strip()
    if selected_voice and selected_voice.lower() != "random":
        payload["default_voice_name"] = selected_voice

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            endpoint,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Zyphra speech generation failed ({resp.status_code}): {resp.text[:500]}")
        if not resp.content:
            raise RuntimeError("Zyphra speech response did not include audio data.")
        return resp.content


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
    **kwargs: Any,
) -> str:
    """Generate an audio file through OpenRouter and save it to Knowledge."""
    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
    user_id = await _resolve_media_task_user_id(
        _media_context_user_id(user_id, runtime_context.user_id),
        entity_id,
        kwargs.get("task_id") or runtime_context.task_id,
    )
    prompt = str(kwargs.get("prompt") or "").strip()
    if not prompt:
        return json.dumps({"error": "prompt is required"})
    purpose = str(kwargs.get("purpose") or "speech").strip().lower()
    duration_seconds = _audio_duration_seconds(kwargs.get("duration_seconds") or kwargs.get("duration"))
    output_name = str(kwargs.get("name") or kwargs.get("output_name") or kwargs.get("filename") or "").strip()
    model, role = await _resolve_user_audio_model(user_id, entity_id, purpose=purpose)
    if kwargs.get("model"):
        model = str(kwargs["model"]).strip()
    provider = _catalog_provider(model)
    request_format, storage_format = _openrouter_audio_formats(
        model,
        role,
        str(kwargs.get("response_format") or kwargs.get("format") or ""),
    )
    voice = str(kwargs.get("voice") or _default_openrouter_voice(model)).strip()

    if role in {"audio", "sfx"} and _is_speech_response_audio_model(model):
        return json.dumps(
            _unsupported_nonvoice_audio_payload(model, purpose, role),
            ensure_ascii=False,
        )

    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(
        user_id,
        entity_id,
        role=role,
    )
    native_voice_provider = ""
    if role == "voice" and provider in {"google", "zyphra"}:
        if is_byok:
            if not _is_native_key_for_provider(api_key, provider):
                provider_label = "Google/Gemini" if provider == "google" else "Zyphra"
                return json.dumps(
                    {
                        "error": (
                            f"The selected {provider_label} TTS model requires a native "
                            f"{provider_label} API key. Save a matching key for Text-to-Speech, "
                            "or choose a matching model."
                        )
                    }
                )
            native_voice_provider = provider
        else:
            native_key, native_base_url = await _platform_native_media_credential_async(provider)
            if native_key:
                api_key = native_key
                base_url_override = native_base_url or base_url_override
                native_voice_provider = provider

    if not native_voice_provider:
        if not api_key or not api_key.startswith("sk-or-"):
            return json.dumps({"error": "Self-hosted audio generation requires a matching provider API key."})
    if entity_id and not is_byok:
        await runtime_assert_credit_available(
            entity_id,
            source=RUNTIME_GENERATE_AUDIO_TOOL_SOURCE,
        )

    try:
        if native_voice_provider == "google":
            request_format = "pcm"
            storage_format = "wav"
            audio_bytes = await _google_speech_bytes(
                api_key=api_key,
                model=model,
                prompt=prompt,
                voice=voice,
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
        elif role == "voice":
            audio_bytes = await _openrouter_speech_bytes(
                api_key=api_key,
                model=model,
                prompt=prompt,
                voice=voice,
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
        audio_url = await _save_generated_audio_bytes(
            entity_id=entity_id,
            user_id=user_id,
            prompt=prompt,
            model=model,
            purpose=purpose,
            audio_bytes=audio_bytes,
            audio_format=storage_format,
            is_byok=is_byok,
            output_name=output_name,
            workspace_id=kwargs.get("workspace_id"),
            task_id=kwargs.get("task_id"),
            agent_id=kwargs.get("agent_id"),
            conversation_id=kwargs.get("conversation_id"),
        )
        payload = {
            "kind": "audio",
            "status": "completed",
            "result_url": audio_url,
            "audio_url": audio_url,
            "fs_path": _fs_path_from_result_url(audio_url, entity_id),
            "prompt": prompt,
            "purpose": purpose,
            "model": model,
            "voice": voice or None,
            "format": storage_format,
            "provider_response_format": request_format,
            "duration_seconds": duration_seconds,
            "file_size": len(audio_bytes),
        }
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - tool should return structured errors
        logger.exception("OpenRouter audio generation failed")
        return json.dumps({"status": "error", "error": str(exc), "model": model, "purpose": purpose})


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
    loaded: list[tuple[str, bytes, str]] = []
    for ref in refs[:limit]:
        provider_url = await _image_reference_to_provider_url(ref, entity_id)
        loaded.append(await _load_image_reference_bytes(provider_url, entity_id))
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

    fallback = "openai/gpt-5-image-mini"
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


def _normalize_image_bytes_for_aspect_ratio(
    image_bytes: bytes,
    mime: str,
    aspect_ratio: str = "",
) -> tuple[bytes, str, str]:
    ratios = {
        "16:9": (16, 9),
        "9:16": (9, 16),
        "1:1": (1, 1),
    }
    target = ratios.get(str(aspect_ratio or "").strip())
    if not target:
        return image_bytes, mime, ""

    try:
        import io
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        width, height = image.size
        if width <= 0 or height <= 0:
            return image_bytes, mime, ""

        target_ratio = target[0] / target[1]
        current_ratio = width / height
        if abs(current_ratio - target_ratio) < 0.01:
            return image_bytes, mime, f"{width}x{height}"

        if current_ratio > target_ratio:
            new_width = max(1, round(height * target_ratio))
            left = max(0, (width - new_width) // 2)
            box = (left, 0, left + new_width, height)
        else:
            new_height = max(1, round(width / target_ratio))
            top = max(0, (height - new_height) // 2)
            box = (0, top, width, top + new_height)

        cropped = image.crop(box)
        output = io.BytesIO()
        if cropped.mode not in {"RGB", "RGBA"}:
            cropped = cropped.convert("RGBA")
        cropped.save(output, format="PNG")
        return output.getvalue(), "image/png", f"{cropped.size[0]}x{cropped.size[1]}"
    except Exception:
        logger.debug("image aspect-ratio normalization failed", exc_info=True)
        return image_bytes, mime, ""


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
    if not prompt:
        return json.dumps({"error": "prompt is required"})

    api_key, base_url_override, is_byok = await _resolve_user_media_credentials(user_id, entity_id, role="image")
    model = await _resolve_user_image_model(user_id, entity_id, api_key.startswith("sk-or-"))
    provider = _catalog_provider(model)
    if not is_byok and provider in {"openai", "google"}:
        native_key, native_base_url = await _platform_native_media_credential_async(provider)
        if native_key:
            api_key = native_key
            base_url_override = native_base_url or base_url_override
            is_byok = False
    if not api_key:
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
    if not is_openrouter and provider in {"openai", "google"}:
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
                data = resp.json()

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
                agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
                conversation_id=runtime_context.conversation_id,
                save_to_knowledge=save_to_knowledge,
                sandbox_path=sandbox_path,
            )
            return json.dumps(
                _image_result_payload(
                    image_url=image_url,
                    prompt=prompt,
                    size=size,
                    model=model,
                    entity_id=entity_id,
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
                    data = resp.json()
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
                    data = resp.json()

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
                agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
                conversation_id=runtime_context.conversation_id,
                save_to_knowledge=save_to_knowledge,
                sandbox_path=sandbox_path,
            )
            return json.dumps(
                _image_result_payload(
                    image_url=image_url,
                    prompt=prompt,
                    size=size,
                    model=model,
                    entity_id=entity_id,
                    include_fs_path=bool(kwargs.get("workspace_id")),
                    saved_to_knowledge=save_to_knowledge,
                )
            )

        if provider == "google":
            native_model = _native_media_model(model, kind="image", provider=provider)
            native_base_url = base_url_override or "https://generativelanguage.googleapis.com/v1beta"
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
                data = resp.json()

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
                    agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
                    conversation_id=runtime_context.conversation_id,
                    save_to_knowledge=save_to_knowledge,
                    sandbox_path=sandbox_path,
                )
                return json.dumps(
                    _image_result_payload(
                        image_url=image_url,
                        prompt=prompt,
                        size=size,
                        model=model,
                        entity_id=entity_id,
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
            "These media references are sent to external video providers, so local "
            "/api/v1/fs/... URLs only work when PUBLIC_BASE_URL is an externally "
            "reachable https:// base URL that can create signed public URLs. "
            "Without that, omit local references and use a self-contained text prompt. "
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
                    "description": "First frame image URL. Use an externally reachable https:// URL, or a local /api/v1/fs/... URL only when PUBLIC_BASE_URL is configured for signed public URLs.",
                },
                "last_frame_url": {
                    "type": "string",
                    "description": "Last frame image URL. Use an externally reachable https:// URL, or a local /api/v1/fs/... URL only when PUBLIC_BASE_URL is configured for signed public URLs.",
                },
                "reference_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference image URLs for style/character consistency. Use externally reachable https:// URLs, or local /api/v1/fs/... URLs only when PUBLIC_BASE_URL is configured for signed public URLs. Seedance supports up to 9.",
                },
                "reference_url": {
                    "type": "string",
                    "description": "Single reference image URL. Alias for reference_urls. Use an externally reachable https:// URL, or a local /api/v1/fs/... URL only when PUBLIC_BASE_URL is configured for signed public URLs.",
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

    # Validate API key before reference preflight so OpenRouter fallback can
    # omit native-only Seedance reference inputs instead of failing on URLs the
    # selected route cannot use.
    api_key, _base_url_override, is_byok = await _resolve_user_media_credentials(user_id, entity_id, role="video")
    if not is_byok and provider in {"bytedance", "kwaivgi"}:
        native_key = await _platform_native_media_key_async(provider)
        if native_key:
            api_key = native_key
            is_byok = False
    else:
        api_key, is_byok = _prefer_native_video_credentials(api_key, provider, is_byok)
    if not api_key:
        api_key = await _platform_native_media_key_async(provider)
        is_byok = False
    if not api_key:
        return _video_error_result("No video generation API key configured", prompt=raw_prompt, model=model)
    mismatch = _media_key_provider_mismatch(api_key, provider)
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

    reference_error = _video_reference_public_base_error(
        [first_frame_url, last_frame_url, *reference_urls, *reference_video_urls, *audio_reference_urls],
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
    from packages.core.tasks.video_adapters import (
        select_video_generation_adapter,
        video_adapter_metadata,
    )

    adapter = select_video_generation_adapter(model=model, provider=provider, api_key=api_key)
    if not adapter:
        return _video_error_result(
            (
                f"No video adapter for {provider or 'this'} model. "
                "Use an OpenRouter key or choose a Seedance/Kling video model."
            ),
            prompt=raw_prompt,
            model=model,
        )
    adapter_meta = video_adapter_metadata(model, provider, api_key)

    # Estimate credits only for platform-routed calls. BYOK is billed by the
    # vendor directly and should show zero Manor credits.
    credits_estimate = 0
    if not is_byok:
        try:
            from packages.core.services.billing_service import video_to_credits

            credits_estimate = video_to_credits(model, duration, resolution)
        except Exception:
            pass

    # Prefer explicit chat context, then fall back to billing context.
    conversation_id = kwargs.get("conversation_id") or None
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
    try:
        await _validate_video_reference_urls_fetchable(
            entity_id=entity_id,
            references=[
                first_frame_url or "",
                last_frame_url or "",
                *reference_urls,
                *reference_video_urls,
                *audio_reference_urls,
            ],
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
            "first_frame_url": first_frame_url or None,
            "last_frame_url": last_frame_url or None,
            "reference_urls": reference_urls[:9] if reference_urls else None,
            "reference_video_urls": reference_video_urls[:3] if reference_video_urls else None,
            "audio_reference_urls": audio_reference_urls[:3] if audio_reference_urls else None,
            "audio_reference_url": audio_reference_url or None,
            "seed": seed,
        }
        if duration_adjusted:
            video_params["requested_duration"] = requested_duration
        if resolution_adjusted:
            video_params["requested_resolution"] = requested_resolution
        if output_name:
            video_params["output_name"] = output_name
        if kwargs.get("workspace_id"):
            video_params["workspace_id"] = kwargs.get("workspace_id")
        if kwargs.get("task_id"):
            video_params["task_id"] = kwargs.get("task_id")
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
            agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
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
                    workspace_id=kwargs.get("workspace_id") or runtime_context.workspace_id,
                    agent_id=kwargs.get("agent_id") or runtime_context.agent_id,
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
