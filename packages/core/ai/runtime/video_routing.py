from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from packages.core.ai.runtime.skill_identity import (
    SkillCatalogKey,
    SkillInvocationFactory,
)


class VideoGenerationMode(StrEnum):
    """Stable wire values accepted by existing web and mobile clients."""

    AUTO = "auto"
    NATIVE_MOTION = "native_motion"
    AI_VIDEO = "ai_video"


class VideoOutputType(StrEnum):
    SINGLE_CLIP = "single_clip"
    CLIP = "clip"
    MULTI_CLIP_FINAL = "multi_clip_final"
    EDIT_EXISTING = "edit_existing"
    UNSUPPORTED = "unsupported"

    @property
    def supports_direct_tool_call(self) -> bool:
        return self in {self.SINGLE_CLIP, self.CLIP}


class VideoRouteKind(StrEnum):
    CODING_BASED = "coding_based_video"
    EDIT_EXISTING = "video_edit"
    AI_VIDEO = "ai_video"


VIDEO_GENERATION_MODE_ALIASES: dict[str, VideoGenerationMode] = {
    "auto": VideoGenerationMode.AUTO,
    "native": VideoGenerationMode.NATIVE_MOTION,
    "motion": VideoGenerationMode.NATIVE_MOTION,
    "native_motion": VideoGenerationMode.NATIVE_MOTION,
    "coded_motion": VideoGenerationMode.NATIVE_MOTION,
    "code_motion": VideoGenerationMode.NATIVE_MOTION,
    "code_based_video": VideoGenerationMode.NATIVE_MOTION,
    "coding_based_video": VideoGenerationMode.NATIVE_MOTION,
    "ai": VideoGenerationMode.AI_VIDEO,
    "model": VideoGenerationMode.AI_VIDEO,
    "generated": VideoGenerationMode.AI_VIDEO,
    "ai_generated": VideoGenerationMode.AI_VIDEO,
    "ai_video": VideoGenerationMode.AI_VIDEO,
}


def normalize_video_generation_mode(value: Any) -> VideoGenerationMode:
    normalized = str(value or VideoGenerationMode.AUTO).strip().lower().replace("-", "_")
    return VIDEO_GENERATION_MODE_ALIASES.get(normalized, VideoGenerationMode.AUTO)


def normalize_video_output_type(value: Any) -> VideoOutputType:
    normalized = str(value or VideoOutputType.SINGLE_CLIP).strip().lower()
    if not normalized:
        return VideoOutputType.SINGLE_CLIP
    try:
        return VideoOutputType(normalized)
    except ValueError:
        return VideoOutputType.UNSUPPORTED


