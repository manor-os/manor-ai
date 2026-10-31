"""Provider adapters for video generation jobs.

This module keeps provider-specific request shapes out of the media job
orchestration code. The orchestration layer owns database state, billing, and
artifact storage; adapters own provider routing and payload semantics.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, Awaitable, Callable, Literal, Protocol
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


# The gateway persists the full JSON body, where base64 expands image bytes by
# roughly one third. Leave room for the prompt and async operation envelope.
VERCEL_VIDEO_INLINE_REFERENCE_MAX_BYTES = 160 * 1024
# Below this much remaining allowance an image is sent by URL instead of being
# squeezed into an unusably small inline copy.
VERCEL_VIDEO_MIN_INLINE_REFERENCE_BYTES = 32 * 1024
# OpenRouter accepts data URLs for image_url payloads. Keep each local image
# bounded so multiple reference images cannot produce an unreasonably large
# video-start request.
OPENROUTER_VIDEO_INLINE_IMAGE_MAX_BYTES = 2 * 1024 * 1024


INLINE_BASE64_REFERENCE = "inline_base64"
SIGNED_URL_REFERENCE = "signed_url"


@dataclass(frozen=True)
class MediaReferenceCapability:
    """Allowed transport for one media kind on a video provider route."""

    transports: tuple[str, ...]
    max_inline_bytes: int | None = None

    @property
    def requires_public_url(self) -> bool:
        return self.transports == (SIGNED_URL_REFERENCE,)


@dataclass(frozen=True)
class VideoModelFile:
    """Provider-neutral file value modeled after AI SDK VideoModelV4File."""

    type: Literal["file", "url"]
    media_type: str
    value: str

    def as_vercel_file(self) -> dict[str, str]:
        key = "data" if self.type == "file" else "url"
        return {"type": self.type, key: self.value, "mediaType": self.media_type}

    def as_data_url(self) -> str:
        if self.type == "url":
            return self.value
        return f"data:{self.media_type};base64,{self.value}"

    def as_url(self) -> str:
        if self.type != "url":
            raise RuntimeError("This provider requires a publicly readable media URL.")
        return self.value


class InlineMediaReferenceLimitError(RuntimeError):
    """An image cannot fit within a provider's inline media payload limit."""


PUBLIC_URL_MEDIA_REFERENCE = MediaReferenceCapability((SIGNED_URL_REFERENCE,))
VERCEL_IMAGE_MEDIA_REFERENCE = MediaReferenceCapability(
    (INLINE_BASE64_REFERENCE, SIGNED_URL_REFERENCE),
    max_inline_bytes=VERCEL_VIDEO_INLINE_REFERENCE_MAX_BYTES,
)
OPENROUTER_IMAGE_MEDIA_REFERENCE = MediaReferenceCapability(
    (INLINE_BASE64_REFERENCE, SIGNED_URL_REFERENCE),
    max_inline_bytes=OPENROUTER_VIDEO_INLINE_IMAGE_MAX_BYTES,
)


VIDEO_NATIVE_MODEL_MAP = {
    "bytedance/seedance-2.5": "doubao-seedance-2-5-260628",
    "bytedance/seedance-2.0": "doubao-seedance-2-0-260128",
    "bytedance/seedance-2.0-fast": "doubao-seedance-2-0-fast-260128",
    "bytedance/seedance-v1.0-pro-fast": "seedance-1-0-pro-fast-251015",
    "kwaivgi/kling-v3.0-std": "kling-v3.0-std",
    "kwaivgi/kling-v3.0-pro": "kling-v3.0-pro",
    # Atlas Cloud model ids are three-part vendor/model/route paths.
    "atlascloud/wan-2.2-turbo-spicy": "atlascloud/wan-2.2-turbo-spicy/image-to-video",
}


def catalog_video_provider(model: str) -> str:
    return (model or "").split("/", 1)[0].strip().lower() if "/" in (model or "") else ""


def _vercel_video_wire_model(model: str, *, has_first_frame: bool) -> str:
    """Gateway model for one video request.

    Kling's gateway catalog splits text- and image-to-video into separate
    models; the product catalog has one Kling v3.0 entry per quality tier.
    Sending a first frame to the t2v model ignores the image.
    """
    if str(model).startswith("kwaivgi/kling-v3.0"):
        return "klingai/kling-v3.0-i2v" if has_first_frame else "klingai/kling-v3.0-t2v"
    from packages.core.services.model_provider_handlers import vercel_model_id

    return vercel_model_id(model)


def _vercel_kling_options(model: str, generate_audio: bool) -> dict[str, str]:
    """providerOptions.klingai for a catalog Kling model (per @ai-sdk/klingai).

    Std/Pro is a ``mode`` on one gateway model, and native ``sound`` exists
    only in pro mode.
    """
    pro = str(model).endswith("-pro")
    options = {"mode": "pro" if pro else "std"}
    if pro:
        options["sound"] = "on" if generate_audio else "off"
    return options


def native_video_model(model: str) -> str:
    raw = (model or "").split("/", 1)[1] if "/" in (model or "") else (model or "")
    return VIDEO_NATIVE_MODEL_MAP.get(model, raw)


# A provider that answers a submit with these statuses turned it away before
# accepting work, so submitting again cannot start a second paid generation.
# 500/502/504 stay final: a relay may have forwarded the job before failing.
SUBMIT_UNAVAILABLE_STATUSES = frozenset({429, 503})


class ProviderSubmitUnavailable(RuntimeError):
    """The provider refused a submit without accepting the job; it may be retried."""


def submit_error(status_code: int, message: str) -> dict[str, Any]:
    """Every adapter's failed submit: retryable refusals raise, the rest are final."""
    if status_code in SUBMIT_UNAVAILABLE_STATUSES:
        raise ProviderSubmitUnavailable(message)
    return {"error": message}


def provider_error_hint(status_code: int, message: str) -> str:
    """Generic, provider-agnostic hint for a failed provider HTTP call.

    Maps the recurring failure modes we see across providers (video + LLM) to an
    actionable hint appended to the raw provider message, so the real cause is
    not buried under a misleading status string:

      - 401/403 — usually a suspended / out-of-balance account, NOT a wrong key
        (providers like Volcengine return "api key is invalid" when in arrears);
      - 429 — rate-limited / out of quota;
      - 5xx / Cloudflare 52x / HTML body — provider or gateway outage/timeout.

    Returns '' when the status is fine, or when the provider message already
    names a concrete cause (balance / quota / rate-limit), so we never duplicate.
    """
    low = (message or "").lower()
    if any(
        s in low
        for s in (
            "balance", "overdue", "arrears", "欠费", "余额", "insufficient",
            "quota", "rate limit", "rate-limit", "too many requests",
        )
    ):
        return ""
    if status_code in (401, 403):
        return (
            " — note: a 401/403 here usually means the account is suspended or out "
            "of balance (overdue balance / 余额不足), not a wrong API key; check the "
            "account balance & billing before re-entering the key."
        )
    if status_code == 429:
        return (
            " — note: 429 means rate-limited or out of quota; retry later or check "
            "the provider account's quota/limits."
        )
    if status_code in (500, 502, 503, 504, 520, 521, 522, 523, 524) or (
        "<!doctype html" in low or "<html" in low
    ):
        return (
            " — note: this looks like a provider/gateway outage or timeout "
            "(5xx / HTML error page); the endpoint or base_url may be down. Retry "
            "shortly or check the provider/gateway status."
        )
    return ""


