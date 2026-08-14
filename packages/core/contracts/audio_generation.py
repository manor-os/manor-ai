"""Canonical enums and result shapes for generated audio artifacts."""

from __future__ import annotations

from enum import Enum
from typing import NotRequired, TypedDict


NARRATION_VOICE_MODE_RANDOM_PER_TASK = "random_per_task"
NARRATION_VOICE_MODE_FIXED_PER_WORKSPACE = "fixed_per_workspace"


class StringValueEnum(str, Enum):
    """String enum that is JSON-compatible and easy to project into schemas."""

    def __str__(self) -> str:
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class GenerateFileKind(StringValueEnum):
    SEARCH = "search"
    DIAGRAM = "diagram"
    CODE = "code"
    DOCUMENT = "document"
    WORD_DOCUMENT = "word_document"
    PDF = "pdf"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class AudioGenerationPurpose(StringValueEnum):
    SPEECH = "speech"
    DIALOGUE = "dialogue"
    NARRATION = "narration"
    MUSIC = "music"
    AMBIENCE = "ambience"
    SOUNDSCAPE = "soundscape"
    SFX = "sfx"
    TRANSITION = "transition"


class AudioGenerationFormat(StringValueEnum):
    MP3 = "mp3"
    WAV = "wav"
    FLAC = "flac"
    OPUS = "opus"
    PCM = "pcm"
    PCM16 = "pcm16"


class AudioGenerationStatus(StringValueEnum):
    COMPLETED = "completed"
    ERROR = "error"


class AudioGenerationErrorCode(StringValueEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_NONVOICE_AUDIO_MODEL = "unsupported_nonvoice_audio_model"
    PROVIDER_KEY_REQUIRED = "provider_key_required"
    NATIVE_MUSIC_KEY_REQUIRED = "native_music_key_required"
    AUDIO_PROVIDER_UNAVAILABLE = "audio_provider_unavailable"
    PROVIDER_BLOCKER = "provider_blocker"
    AUDIO_GENERATION_FAILED = "audio_generation_failed"
    NARRATION_VOICE_PROFILE_TASK_REQUIRED = "narration_voice_profile_task_required"
    NARRATION_VOICE_PROFILE_WORKSPACE_REQUIRED = "narration_voice_profile_workspace_required"
    NARRATION_VOICE_PROFILE_UNSUPPORTED = "narration_voice_profile_unsupported"
    NARRATION_VOICE_PROFILE_INVALID = "narration_voice_profile_invalid"


class AudioGenerationProvider(StringValueEnum):
    OPENROUTER = "openrouter"
    GOOGLE = "google"
    OPENAI = "openai"
    ZYPHRA = "zyphra"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> AudioGenerationProvider:
        normalized = str(value or "").strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            return cls.UNKNOWN


class AudioGenerationRole(StringValueEnum):
    VOICE = "voice"
    AUDIO = "audio"
    SFX = "sfx"


_AUDIO_PURPOSE_ALIASES: dict[str, AudioGenerationPurpose] = {
    "voice": AudioGenerationPurpose.SPEECH,
    "tts": AudioGenerationPurpose.SPEECH,
    "score": AudioGenerationPurpose.MUSIC,
    "bgm": AudioGenerationPurpose.MUSIC,
    "ambient": AudioGenerationPurpose.AMBIENCE,
    "background": AudioGenerationPurpose.AMBIENCE,
    "background_bed": AudioGenerationPurpose.AMBIENCE,
    "bed": AudioGenerationPurpose.AMBIENCE,
    "sound_effect": AudioGenerationPurpose.SFX,
    "sound-effect": AudioGenerationPurpose.SFX,
    "foley": AudioGenerationPurpose.SFX,
}


def normalize_audio_generation_purpose(value: object) -> AudioGenerationPurpose:
    normalized = str(value or AudioGenerationPurpose.SPEECH.value).strip().lower()
    if normalized in _AUDIO_PURPOSE_ALIASES:
        return _AUDIO_PURPOSE_ALIASES[normalized]
    try:
        return AudioGenerationPurpose(normalized)
    except ValueError:
        return AudioGenerationPurpose.SPEECH


class AudioGenerationCompletedResult(TypedDict):
    kind: GenerateFileKind
    status: AudioGenerationStatus
    provider: AudioGenerationProvider
    result_url: str
    audio_url: str
    fs_path: str | None
    prompt: str
    purpose: AudioGenerationPurpose
    model: str
    voice: str | None
    voice_instructions: str | None
    language: str
    format: AudioGenerationFormat
    provider_response_format: AudioGenerationFormat
    duration_seconds: float | None
    requested_duration_seconds: float | None
    file_size: int
    narration_profile: NotRequired[dict[str, str | int]]


class AudioGenerationErrorResult(TypedDict):
    kind: GenerateFileKind
    status: AudioGenerationStatus
    code: AudioGenerationErrorCode
    error: str
    purpose: AudioGenerationPurpose
    provider: AudioGenerationProvider
    retryable: bool
    audio_generated: bool
    model: NotRequired[str]
    provider_status: NotRequired[int | None]
    attempts: NotRequired[int]
    format_related: NotRequired[bool]
    retry_advice: NotRequired[str]
    role: NotRequired[AudioGenerationRole]


AudioGenerationResult = AudioGenerationCompletedResult | AudioGenerationErrorResult