@dataclass(frozen=True)
class VideoRequest:
    generation_mode: VideoGenerationMode
    output_type: VideoOutputType

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generation_mode",
            normalize_video_generation_mode(self.generation_mode),
        )
        object.__setattr__(
            self,
            "output_type",
            normalize_video_output_type(self.output_type),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> VideoRequest:
        source = payload or {}
        return cls(
            generation_mode=normalize_video_generation_mode(source.get("generation_mode")),
            output_type=normalize_video_output_type(source.get("output_type")),
        )


@dataclass(frozen=True)
class VideoToolCallContext:
    skill_input: str
    ai_video_call_factory: Callable[[], dict[str, Any]] | None = None
    skill_invocation_factory: SkillInvocationFactory | None = None


class VideoRouteStrategy(Protocol):
    request: VideoRequest
    route_kind: VideoRouteKind
    required_skill_key: SkillCatalogKey | None

    def build_direct_tool_calls(
        self,
        context: VideoToolCallContext,
    ) -> list[dict[str, Any]]: ...

    def system_prompt(self) -> str: ...


def _skill_tool_call(
    context: VideoToolCallContext,
    skill_key: SkillCatalogKey,
) -> dict[str, Any]:
    if context.skill_invocation_factory is None:
        raise ValueError("Video skill routing requires a SkillInvocationFactory")
    return context.skill_invocation_factory.build(skill_key, context.skill_input)


@dataclass(frozen=True)
class ExistingVideoEditStrategy:
    request: VideoRequest
    route_kind: VideoRouteKind = VideoRouteKind.EDIT_EXISTING
    required_skill_key: SkillCatalogKey | None = SkillCatalogKey.VIDEO_EDIT

    def build_direct_tool_calls(
        self,
        context: VideoToolCallContext,
    ) -> list[dict[str, Any]]:
        return [_skill_tool_call(context, SkillCatalogKey.VIDEO_EDIT)]

    def system_prompt(self) -> str:
        return (
            "Selected video task: edit_existing. Invoke the built-in video-edit skill. It must inspect and edit the "
            "existing video or editable project, run strict review, repair every blocking finding, and render the "
            "verified revision. Do not route a fresh video-generation request to video-edit."
        )


@dataclass(frozen=True)
class CodingBasedVideoStrategy:
    request: VideoRequest
    route_kind: VideoRouteKind = VideoRouteKind.CODING_BASED
    required_skill_key: SkillCatalogKey | None = SkillCatalogKey.CODING_BASED_VIDEO

    def build_direct_tool_calls(
        self,
        context: VideoToolCallContext,
    ) -> list[dict[str, Any]]:
        if not self.request.output_type.supports_direct_tool_call:
            return []
        return [_skill_tool_call(context, SkillCatalogKey.CODING_BASED_VIDEO)]

    def system_prompt(self) -> str:
        if self.request.generation_mode is VideoGenerationMode.NATIVE_MOTION:
            return (
                "Selected generation mode: coding-based video (legacy wire value: native_motion). Do not call a "
                "video-generation model or generate_file(kind='video'). Invoke the built-in coding-based-video skill and "
                "let it own the complete generation workflow. It must build an editable coded composition, run strict "
                "checks, repair every blocking finding, generate review snapshots, and render the verified final. Do not substitute "
                "manor.video_edit_recipe or a fixed motion preset unless the video runtime is unavailable and the user "
                "explicitly accepts the lower-fidelity fast/editable fallback. Do not stop at a prose plan."
            )
        return (
            "Selected generation mode: auto. Decide per scene instead of routing every video request to a video model. "
            "For a fresh product demo, UI walkthrough, promo, explainer, captioned composition, or any final containing "
            "UI, typography, diagrams, particles, product animation, or brand motion, invoke the built-in "
            "coding-based-video skill once and let it own generation, quality review, repair, and render. Invoke video-edit "
            "only when the user is editing an existing video or editable project. "
            "Use generate_file(kind='video') only for photorealistic people, environments, or footage that cannot be built "
            "efficiently as motion graphics; the coding-based composition may incorporate those completed clips as replaceable "
            "assets. Do not silently fall back to manor.video_edit_recipe or a fixed motion preset. For every AI-generated clip, "
            "call wait_media_jobs and wait for the real result before continuing."
        )


@dataclass(frozen=True)
class AiVideoStrategy:
    request: VideoRequest
    route_kind: VideoRouteKind = VideoRouteKind.AI_VIDEO
    required_skill_key: SkillCatalogKey | None = None

    def build_direct_tool_calls(
        self,
        context: VideoToolCallContext,
    ) -> list[dict[str, Any]]:
        if not self.request.output_type.supports_direct_tool_call:
            return []
        if context.ai_video_call_factory is None:
            raise ValueError("AI video routing requires an AI video tool-call factory")
        return [context.ai_video_call_factory()]

    def system_prompt(self) -> str:
        return (
            "Selected generation mode: ai_video. Generate new footage with generate_file(kind='video'). Video "
            "generation is async and returns status='pending' with a job_id. You MUST call wait_media_jobs with "
            "that job_id, then report the completed video or the real failure reason. Never claim success while a "
            "video job is pending. Keep generated clips replaceable in the final edit when the request is a composed video."
        )


class VideoRouteFactory:
    _generation_strategies: dict[
        VideoGenerationMode,
        type[CodingBasedVideoStrategy] | type[AiVideoStrategy],
    ] = {
        VideoGenerationMode.AUTO: CodingBasedVideoStrategy,
        VideoGenerationMode.NATIVE_MOTION: CodingBasedVideoStrategy,
        VideoGenerationMode.AI_VIDEO: AiVideoStrategy,
    }

    @classmethod
    def resolve(cls, request: VideoRequest) -> VideoRouteStrategy:
        if request.output_type is VideoOutputType.EDIT_EXISTING:
            return ExistingVideoEditStrategy(request)
        strategy_type = cls._generation_strategies[request.generation_mode]
        return strategy_type(request)


def resolve_video_route(
    payload: Mapping[str, Any] | None,
) -> VideoRouteStrategy:
    return VideoRouteFactory.resolve(VideoRequest.from_payload(payload))