def seedance_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def seedance_reference_media_blocked_by_frames(params: dict[str, Any]) -> bool:
    """Volcengine rejects first/last-frame content mixed with reference media."""
    return bool(params.get("first_frame_url") or params.get("last_frame_url"))


def normalize_volcengine_base_url(base_url: str | None) -> str:
    base = (base_url or "https://ark.cn-beijing.volces.com/api/v3").strip().rstrip("/")
    suffix = "/contents/generations/tasks"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    return base.rstrip("/")


def volcengine_base_url_candidates(base_url: str | None) -> list[str]:
    """Return Seedance task API bases, supporting both Ark and LAS native keys."""
    if base_url and str(base_url).strip():
        return [normalize_volcengine_base_url(base_url)]
    return [
        "https://ark.cn-beijing.volces.com/api/v3",
        "https://operator.las.cn-beijing.volces.com/api/v1",
    ]


def normalize_kling_base_url(base_url: str | None) -> str:
    base = (base_url or "https://api-singapore.klingai.com").strip().rstrip("/")
    for suffix in ("/v1/videos/text2video", "/v1/videos/image2video"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def kling_base_url_candidates(base_url: str | None) -> list[str]:
    if base_url and str(base_url).strip():
        return [normalize_kling_base_url(base_url)]
    return ["https://api-singapore.klingai.com", "https://api.klingai.com", "https://api.klingapi.com"]


def is_official_kling_base(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == "klingai.com" or host.endswith(".klingai.com")


def kling_official_model_and_mode(native_model: str, params: dict[str, Any]) -> tuple[str, str]:
    explicit_mode = str(params.get("mode") or "").strip()
    if native_model == "kling-v3.0-pro":
        return "kling-v3", explicit_mode or "pro"
    if native_model == "kling-v3.0-std":
        return "kling-v3", explicit_mode or "std"
    return native_model, explicit_mode


def kling_payload_for_base(
    *,
    base: str,
    native_model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
    first_frame_url: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if is_official_kling_base(base):
        model_name, mode = kling_official_model_and_mode(native_model, params)
        payload: dict[str, Any] = {
            "model_name": model_name,
            "prompt": prompt,
            "duration": str(duration),
            "aspect_ratio": aspect_ratio,
        }
        if mode:
            payload["mode"] = mode
        if first_frame_url:
            payload["image"] = first_frame_url
        if params.get("negative_prompt"):
            payload["negative_prompt"] = params["negative_prompt"]
        if params.get("sound") is not None:
            payload["sound"] = params["sound"]
        if params.get("callback_url"):
            payload["callback_url"] = params["callback_url"]
        if params.get("external_task_id"):
            payload["external_task_id"] = params["external_task_id"]
    else:
        payload = {
            "model": native_model,
            "prompt": prompt,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }
        if first_frame_url:
            payload["image_url"] = first_frame_url

    if params.get("seed") is not None:
        payload["seed"] = params["seed"]
    return payload


class _JobLike(Protocol):
    id: str
    entity_id: str
    prompt: str
    model: str | None
    params: dict[str, Any] | None


@dataclass(frozen=True)
class VideoAdapterRuntime:
    http_client_cls: Any
    media_api_timeout: Any
    ensure_public_url: Callable[..., Awaitable[str]]
    public_url_kwargs: Callable[[str], dict[str, Any]]
    remember_provider_poll: Callable[[str, str, str, str | None], Awaitable[None]]
    poll_openrouter_generation: Callable[..., Awaitable[str]]
    poll_volcengine_task: Callable[..., Awaitable[str]]
    poll_generic_video_task: Callable[..., Awaitable[str]]
    download_and_save: Callable[..., Awaitable[dict[str, Any]]]
    extract_video_url: Callable[[Any], str]
    extract_task_id: Callable[[dict[str, Any]], str]
    provider_error_message: Callable[[Any], str]
    openrouter_api_url: Callable[[str], str]
    normalize_duration: Callable[..., int]
    normalize_resolution: Callable[[str | None, Any], str]
    ensure_inline_data_url: Callable[..., Awaitable[str]] | None = None


@dataclass(frozen=True)
class VideoAdapterMetadata:
    adapter: str
    provider: str
    route: str
    native_model: str


class VideoGenerationAdapter:
    adapter_name = "base"
    provider = ""
    route = "native"
    poll_provider = ""

    def supports_inline_local_references(self) -> bool:
        return not self.media_reference_capability("", "image").requires_public_url

    def media_reference_capability(
        self,
        model: str,
        media_kind: Literal["image", "video", "audio"],
    ) -> MediaReferenceCapability:
        del model, media_kind
        return PUBLIC_URL_MEDIA_REFERENCE

    def references_requiring_public_urls(
        self,
        model: str,
        *,
        first_frame_url: str = "",
        last_frame_url: str = "",
        reference_urls: list[str] | None = None,
        reference_video_urls: list[str] | None = None,
        audio_reference_urls: list[str] | None = None,
    ) -> list[str]:
        refs: list[str] = []
        groups = (
            ("image", [first_frame_url, last_frame_url, *(reference_urls or [])]),
            ("video", list(reference_video_urls or [])),
            ("audio", list(audio_reference_urls or [])),
        )
        for media_kind, values in groups:
            if self.media_reference_capability(model, media_kind).requires_public_url:
                refs.extend(str(value or "") for value in values if str(value or "").strip())
        return refs

    def metadata(self, model: str) -> VideoAdapterMetadata:
        return VideoAdapterMetadata(
            adapter=self.adapter_name,
            provider=self.provider,
            route=self.route,
            native_model=native_video_model(model),
        )

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        raise NotImplementedError


class OpenRouterVideoAdapter(VideoGenerationAdapter):
    adapter_name = "openrouter"
    provider = "openrouter"
    route = "openrouter"
    poll_provider = "openrouter"

    def metadata(self, model: str) -> VideoAdapterMetadata:
        return VideoAdapterMetadata(
            adapter=self.adapter_name,
            provider=catalog_video_provider(model) or self.provider,
            route=self.route,
            native_model=model,
        )

    def media_reference_capability(
        self,
        model: str,
        media_kind: Literal["image", "video", "audio"],
    ) -> MediaReferenceCapability:
        del model
        return OPENROUTER_IMAGE_MEDIA_REFERENCE if media_kind == "image" else PUBLIC_URL_MEDIA_REFERENCE

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        model = job.model or "bytedance/seedance-2.0"
        params = job.params or {}
        duration = runtime.normalize_duration(params.get("duration", 5))
        resolution = runtime.normalize_resolution(model, params.get("resolution", "720p"))
        aspect_ratio = params.get("aspect_ratio", "16:9")
        public_base_url = str(params.get("public_base_url") or "").strip()

        payload: dict[str, Any] = {"model": model, "prompt": job.prompt}
        if duration:
            payload["duration"] = duration
        if resolution:
            height = {"480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440}.get(resolution, 720)
            payload["height"] = height
            ratio_width, ratio_height = {
                "adaptive": (16, 9),
                "21:9": (21, 9),
                "16:9": (16, 9),
                "9:16": (9, 16),
                "1:1": (1, 1),
                "4:3": (4, 3),
                "3:4": (3, 4),
            }.get(aspect_ratio, (16, 9))
            payload["width"] = int(height * ratio_width / ratio_height)
        if params.get("seed") is not None:
            payload["seed"] = params["seed"]

        frame_images = await build_openrouter_frame_images(
            params,
            job.entity_id,
            runtime=runtime,
            public_base_url=public_base_url,
            capability=self.media_reference_capability(model, "image"),
        )
        if frame_images:
            payload["frame_images"] = frame_images

        reference_urls = params.get("reference_urls") or []
        if reference_urls:
            refs = []
            for ref_url in reference_urls[:9]:
                reference = await materialize_video_reference(
                    ref_url,
                    job.entity_id,
                    media_kind="image",
                    capability=self.media_reference_capability(model, "image"),
                    runtime=runtime,
                    public_base_url=public_base_url,
                )
                refs.append({
                    "type": "image_url",
                    "image_url": {
                        "url": reference.as_data_url()
                    },
                })
            payload["input_references"] = refs

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://manor.ai",
            "X-Title": "Manor AI",
        }
        api_base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        async with runtime.http_client_cls(timeout=runtime.media_api_timeout) as client:
            resp = await client.post(f"{api_base}/videos", headers=headers, json=payload)
            data = resp.json()

        logger.info("Video job %s OpenRouter response (%d): %s", job.id, resp.status_code, str(data)[:500])
        if resp.status_code not in (200, 201, 202):
            err = data.get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            return submit_error(
                resp.status_code,
                f"Video generation failed ({resp.status_code}): {msg}{provider_error_hint(resp.status_code, msg)}",
            )

        generation_id = (
            data.get("id")
            or data.get("generation_id")
            or (data.get("data", {}) or {}).get("id")
            or (data.get("data", {}) or {}).get("generation_id")
        )
        polling_url = data.get("polling_url") or ""
        unsigned = data.get("unsigned_urls") or []
        video_url = (
            (unsigned[0] if unsigned else "")
            or data.get("url")
            or data.get("video_url")
            or (data.get("data", {}) or {}).get("url")
            or (data.get("data", {}) or {}).get("video_url")
            or ""
        )

        if not video_url and (generation_id or polling_url):
            poll_path = runtime.openrouter_api_url(polling_url or f"/videos/{generation_id}")
            await runtime.remember_provider_poll(job.id, self.poll_provider, poll_path, generation_id)
            video_url = await runtime.poll_openrouter_generation(poll_path, headers)

        if not video_url:
            return {"error": "No video URL in response", "raw": str(data)[:500]}

        return await _download_adapter_result(
            runtime,
            job,
            video_url,
            model,
            duration,
            resolution,
            auth_headers={"Authorization": headers["Authorization"], "HTTP-Referer": "https://manor.ai"},
        )


class VercelGatewayVideoAdapter(VideoGenerationAdapter):
    """AI Gateway v4 async video-model adapter."""

    adapter_name = "vercel_gateway"
    provider = "vercel"
    route = "vercel"
    poll_provider = "vercel"

    def media_reference_capability(
        self,
        model: str,
        media_kind: Literal["image", "video", "audio"],
    ) -> MediaReferenceCapability:
        del model
        return VERCEL_IMAGE_MEDIA_REFERENCE if media_kind == "image" else PUBLIC_URL_MEDIA_REFERENCE

    def metadata(self, model: str) -> VideoAdapterMetadata:
        return VideoAdapterMetadata(
            adapter=self.adapter_name,
            provider=self.provider,
            route=self.route,
            native_model=model,
        )

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        from packages.core.services.vercel_ai_gateway import (
            VercelAIGatewayError,
            vercel_gateway_endpoint,
            vercel_gateway_post,
        )

        model = job.model or "bytedance/seedance-2.0"
        params = job.params or {}
        duration = runtime.normalize_duration(params.get("duration", 5))
        resolution = runtime.normalize_resolution(model, params.get("resolution", "720p"))
        aspect_ratio = str(params.get("aspect_ratio") or "16:9")
        public_base_url = str(params.get("public_base_url") or "").strip()

        payload: dict[str, Any] = {
            "prompt": job.prompt,
            "n": 1,
            "duration": duration,
            "aspectRatio": aspect_ratio,
            "resolution": _vercel_video_resolution(resolution, aspect_ratio),
            "generateAudio": seedance_bool(params.get("generate_audio", False)),
        }
        # The gateway ignores a top-level generateAudio: provider settings go
        # under providerOptions.<provider>. Without it Seedance adds an audio
        # track, which its copyright filter can reject.
        provider = str(model).split("/", 1)[0].strip()
        if provider == "kwaivgi":
            payload["providerOptions"] = {"klingai": _vercel_kling_options(model, payload["generateAudio"])}
        elif provider == "xai":
            # Grok always adds its own audio track and has no audio switch.
            payload.pop("generateAudio")
        elif provider:
            payload["providerOptions"] = {provider: {"generateAudio": payload["generateAudio"]}}
        wire_model = _vercel_video_wire_model(model, has_first_frame=bool(params.get("first_frame_url")))
        if params.get("seed") is not None:
            payload["seed"] = params["seed"]

        # The gateway caps the whole request (~300KB), not each image, so the
        # inline allowance is one budget shared by every image in it; an image
        # that does not fit what is left goes as a signed URL instead.
        inline_budget = [VERCEL_VIDEO_INLINE_REFERENCE_MAX_BYTES]

        async def reference_file(value: str, media_kind: Literal["image", "video", "audio"]) -> dict[str, str]:
            capability = self.media_reference_capability(model, media_kind)
            if INLINE_BASE64_REFERENCE in capability.transports:
                if inline_budget[0] < VERCEL_VIDEO_MIN_INLINE_REFERENCE_BYTES:
                    capability = PUBLIC_URL_MEDIA_REFERENCE
                else:
                    capability = MediaReferenceCapability(
                        capability.transports, max_inline_bytes=inline_budget[0],
                    )
            reference = await materialize_video_reference(
                value,
                job.entity_id,
                media_kind=media_kind,
                capability=capability,
                runtime=runtime,
                public_base_url=public_base_url,
            )
            if reference.type == "file":
                inline_budget[0] -= len(reference.value) * 3 // 4
            return reference.as_vercel_file()

        frame_images: list[dict[str, Any]] = []
        for frame_type, value in (
            ("first_frame", params.get("first_frame_url")),
            ("last_frame", params.get("last_frame_url")),
        ):
            if not value:
                continue
            frame_images.append(
                {"frameType": frame_type, "image": await reference_file(str(value), "image")}
            )
        if frame_images:
            payload["frameImages"] = frame_images

        input_references: list[dict[str, Any]] = []
        for media_kind, values in (
            ("image", params.get("reference_urls") or []),
            ("video", params.get("reference_video_urls") or []),
        ):
            for value in values:
                input_references.append(await reference_file(str(value), media_kind))
        if input_references:
            payload["inputReferences"] = input_references

        # The v4 file lists have no audio slot. Seedance takes audio references
        # as provider options (``referenceAudio``, URL strings, per
        # @ai-sdk/bytedance), so they must be fetchable signed URLs.
        audio_reference_urls = list(params.get("audio_reference_urls") or [])
        if not audio_reference_urls and params.get("audio_reference_url"):
            audio_reference_urls = [params["audio_reference_url"]]
        if audio_reference_urls and provider == "bytedance":
            from packages.core.constants.models import video_model_capabilities

            limit = video_model_capabilities(model)["max_audio_references"]
            payload["providerOptions"][provider]["referenceAudio"] = [
                (await materialize_video_reference(
                    str(value),
                    job.entity_id,
                    media_kind="audio",
                    capability=PUBLIC_URL_MEDIA_REFERENCE,
                    runtime=runtime,
                    public_base_url=public_base_url,
                )).value
                for value in audio_reference_urls[:limit]
            ]

        auth_method = str(params.get("vercel_auth_method") or "api-key")
        try:
            started = await vercel_gateway_post(
                api_key=api_key,
                base_url=base_url,
                model=wire_model,
                protocol="video",
                payload=payload,
                auth_method=auth_method,
                endpoint_suffix="start",
                timeout=180.0,
            )
        except VercelAIGatewayError as exc:
            return submit_error(exc.status_code, str(exc))
        except Exception as exc:
            return {"error": str(exc)}

        operation = started.get("operation")
        if operation is None:
            return {"error": "Vercel AI Gateway video start response omitted operation state."}

        status_url = f"{vercel_gateway_endpoint(base_url, 'video')}/status"
        operation_json = json.dumps(operation, separators=(",", ":"))
        await runtime.remember_provider_poll(
            job.id,
            self.poll_provider,
            status_url,
            operation_json,
        )

        deadline = time.monotonic() + 720.0
        while time.monotonic() < deadline:
            await asyncio.sleep(5.0)
            try:
                status = await vercel_gateway_post(
                    api_key=api_key,
                    base_url=base_url,
                    model=wire_model,
                    protocol="video",
                    payload={"operation": operation},
                    auth_method=auth_method,
                    endpoint_suffix="status",
                    timeout=60.0,
                )
            except Exception as exc:
                logger.debug("Video job %s Vercel status poll failed: %s", job.id, exc)
                continue

            state = str(status.get("status") or "").strip().lower()
            if state == "completed":
                video_url = _vercel_video_result_url(status)
                if not video_url:
                    return {"error": "Vercel completed without video data."}
                return await _download_adapter_result(
                    runtime,
                    job,
                    video_url,
                    model,
                    duration,
                    resolution,
                )
            if state in {"error", "cancelled"}:
                return {
                    "error": str(
                        status.get("error")
                        or "Vercel video generation was cancelled."
                    )
                }

        raise TimeoutError("Vercel video generation is still pending after 720 seconds")


def _vercel_video_resolution(resolution: str, aspect_ratio: str) -> str:
    """Gateway pixel size: preserve native portrait presets before defaults.

    1080p vertical is 1080x1920, not 608x1080: providers reject a portrait
    size whose long side is the tier. Native 3:4 presets use a provider tier
    rather than a literal short-edge pixel count; other ratios retain the
    existing short-side conversion.
    """
    portrait_presets = {
        "9:16": {"480p": "480x864", "720p": "720x1280", "1080p": "1080x1920"},
        "3:4": {"480p": "560x752", "720p": "834x1112", "1080p": "1248x1664"},
    }
    if aspect_ratio in portrait_presets:
        return portrait_presets[aspect_ratio].get(resolution, portrait_presets[aspect_ratio]["720p"])
    short_side = {"480p": 480, "720p": 720, "1080p": 1080}.get(resolution, 720)
    width_ratio, height_ratio = {
        "21:9": (21, 9),
        "16:9": (16, 9),
        "9:16": (9, 16),
        "1:1": (1, 1),
        "4:3": (4, 3),
        "3:4": (3, 4),
    }.get(aspect_ratio, (16, 9))
    if width_ratio >= height_ratio:
        return f"{max(1, round(short_side * width_ratio / height_ratio))}x{short_side}"
    return f"{short_side}x{max(1, round(short_side * height_ratio / width_ratio))}"


def _video_media_type(value: str, media_kind: Literal["image", "video", "audio"]) -> str:
    lowered = urlsplit(value).path.lower()
    if lowered.endswith(".webm"):
        return "video/webm"
    if lowered.endswith(".mov"):
        return "video/quicktime"
    if lowered.endswith(".mp4"):
        return "video/mp4"
    if lowered.endswith(".webp"):
        return "image/webp"
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith(".wav"):
        return "audio/wav"
    if lowered.endswith(".mp3"):
        return "audio/mpeg"
    return {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}[media_kind]


def _video_model_file_from_data_url(
    value: str,
    media_kind: Literal["image", "video", "audio"],
) -> VideoModelFile:
    header, separator, data = value.partition(",")
    if not separator or not header.startswith("data:") or ";base64" not in header:
        raise RuntimeError("Inline video reference must be a base64 data URL.")
    media_type = header[5:].split(";", 1)[0] or _video_media_type(value, media_kind)
    return VideoModelFile(type="file", media_type=media_type, value=data)


async def materialize_video_reference(
    value: str,
    entity_id: str,
    *,
    media_kind: Literal["image", "video", "audio"],
    capability: MediaReferenceCapability,
    runtime: VideoAdapterRuntime,
    public_base_url: str,
) -> VideoModelFile:
    """Materialize an entity reference as the shared VideoModelV4-like union."""
    if INLINE_BASE64_REFERENCE in capability.transports and runtime.ensure_inline_data_url is not None:
        try:
            inline = await runtime.ensure_inline_data_url(
                value,
                entity_id,
                max_bytes=capability.max_inline_bytes or VERCEL_VIDEO_INLINE_REFERENCE_MAX_BYTES,
            )
        except InlineMediaReferenceLimitError:
            if SIGNED_URL_REFERENCE not in capability.transports:
                raise
            logger.info(
                "Video reference exceeded inline limit; using signed URL instead "
                "(entity=%s, media_kind=%s)",
                entity_id,
                media_kind,
            )
        else:
            if inline.startswith("data:"):
                return _video_model_file_from_data_url(inline, media_kind)
            return VideoModelFile(type="url", media_type=_video_media_type(inline, media_kind), value=inline)

    if SIGNED_URL_REFERENCE in capability.transports:
        public_url = await runtime.ensure_public_url(
            value,
            entity_id,
            **runtime.public_url_kwargs(public_base_url),
        )
        return VideoModelFile(type="url", media_type=_video_media_type(public_url, media_kind), value=public_url)

    raise RuntimeError(f"No supported transport for {media_kind} media reference.")


def _vercel_video_result_url(payload: dict[str, Any]) -> str:
    videos = payload.get("videos") or []
    first = videos[0] if isinstance(videos, list) and videos else None
    if not isinstance(first, dict):
        return ""
    if first.get("type") == "url" and first.get("url"):
        return str(first["url"])
    if first.get("type") == "base64" and first.get("data"):
        media_type = str(first.get("mediaType") or "video/mp4")
        return f"data:{media_type};base64,{str(first['data'])}"
    return ""


class VolcengineSeedanceAdapter(VideoGenerationAdapter):
    adapter_name = "volcengine_seedance"
    provider = "bytedance"
    route = "native"
    poll_provider = "bytedance"

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        model = job.model or "bytedance/seedance-2.0"
        native_model = native_video_model(model)
        params = job.params or {}
        duration = runtime.normalize_duration(params.get("duration", 5), model)
        resolution = runtime.normalize_resolution(native_model, params.get("resolution", "720p"))
        aspect_ratio = params.get("aspect_ratio", "16:9")
        if model == "bytedance/seedance-2.5" and (params.get("first_frame_url") or params.get("last_frame_url")):
            aspect_ratio = "adaptive"
        public_base_url = str(params.get("public_base_url") or "").strip()
        bases = volcengine_base_url_candidates(base_url)

        content: list[dict[str, Any]] = [{"type": "text", "text": job.prompt}]
        frame_control_blocks_reference_media = seedance_reference_media_blocked_by_frames(params)
        first_frame = params.get("first_frame_url")
        if first_frame:
            content.append(await seedance_image_content(
                first_frame,
                job.entity_id,
                "first_frame",
                runtime=runtime,
                public_base_url=public_base_url,
            ))
        last_frame = params.get("last_frame_url")
        if last_frame:
            content.append(await seedance_image_content(
                last_frame,
                job.entity_id,
                "last_frame",
                runtime=runtime,
                public_base_url=public_base_url,
            ))

        from packages.core.constants.models import video_model_capabilities

        capabilities = video_model_capabilities(model)
        reference_urls = params.get("reference_urls") or []
        if reference_urls and not frame_control_blocks_reference_media:
            for ref_url in reference_urls[:capabilities["max_reference_images"]]:
                content.append(await seedance_image_content(
                    ref_url,
                    job.entity_id,
                    "reference_image",
                    runtime=runtime,
                    public_base_url=public_base_url,
                ))
        reference_video_urls = params.get("reference_video_urls") or []
        if reference_video_urls and not frame_control_blocks_reference_media:
            for ref_url in reference_video_urls[:capabilities["max_reference_videos"]]:
                content.append(await seedance_media_content(
                    ref_url,
                    job.entity_id,
                    media_type="video",
                    role="reference_video",
                    runtime=runtime,
                    public_base_url=public_base_url,
                ))
        audio_reference_urls = params.get("audio_reference_urls") or []
        if not audio_reference_urls and params.get("audio_reference_url"):
            audio_reference_urls = [params.get("audio_reference_url")]
        if audio_reference_urls and not frame_control_blocks_reference_media:
            for ref_url in audio_reference_urls[:capabilities["max_audio_references"]]:
                content.append(await seedance_media_content(
                    ref_url,
                    job.entity_id,
                    media_type="audio",
                    role="reference_audio",
                    runtime=runtime,
                    public_base_url=public_base_url,
                ))
        if frame_control_blocks_reference_media and (reference_urls or reference_video_urls or audio_reference_urls):
            logger.info(
                "Video job %s Seedance frame control omits reference media to satisfy Volcengine content rules",
                job.id,
            )

        payload: dict[str, Any] = {
            "model": native_model,
            "content": content,
            "ratio": aspect_ratio,
            "resolution": resolution,
            "generate_audio": (
                False
                if frame_control_blocks_reference_media and audio_reference_urls
                else seedance_bool(params.get("generate_audio", False))
            ),
            "watermark": seedance_bool(params.get("watermark", False)),
        }
        if params.get("frames") is not None:
            try:
                payload["frames"] = max(1, int(params["frames"]))
            except (TypeError, ValueError):
                payload["duration"] = duration
        else:
            payload["duration"] = duration
        if params.get("seed") is not None and model != "bytedance/seedance-2.5":
            payload["seed"] = params["seed"]
        for key in ("return_last_frame", "camera_fixed", "draft"):
            if model == "bytedance/seedance-2.5" and key == "camera_fixed":
                continue
            if params.get(key) is not None:
                payload[key] = seedance_bool(params[key])

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        data: dict[str, Any] = {}
        resp = None
        base = bases[0]
        async with runtime.http_client_cls(timeout=runtime.media_api_timeout) as client:
            for index, candidate in enumerate(bases):
                base = candidate
                resp = await client.post(f"{base}/contents/generations/tasks", headers=headers, json=payload)
                data = resp.json()
                if resp.status_code in (401, 403, 404) and index + 1 < len(bases):
                    logger.info(
                        "Video job %s Seedance base %s returned %d; trying alternate Volcengine base",
                        job.id, base, resp.status_code,
                    )
                    continue
                break

        logger.info("Video job %s Volcengine response (%d): %s", job.id, resp.status_code, str(data)[:500])
        if resp.status_code not in (200, 201, 202):
            err = data.get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err or data)
            hint = provider_error_hint(resp.status_code, msg)
            return submit_error(resp.status_code, f"Seedance generation failed ({resp.status_code}): {msg}{hint}")

        task_id = runtime.extract_task_id(data)
        video_url = runtime.extract_video_url(data)
        if not video_url and task_id:
            poll_url = f"{base}/contents/generations/tasks/{task_id}"
            await runtime.remember_provider_poll(job.id, self.poll_provider, poll_url, task_id)
            video_url = await runtime.poll_volcengine_task(poll_url, headers)
        if not video_url:
            return {"error": "No video URL in Seedance response", "raw": str(data)[:500]}

        return await _download_adapter_result(runtime, job, video_url, model, duration, resolution)


ATLASCLOUD_API_BASE = "https://api.atlascloud.ai"


class AtlasCloudVideoAdapter(VideoGenerationAdapter):
    """Atlas Cloud unified video API (Wan family).

    BYOK-only: Manor holds no platform Atlas key, so this adapter is only
    reachable with a user-supplied key (enforced in generate_video).
    Contract: POST /api/v1/model/generateVideo → {"data": {"id": ...}},
    then GET /api/v1/model/prediction/{id} until status completed/failed
    with the video URL in data.outputs[].
    """

    adapter_name = "atlascloud"
    provider = "atlascloud"
    route = "native"
    poll_provider = "atlascloud"

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        model = job.model or "atlascloud/wan-2.2-turbo-spicy"
        native_model = native_video_model(model)
        params = job.params or {}
        duration = runtime.normalize_duration(params.get("duration", 5))
        resolution = runtime.normalize_resolution(model, params.get("resolution", "480p"))
        public_base_url = str(params.get("public_base_url") or "").strip()
        base = (base_url or ATLASCLOUD_API_BASE).rstrip("/")

        first_frame = params.get("first_frame_url")
        if not first_frame:
            # Verified against the live API: this route rejects prompt-only
            # requests with "image is required for i2v".
            return {
                "error": (
                    "Wan 2.2 Turbo (Atlas) is an image-to-video model — it needs a "
                    "source image. Generate or attach an image first and pass it as "
                    "the first frame, or switch to a text-to-video model."
                )
            }

        payload: dict[str, Any] = {
            "model": native_model,
            "prompt": job.prompt,
            "duration": duration,
            "resolution": resolution,
            "image": await runtime.ensure_public_url(
                first_frame,
                job.entity_id,
                **runtime.public_url_kwargs(public_base_url),
            ),
        }

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with runtime.http_client_cls(timeout=runtime.media_api_timeout) as client:
            resp = await client.post(
                f"{base}/api/v1/model/generateVideo", headers=headers, json=payload,
            )
            data = resp.json()

        logger.info(
            "Video job %s Atlas Cloud response (%d): %s", job.id, resp.status_code, str(data)[:500],
        )
        # Atlas wraps errors as {"code": N, "msg"|"message": "..."} and can
        # return HTTP 200 with a non-200 inner code.
        inner_code = data.get("code")
        failed = resp.status_code not in (200, 201, 202) or (
            isinstance(inner_code, int) and inner_code >= 400
        )
        if failed:
            err = data.get("error") or data.get("message") or data.get("msg") or data
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("msg") or "")
            else:
                msg = str(err)
            status = inner_code if isinstance(inner_code, int) and inner_code >= 400 else resp.status_code
            return submit_error(
                status,
                f"Atlas Cloud generation failed ({status}): {msg}{provider_error_hint(status, msg)}",
            )

        body = data.get("data") if isinstance(data.get("data"), dict) else data
        prediction_id = body.get("id") or body.get("prediction_id")
        video_url = runtime.extract_video_url(data)
        if not video_url and prediction_id:
            poll_url = f"{base}/api/v1/model/prediction/{prediction_id}"
            await runtime.remember_provider_poll(job.id, self.poll_provider, poll_url, prediction_id)
            video_url = await runtime.poll_generic_video_task(poll_url, headers)
        if not video_url:
            return {"error": "No video URL in Atlas Cloud response", "raw": str(data)[:500]}

        return await _download_adapter_result(runtime, job, video_url, model, duration, resolution)


class KlingVideoAdapter(VideoGenerationAdapter):
    adapter_name = "kling"
    provider = "kwaivgi"
    route = "native"
    poll_provider = "kwaivgi"

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        model = job.model or "kwaivgi/kling-v3.0-std"
        native_model = native_video_model(model)
        params = job.params or {}
        duration = runtime.normalize_duration(params.get("duration", 5))
        resolution = runtime.normalize_resolution(model, params.get("resolution", "720p"))
        aspect_ratio = params.get("aspect_ratio", "16:9")
        public_base_url = str(params.get("public_base_url") or "").strip()
        bases = kling_base_url_candidates(base_url)

        first_frame = params.get("first_frame_url")
        endpoint = "image2video" if first_frame else "text2video"
        first_frame_url = ""
        if first_frame:
            first_frame_url = await runtime.ensure_public_url(
                first_frame,
                job.entity_id,
                **runtime.public_url_kwargs(public_base_url),
            )

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        data: dict[str, Any] = {}
        resp = None
        base = bases[0]
        last_connect_error: Exception | None = None
        async with runtime.http_client_cls(timeout=runtime.media_api_timeout) as client:
            for index, candidate in enumerate(bases):
                base = candidate
                payload = kling_payload_for_base(
                    base=base,
                    native_model=native_model,
                    prompt=job.prompt,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    first_frame_url=first_frame_url,
                    params=params,
                )
                try:
                    resp = await client.post(f"{base}/v1/videos/{endpoint}", headers=headers, json=payload)
                    data = resp.json()
                    break
                except httpx.ConnectError as exc:
                    last_connect_error = exc
                    if index + 1 < len(bases):
                        logger.info(
                            "Video job %s Kling base %s could not be reached; trying alternate base",
                            job.id, base,
                        )
                        continue
                    raise
        if resp is None:
            return {"error": f"Kling generation failed: {last_connect_error or 'connection failed'}"}

        logger.info("Video job %s Kling response (%d): %s", job.id, resp.status_code, str(data)[:500])
        if resp.status_code not in (200, 201, 202):
            err = data.get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err or data)
            return submit_error(
                resp.status_code,
                f"Kling generation failed ({resp.status_code}): {msg}{provider_error_hint(resp.status_code, msg)}",
            )

        video_url = runtime.extract_video_url(data)
        task_id = (
            data.get("id") or data.get("task_id") or data.get("taskId")
            or (data.get("data") or {}).get("id")
            or (data.get("data") or {}).get("task_id")
            or (data.get("data") or {}).get("taskId")
        )
        polling_url = data.get("polling_url") or (data.get("data") or {}).get("polling_url")
        if not video_url and (task_id or polling_url):
            if polling_url:
                poll_url = polling_url
            elif is_official_kling_base(base):
                poll_url = f"{base}/v1/videos/{endpoint}/{task_id}"
            else:
                poll_url = f"{base}/v1/videos/{task_id}"
            await runtime.remember_provider_poll(job.id, self.poll_provider, poll_url, task_id)
            video_url = await runtime.poll_generic_video_task(poll_url, headers)
        if not video_url:
            return {"error": "No video URL in Kling response", "raw": str(data)[:500]}

        return await _download_adapter_result(runtime, job, video_url, model, duration, resolution)


