"""Validation and in-memory format conversion for generated audio artifacts."""

from __future__ import annotations

import asyncio
import io
import shutil
import wave


class AudioConversionError(RuntimeError):
    """Raised when generated audio is empty, malformed, or cannot be converted."""


_FORMAT_ALIASES = {
    "wave": "wav",
    "pcm16": "pcm",
}

SUPPORTED_AUDIO_ARTIFACT_FORMATS = frozenset(
    {
        "aac",
        "flac",
        "m4a",
        "mp3",
        "ogg",
        "opus",
        "wav",
    }
)


def normalize_audio_format(value: str) -> str:
    normalized = str(value or "").strip().lower().lstrip(".")
    return _FORMAT_ALIASES.get(normalized, normalized)


def _pcm_has_signal(pcm_bytes: bytes, *, sample_width: int) -> bool:
    if sample_width == 1:
        return any(sample != 128 for sample in pcm_bytes)
    return any(
        int.from_bytes(
            pcm_bytes[offset : offset + sample_width],
            "little",
            signed=True,
        )
        != 0
        for offset in range(0, len(pcm_bytes), sample_width)
    )


def validate_generated_audio_bytes(audio_bytes: bytes, audio_format: str) -> None:
    """Reject empty containers and structurally invalid generated audio."""
    if not isinstance(audio_bytes, bytes) or not audio_bytes:
        raise AudioConversionError("Audio provider returned an empty audio response.")

    fmt = normalize_audio_format(audio_format)
    if fmt == "pcm":
        if len(audio_bytes) % 2:
            raise AudioConversionError("Audio provider returned truncated PCM16 sample data.")
        if not _pcm_has_signal(audio_bytes, sample_width=2):
            raise AudioConversionError("Audio provider returned PCM with no audible signal.")
        return

    if fmt == "wav":
        if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
            raise AudioConversionError("Audio provider returned an invalid WAV container.")
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
                frame_count = wav_file.getnframes()
                channel_count = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                if frame_count <= 0:
                    raise AudioConversionError("Audio provider returned a WAV file with zero audio frames.")
                pcm_bytes = wav_file.readframes(frame_count)
        except AudioConversionError:
            raise
        except Exception as exc:
            raise AudioConversionError(f"Audio provider returned a malformed WAV file: {exc}") from exc
        expected_size = frame_count * channel_count * sample_width
        if len(pcm_bytes) != expected_size:
            raise AudioConversionError("Audio provider returned truncated WAV frame data.")
        if not _pcm_has_signal(pcm_bytes, sample_width=sample_width):
            raise AudioConversionError("Audio provider returned WAV audio with no audible signal.")
        return

    if fmt == "mp3" and not (
        audio_bytes.startswith(b"ID3")
        or (len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and audio_bytes[1] & 0xE0 == 0xE0)
    ):
        raise AudioConversionError("Audio provider returned an invalid MP3 stream.")
    if fmt == "flac" and not audio_bytes.startswith(b"fLaC"):
        raise AudioConversionError("Audio provider returned an invalid FLAC stream.")
    if fmt in {"ogg", "opus"} and not audio_bytes.startswith(b"OggS"):
        raise AudioConversionError("Audio provider returned an invalid Ogg/Opus stream.")
    if fmt == "aac" and not (len(audio_bytes) >= 2 and audio_bytes[0] == 0xFF and audio_bytes[1] & 0xF6 == 0xF0):
        raise AudioConversionError("Audio provider returned an invalid AAC stream.")


def ffmpeg_audio_codec_args(audio_format: str) -> list[str]:
    """Return the shared FFmpeg codec settings used by Manor audio tools."""
    if audio_format == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if audio_format == "wav":
        return ["-c:a", "pcm_s16le"]
    if audio_format == "flac":
        return ["-c:a", "flac"]
    if audio_format == "opus":
        return ["-c:a", "libopus", "-b:a", "128k"]
    if audio_format == "ogg":
        return ["-c:a", "libvorbis", "-q:a", "5"]
    if audio_format == "aac":
        return ["-c:a", "aac", "-b:a", "192k"]
    if audio_format == "m4a":
        return ["-c:a", "aac", "-b:a", "192k"]
    return ["-c:a", "pcm_s16le"]


def _ffmpeg_output_args(audio_format: str) -> list[str]:
    muxer = {
        "aac": "adts",
        "flac": "flac",
        "m4a": "mp4",
        "mp3": "mp3",
        "ogg": "ogg",
        "opus": "opus",
        "wav": "wav",
    }.get(audio_format)
    if not muxer:
        raise AudioConversionError(f"Unsupported audio output format: {audio_format or '(empty)'}")
    extra_args = ["-movflags", "frag_keyframe+empty_moov"] if audio_format == "m4a" else []
    return [*ffmpeg_audio_codec_args(audio_format), *extra_args, "-f", muxer]


async def transcode_audio_bytes(
    audio_bytes: bytes,
    *,
    source_format: str,
    target_format: str,
    timeout_seconds: float = 120.0,
) -> bytes:
    """Convert generated audio through the FFmpeg runtime already used by media tools."""
    source = normalize_audio_format(source_format)
    target = normalize_audio_format(target_format)
    validate_generated_audio_bytes(audio_bytes, source)
    if source == target:
        return audio_bytes
    if target not in SUPPORTED_AUDIO_ARTIFACT_FORMATS:
        raise AudioConversionError(f"Unsupported audio output format: {target or '(empty)'}")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioConversionError(
            f"FFmpeg is required to convert generated {source.upper()} audio to {target.upper()}."
        )

    input_args: list[str] = []
    if source == "pcm":
        input_args = ["-f", "s16le", "-ar", "24000", "-ac", "1"]
    args = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *input_args,
        "-i",
        "pipe:0",
        "-vn",
        *_ffmpeg_output_args(target),
        "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        converted, stderr = await asyncio.wait_for(
            process.communicate(audio_bytes),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise AudioConversionError(f"Audio format conversion timed out after {timeout_seconds:.0f} seconds.") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-2000:]
        raise AudioConversionError(
            f"FFmpeg could not convert generated {source.upper()} audio to {target.upper()}: {detail}"
        )
    validate_generated_audio_bytes(converted, target)
    return converted
