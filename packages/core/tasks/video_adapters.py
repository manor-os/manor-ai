"""Provider adapters for video generation jobs.

This module keeps provider-specific request shapes out of the media job
orchestration code. The orchestration layer owns database state, billing, and
artifact storage; adapters own provider routing and payload semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


VIDEO_NATIVE_MODEL_MAP = {
    "bytedance/seedance-2.0": "doubao-seedance-2-0-260128",
    "bytedance/seedance-2.0-fast": "doubao-seedance-2-0-fast-260128",
    "kwaivgi/kling-v3.0-std": "kling-v3.0-std",
    "kwaivgi/kling-v3.0-pro": "kling-v3.0-pro",
}


def catalog_video_provider(model: str) -> str:
    return (model or "").split("/", 1)[0].strip().lower() if "/" in (model or "") else ""


def native_video_model(model: str) -> str:
    raw = (model or "").split("/", 1)[1] if "/" in (model or "") else (model or "")
    return VIDEO_NATIVE_MODEL_MAP.get(model, raw)


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
    normalize_duration: Callable[[Any], int]
    normalize_resolution: Callable[[str | None, Any], str]


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
        )
        if frame_images:
            payload["frame_images"] = frame_images

        reference_urls = params.get("reference_urls") or []
        if reference_urls:
            refs = []
            for ref_url in reference_urls[:9]:
                refs.append({
                    "type": "image_url",
                    "image_url": {
                        "url": await runtime.ensure_public_url(
                            ref_url,
                            job.entity_id,
                            **runtime.public_url_kwargs(public_base_url),
                        )
                    },
                })
            payload["input_references"] = refs

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://manor.ai",
            "X-Title": "Manor AI",
        }
        api_base = "https://openrouter.ai/api/v1"
        async with runtime.http_client_cls(timeout=runtime.media_api_timeout) as client:
            resp = await client.post(f"{api_base}/videos", headers=headers, json=payload)
            data = resp.json()

        logger.info("Video job %s OpenRouter response (%d): %s", job.id, resp.status_code, str(data)[:500])
        if resp.status_code not in (200, 201, 202):
            err = data.get("error", {})
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            return {"error": f"Video generation failed ({resp.status_code}): {msg}{provider_error_hint(resp.status_code, msg)}"}

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
        duration = runtime.normalize_duration(params.get("duration", 5))
        resolution = runtime.normalize_resolution(native_model, params.get("resolution", "720p"))
        aspect_ratio = params.get("aspect_ratio", "16:9")
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

        reference_urls = params.get("reference_urls") or []
        if reference_urls and not frame_control_blocks_reference_media:
            for ref_url in reference_urls[:9]:
                content.append(await seedance_image_content(
                    ref_url,
                    job.entity_id,
                    "reference_image",
                    runtime=runtime,
                    public_base_url=public_base_url,
                ))
        reference_video_urls = params.get("reference_video_urls") or []
        if reference_video_urls and not frame_control_blocks_reference_media:
            for ref_url in reference_video_urls[:3]:
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
            for ref_url in audio_reference_urls[:3]:
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
        if params.get("seed") is not None:
            payload["seed"] = params["seed"]
        for key in ("return_last_frame", "camera_fixed", "draft"):
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
            return {"error": f"Seedance generation failed ({resp.status_code}): {msg}{hint}"}

        task_id = runtime.extract_task_id(data)
        video_url = runtime.extract_video_url(data)
        if not video_url and task_id:
            poll_url = f"{base}/contents/generations/tasks/{task_id}"
            await runtime.remember_provider_poll(job.id, self.poll_provider, poll_url, task_id)
            video_url = await runtime.poll_volcengine_task(poll_url, headers)
        if not video_url:
            return {"error": "No video URL in Seedance response", "raw": str(data)[:500]}

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
            return {"error": f"Kling generation failed ({resp.status_code}): {msg}{provider_error_hint(resp.status_code, msg)}"}

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
    item: dict[str, Any] = {
        "type": field,
        field: {
            "url": await runtime.ensure_public_url(
                url,
                entity_id,
                **runtime.public_url_kwargs(public_base_url),
            )
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
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    first_frame = params.get("first_frame_url", "")
    last_frame = params.get("last_frame_url", "")
    if first_frame:
        frames.append({
            "type": "image_url",
            "image_url": {
                "url": await runtime.ensure_public_url(
                    first_frame,
                    entity_id,
                    **runtime.public_url_kwargs(public_base_url),
                )
            },
            "frame_type": "first_frame",
        })
    if last_frame:
        frames.append({
            "type": "image_url",
            "image_url": {
                "url": await runtime.ensure_public_url(
                    last_frame,
                    entity_id,
                    **runtime.public_url_kwargs(public_base_url),
                )
            },
            "frame_type": "last_frame",
        })
    return frames


def select_video_generation_adapter(
    *,
    model: str,
    provider: str,
    api_key: str,
) -> VideoGenerationAdapter | None:
    if (api_key or "").startswith("sk-or-"):
        return OpenRouterVideoAdapter()
    selected_provider = (provider or catalog_video_provider(model)).lower()
    if selected_provider == "bytedance":
        return VolcengineSeedanceAdapter()
    if selected_provider == "kwaivgi":
        return KlingVideoAdapter()
    return None


def video_adapter_by_name(name: str) -> VideoGenerationAdapter | None:
    adapters: dict[str, VideoGenerationAdapter] = {
        OpenRouterVideoAdapter.adapter_name: OpenRouterVideoAdapter(),
        VolcengineSeedanceAdapter.adapter_name: VolcengineSeedanceAdapter(),
        KlingVideoAdapter.adapter_name: KlingVideoAdapter(),
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
    )