async def seedance_media_content(
    url: str,
    entity_id: str,
    *,
    media_type: str,
    role: str | None = None,
    runtime: VideoAdapterRuntime,
    public_base_url: str = "",
) -> dict[str, Any]:
    field = f"{media_type}_url"
    reference = await materialize_video_reference(
        url,
        entity_id,
        media_kind=media_type,
        capability=PUBLIC_URL_MEDIA_REFERENCE,
        runtime=runtime,
        public_base_url=public_base_url,
    )
    item: dict[str, Any] = {
        "type": field,
        field: {
            "url": reference.as_url()
        },
    }
    if role:
        item["role"] = role
    return item


async def seedance_image_content(
    url: str,
    entity_id: str,
    role: str | None = None,
    *,
    runtime: VideoAdapterRuntime,
    public_base_url: str = "",
) -> dict[str, Any]:
    return await seedance_media_content(
        url,
        entity_id,
        media_type="image",
        role=role,
        runtime=runtime,
        public_base_url=public_base_url,
    )


async def build_openrouter_frame_images(
    params: dict[str, Any],
    entity_id: str,
    *,
    runtime: VideoAdapterRuntime,
    public_base_url: str = "",
    capability: MediaReferenceCapability = OPENROUTER_IMAGE_MEDIA_REFERENCE,
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    first_frame = params.get("first_frame_url", "")
    last_frame = params.get("last_frame_url", "")
    if first_frame:
        reference = await materialize_video_reference(
            first_frame,
            entity_id,
            media_kind="image",
            capability=capability,
            runtime=runtime,
            public_base_url=public_base_url,
        )
        frames.append({
            "type": "image_url",
            "image_url": {
                "url": reference.as_data_url()
            },
            "frame_type": "first_frame",
        })
    if last_frame:
        reference = await materialize_video_reference(
            last_frame,
            entity_id,
            media_kind="image",
            capability=capability,
            runtime=runtime,
            public_base_url=public_base_url,
        )
        frames.append({
            "type": "image_url",
            "image_url": {
                "url": reference.as_data_url()
            },
            "frame_type": "last_frame",
        })
    return frames

_XAI_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4"}


def _xai_request_id(data: Any) -> str:
    """Task id from an xAI-compatible create response (relays vary the key)."""
    if not isinstance(data, dict):
        return ""
    for key in ("request_id", "id", "task_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("data", "video"):
        found = _xai_request_id(data.get(key))
        if found:
            return found
    return ""


class XaiVideoAdapter(VideoGenerationAdapter):
    """Grok Imagine video: xAI's API or an xAI-compatible relay (Sub2API).

    BYOK-only (enforced in generate_video). The saved base URL decides the
    host; the contract is the same on both:
    POST {base}/videos/generations → {"request_id"}, GET {base}/videos/{id}
    until status done/failed, then the MP4 from {base}/videos/{id}/content.
    The completed payload's video URL can be relative or point upstream, so
    the result is always downloaded from the content endpoint with the same
    key — never by sending the key to a URL taken from a response.
    """

    adapter_name = "xai"
    provider = "xai"
    route = "native"
    poll_provider = "xai"

    def media_reference_capability(
        self,
        model: str,
        media_kind: Literal["image", "video", "audio"],
    ) -> MediaReferenceCapability:
        del model
        if media_kind == "image":
            return OPENROUTER_IMAGE_MEDIA_REFERENCE
        return PUBLIC_URL_MEDIA_REFERENCE

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        model = job.model or "xai/grok-imagine-video-1.5"
        params = job.params or {}
        from packages.core.services.xai_video_transport import (
            XAI_BINDING_KEY, XaiTransportError, require_xai_job_binding,
            xai_poll_url, xai_request,
        )

        try:
            base = require_xai_job_binding(
                params.get(XAI_BINDING_KEY), entity_id=job.entity_id,
                model=model, base_url=base_url or "", api_key=api_key,
                is_byok=params.get("credential_source") == "byok",
            )
        except XaiTransportError as exc:
            return {"error": str(exc)}
        from packages.core.constants.models import video_model_capabilities

        # Each Grok model takes what docs.x.ai lists for it: Lite no references or
        # last frame, the first model up to 7 references at 720p.
        capabilities = video_model_capabilities(model)
        first_frame = params.get("first_frame_url")
        last_frame = params.get("last_frame_url") if capabilities.get("last_frame") else ""
        references = [url for url in params.get("reference_urls") or [] if url][
            :int(capabilities.get("max_reference_images") or 0)]
        duration = max(1, min(15, int(params.get("duration") or 5)))
        resolution = runtime.normalize_resolution(model, params.get("resolution", "720p"))
        if references or last_frame or native_video_model(model) == "grok-imagine-video":
            # Reference-to-video, and the first model, render at 720p at most.
            resolution = "720p"
        if references and native_video_model(model) == "grok-imagine-video":
            duration = min(duration, 10)

        async def image_url(url: str) -> dict[str, str]:
            image = await materialize_video_reference(
                url,
                job.entity_id,
                media_kind="image",
                capability=self.media_reference_capability(model, "image"),
                runtime=runtime,
                public_base_url=str(params.get("public_base_url") or "").strip(),
            )
            return {"url": image.as_data_url()}

        prompt = job.prompt
        if references and "<IMAGE_" not in prompt:
            # A pinned first frame is <IMAGE_0>, so the references are numbered after it.
            offset = 1 if first_frame else 0
            names = ", ".join(f"<IMAGE_{offset + index}>" for index in range(len(references)))
            prompt = f"{prompt}\n\nKeep the same people, faces, clothing and look as in {names}."
        payload: dict[str, Any] = {
            "model": native_video_model(model),
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
        }
        if first_frame:
            payload["image"] = await image_url(first_frame)
        if references:
            payload["reference_images"] = [await image_url(url) for url in references]
        if last_frame:
            payload["last_frame"] = await image_url(last_frame)
        aspect_ratio = str(params.get("aspect_ratio") or "").strip()
        if aspect_ratio in _XAI_ASPECT_RATIOS:
            payload["aspect_ratio"] = aspect_ratio

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            resp = await xai_request(
                "POST", f"{base}/videos/generations", headers=headers,
                json_body=payload, timeout=runtime.media_api_timeout,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}
        except XaiTransportError as exc:
            return {"error": str(exc)}

        logger.info("Video job %s xAI response status %d", job.id, resp.status_code)
        request_id = _xai_request_id(data)
        if resp.status_code not in (200, 201, 202) or not request_id:
            return submit_error(
                resp.status_code, f"Grok video generation failed ({resp.status_code}). Check the relay and API key.",
            )

        try:
            poll_url = xai_poll_url(base, request_id)
        except XaiTransportError as exc:
            return {"error": str(exc)}
        await runtime.remember_provider_poll(job.id, self.poll_provider, poll_url, request_id)
        await runtime.poll_generic_video_task(poll_url, {"Authorization": f"Bearer {api_key}"}, provider="xai")
        return await _download_adapter_result(
            runtime,
            job,
            f"{poll_url}/content",
            model,
            duration,
            resolution,
            auth_headers={"Authorization": f"Bearer {api_key}"},
        )


GOOGLE_GENAI_BASE_URL = "https://generativelanguage.googleapis.com"
# Google takes images inline in the request body; it cannot read Manor's signed URLs.
GOOGLE_IMAGE_MEDIA_REFERENCE = MediaReferenceCapability(
    (INLINE_BASE64_REFERENCE,), max_inline_bytes=7 * 1024 * 1024,
)
_GOOGLE_ASPECT_RATIOS = frozenset({"16:9", "9:16"})


def normalize_google_base_url(base_url: str | None) -> str:
    """The Gemini API origin, whether or not the saved URL carries its version path."""
    base = str(base_url or "").strip().rstrip("/") or GOOGLE_GENAI_BASE_URL
    for suffix in ("/v1beta", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def _google_interaction_video(data: Any) -> tuple[str, str]:
    """The (mime type, base64 data) of the video in an interaction's model output."""
    steps = data.get("steps") if isinstance(data, dict) else None
    for step in steps or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "video" and item.get("data"):
                return str(item.get("mime_type") or "video/mp4"), str(item["data"])
    return "", ""


class GoogleInteractionsVideoAdapter(VideoGenerationAdapter):
    """Gemini Omni video through the Gemini Interactions API, on the Account's own key.

    POST {base}/v1beta/interactions answers synchronously with the clip as base64
    in steps[].content[]. Videos are kept to 720p, the largest size Google returns
    inline, so the key is never sent to a download URL taken from a response.
    The API has no duration field; the requested length goes in the prompt.
    """

    adapter_name = "google_interactions"
    provider = "google"
    route = "native"
    poll_provider = "google"

    def media_reference_capability(
        self,
        model: str,
        media_kind: Literal["image", "video", "audio"],
    ) -> MediaReferenceCapability:
        del model
        if media_kind == "image":
            return GOOGLE_IMAGE_MEDIA_REFERENCE
        return PUBLIC_URL_MEDIA_REFERENCE

    async def submit(
        self,
        job: _JobLike,
        api_key: str,
        base_url: str | None,
        runtime: VideoAdapterRuntime,
    ) -> dict[str, Any]:
        from packages.core.constants.models import video_model_capabilities

        model = job.model or ""
        params = job.params or {}
        capabilities = video_model_capabilities(model)
        first_frame = params.get("first_frame_url") or ""
        # A last frame only means something after a first frame (interpolation).
        last_frame = params.get("last_frame_url") if first_frame and capabilities.get("last_frame") else ""
        frames = [url for url in (first_frame, last_frame) if url]
        references = [url for url in params.get("reference_urls") or [] if url][
            :int(capabilities.get("max_reference_images") or 0)]
        duration = max(3, min(10, int(params.get("duration") or 5)))

        inputs: list[dict[str, str]] = []
        for url in [*frames, *references]:
            image = await materialize_video_reference(
                url,
                job.entity_id,
                media_kind="image",
                capability=GOOGLE_IMAGE_MEDIA_REFERENCE,
                runtime=runtime,
                public_base_url=str(params.get("public_base_url") or "").strip(),
            )
            inputs.append({"type": "image", "data": image.value, "mime_type": image.media_type})
        notes: list[str] = []
        if frames:
            notes.append("Image 1 is the first frame of the clip.")
        if len(frames) > 1:
            notes.append("Image 2 is the last frame of the clip.")
        if references:
            first = len(frames) + 1
            notes.append(
                f"Images {first}-{first + len(references) - 1} are references: keep the same "
                "people, faces, clothing and look."
            )
        notes.append(f"The clip is about {duration} seconds long.")
        inputs.append({"type": "text", "text": "\n\n".join([job.prompt, " ".join(notes)])})

        response_format: dict[str, str] = {"type": "video", "resolution": "720p"}
        aspect_ratio = str(params.get("aspect_ratio") or "").strip()
        if aspect_ratio in _GOOGLE_ASPECT_RATIOS:
            response_format["aspect_ratio"] = aspect_ratio
        payload = {
            "model": native_video_model(model),
            "input": inputs,
            "response_format": response_format,
            "background": False,
            "store": False,
            "stream": False,
        }
        url = f"{normalize_google_base_url(base_url)}/v1beta/interactions"
        # Generation runs inside this one request, so its read timeout is generous.
        timeout = httpx.Timeout(connect=30.0, read=900.0, write=180.0, pool=30.0)
        async with runtime.http_client_cls(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            )
        try:
            data = resp.json()
        except Exception:
            data = {}
        logger.info("Video job %s Google Interactions response status %d", job.id, resp.status_code)
        if resp.status_code >= 400:
            message = runtime.provider_error_message(data) or resp.text[:300]
            return submit_error(
                resp.status_code,
                f"Gemini video generation failed ({resp.status_code}): {message}"
                + provider_error_hint(resp.status_code, message),
            )
        mime_type, video = _google_interaction_video(data)
        if not video:
            status = str((data or {}).get("status") or "") if isinstance(data, dict) else ""
            return {"error": f"Gemini returned no video (status {status or 'unknown'}). The prompt may have been blocked."}
        return await _download_adapter_result(
            runtime,
            job,
            f"data:{mime_type};base64,{video}",
            model,
            duration,
            "720p",
        )


def select_video_generation_adapter(
    *,
    model: str,
    provider: str,
    api_key: str,
) -> VideoGenerationAdapter | None:
    selected_provider = (provider or catalog_video_provider(model)).lower()
    if model == "bytedance/seedance-2.5" and (selected_provider == "vercel" or (api_key or "").startswith("sk-or-")):
        return None
    # Atlas models exist only on Atlas — never route them through OpenRouter,
    # even when the resolved key is an OpenRouter key.
    if selected_provider == "atlascloud":
        return AtlasCloudVideoAdapter()
    # A Grok key (own or relayed) uses the xAI API; managed Grok arrives here with provider "vercel".
    if selected_provider == "xai":
        return XaiVideoAdapter()
    if selected_provider == "google":
        return GoogleInteractionsVideoAdapter()
    if selected_provider == "vercel":
        return VercelGatewayVideoAdapter()
    if (api_key or "").startswith("sk-or-"):
        return OpenRouterVideoAdapter()
    if selected_provider == "bytedance":
        return VolcengineSeedanceAdapter()
    if selected_provider == "kwaivgi":
        return KlingVideoAdapter()
    return None


def video_adapter_by_name(name: str) -> VideoGenerationAdapter | None:
    adapters: dict[str, VideoGenerationAdapter] = {
        OpenRouterVideoAdapter.adapter_name: OpenRouterVideoAdapter(),
        VercelGatewayVideoAdapter.adapter_name: VercelGatewayVideoAdapter(),
        VolcengineSeedanceAdapter.adapter_name: VolcengineSeedanceAdapter(),
        KlingVideoAdapter.adapter_name: KlingVideoAdapter(),
        AtlasCloudVideoAdapter.adapter_name: AtlasCloudVideoAdapter(),
        XaiVideoAdapter.adapter_name: XaiVideoAdapter(),
        GoogleInteractionsVideoAdapter.adapter_name: GoogleInteractionsVideoAdapter(),
    }
    return adapters.get(str(name or "").strip())


def video_adapter_metadata(model: str, provider: str, api_key: str) -> dict[str, Any]:
    adapter = select_video_generation_adapter(model=model, provider=provider, api_key=api_key)
    if not adapter:
        return {
            "video_provider": provider or catalog_video_provider(model),
            "video_adapter": "",
            "video_route": "",
            "native_model": native_video_model(model),
        }
    meta = adapter.metadata(model)
    return {
        "video_provider": meta.provider,
        "video_adapter": meta.adapter,
        "video_route": meta.route,
        "native_model": meta.native_model,
    }


async def _download_adapter_result(
    runtime: VideoAdapterRuntime,
    job: _JobLike,
    video_url: str,
    model: str,
    duration: int,
    resolution: str,
    *,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    from packages.core.contracts.media_job_output import media_job_output_kwargs

    params = job.params or {}
    return await runtime.download_and_save(
        video_url,
        job.prompt,
        model,
        job.id,
        job.entity_id,
        duration,
        resolution,
        output_name=params.get("output_name") or "",
        auth_headers=auth_headers,
        workspace_id=params.get("workspace_id"),
        task_id=params.get("task_id"),
        agent_id=getattr(job, "agent_id", None),
        conversation_id=getattr(job, "conversation_id", None),
        user_id=getattr(job, "user_id", None),
        with_audio=bool(params.get("generate_audio")),
        has_video_input=bool(params.get("reference_video_urls")),
        **media_job_output_kwargs(params),
    )
