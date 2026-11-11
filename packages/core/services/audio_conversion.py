"""Validation and in-memory format conversion for generated audio artifacts."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tempfile
import wave


class AudioConversionError(RuntimeError):
    """Raised when generated audio is empty, malformed, or cannot be converted."""


_FORMAT_ALIASES = {
    "wave": "wav",
    "pcm16": "pcm",
}

# Formats a generated audio artifact can be stored as: each has its own file
# extension and MIME type in the artifact writer and a slot in the authorized
# media output plan.  M4A has neither, so it is never a conversion target.
SUPPORTED_AUDIO_ARTIFACT_FORMATS = frozenset(
    {
        "aac",
        "flac",
        "mp3",
        "ogg",
        "opus",
        "wav",
    }
)


def normalize_audio_format(value: str) -> str:
    normalized = str(value or "").strip().lower().lstrip(".")
    return _FORMAT_ALIASES.get(normalized, normalized)


def sniff_audio_format(audio_bytes: bytes) -> str:
    """Name the container ``audio_bytes`` actually holds, or ``""`` when unknown.

    Providers do not always honour the requested format (OpenRouter's Lyria
    returns MP3 whatever is asked for), so the bytes, not the request, decide
    which format an artifact is labelled with.
    """
    head = bytes(audio_bytes[:64]) if isinstance(audio_bytes, (bytes, bytearray)) else b""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "opus" if b"OpusHead" in head else "ogg"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF:
        # ADTS (AAC) and MPEG audio share the frame sync; ADTS has layer 00.
        if head[1] & 0xF6 == 0xF0:
            return "aac"
        if head[1] & 0xE0 == 0xE0 and (head[1] >> 1) & 0x03:
            return "mp3"
    return ""


_WAV_STREAMING_SIZES = frozenset({0, 0xFFFFFFFF})


def repair_streamed_wav_header(audio_bytes: bytes) -> bytes:
    """Fill in the sizes a streaming WAV writer leaves as placeholders.

    A WAV written to a pipe (FFmpeg ``pipe:1``, streamed provider audio) cannot
    seek back to record its length, so its RIFF and ``data`` sizes stay 0 or
    0xFFFFFFFF.  The sample data is complete; only the header needs the real
    size.  A header that already states a size is left untouched, so genuinely
    truncated audio is still rejected by validation.
    """
    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        return audio_bytes
    offset = 12
    block_align = 0
    while offset + 8 <= len(audio_bytes):
        chunk_id = audio_bytes[offset : offset + 4]
        size = int.from_bytes(audio_bytes[offset + 4 : offset + 8], "little")
        body = offset + 8
        if chunk_id == b"fmt " and size >= 16 and body + 16 <= len(audio_bytes):
            block_align = int.from_bytes(audio_bytes[body + 12 : body + 14], "little")
        if chunk_id == b"data":
            if size not in _WAV_STREAMING_SIZES:
                return audio_bytes
            available = len(audio_bytes) - body
            if block_align > 0:
                available -= available % block_align
            repaired = bytearray(audio_bytes[: body + available])
            repaired[offset + 4 : offset + 8] = available.to_bytes(4, "little")
            repaired[4:8] = (len(repaired) - 8).to_bytes(4, "little")
            return bytes(repaired)
        if size in _WAV_STREAMING_SIZES - {0}:
            return audio_bytes
        offset = body + size + (size & 1)
    return audio_bytes


async def probe_audio_duration_seconds(
    audio_bytes: bytes,
    audio_format: str,
    *,
    timeout_seconds: float = 30.0,
) -> float | None:
    """Measure decoded audio length; ``None`` when it cannot be determined.

    WAV is read from its header.  Other containers go through ``ffprobe`` on a
    temporary file, as the media tools probe Knowledge files: a piped MP3 has
    no seekable length, so ffprobe reports no duration for stdin.
    """
    fmt = normalize_audio_format(audio_format)
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
                frames, rate = wav_file.getnframes(), wav_file.getframerate()
            if frames > 0 and rate > 0:
                return round(frames / rate, 3)
        except Exception:
            pass
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not audio_bytes:
        return None
    suffix = f".{fmt}" if fmt in SUPPORTED_AUDIO_ARTIFACT_FORMATS else ""
    with tempfile.TemporaryDirectory(prefix="manor-audio-probe-") as temp_dir:
        path = os.path.join(temp_dir, f"audio{suffix}")
        with open(path, "wb") as handle:
            handle.write(audio_bytes)
        process = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", path,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return None
    if process.returncode != 0:
        return None
    try:
        duration = float((json.loads(stdout or b"{}").get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return None
    return round(duration, 3) if duration > 0 else None


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
    if target == "wav":
        # FFmpeg cannot seek back on stdout, so its WAV header carries
        # placeholder sizes that every reader treats as truncated data.
        converted = repair_streamed_wav_header(converted)
    validate_generated_audio_bytes(converted, target)
    return converted


async def speech_audio_bytes(
    audio_bytes: bytes,
    *,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    timeout_seconds: float = 180.0,
) -> bytes:
    """The span a speech-to-text upload needs: mono 16 kHz speech MP3 from ``start_seconds``.

    Recognisers hear speech and singing at 16 kHz mono, so a 39 MB studio WAV
    of a whole song becomes about 1.3 MB, and a 15 s excerpt under 100 KB, well
    inside an STT provider's upload limit. The span is seeked on the input, so
    a late excerpt does not decode the song before it.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioConversionError("FFmpeg is required to prepare audio for transcription.")
    start = max(0.0, float(start_seconds or 0.0))
    window = ["-t", f"{float(end_seconds) - start:.3f}"] if end_seconds is not None else []
    if window and float(end_seconds) <= start:
        raise AudioConversionError("The transcription window must end after it starts.")
    with tempfile.TemporaryDirectory(prefix="speech-audio-") as tmp_dir:
        # A file, not a pipe: ffmpeg can only seek a span in a file it can read back.
        source = os.path.join(tmp_dir, "source")
        with open(source, "wb") as handle:
            handle.write(audio_bytes)
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            *(["-ss", f"{start:.3f}"] if start else []), "-i", source, *window,
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            converted, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise AudioConversionError(
                f"Preparing audio for transcription timed out after {timeout_seconds:.0f} seconds."
            ) from exc
    if process.returncode != 0 or not converted:
        detail = stderr.decode("utf-8", errors="replace")[-2000:]
        raise AudioConversionError(f"FFmpeg could not prepare the audio for transcription: {detail}")
    return converted
