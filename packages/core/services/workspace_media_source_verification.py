"""Verification of a final Workspace video against its declared source audio.

The verifier is deliberately independent of a particular creative workflow.
It pins both Knowledge Documents through the normal Workspace ACL boundary,
fully decodes the video, and compares decoded audio instead of inferring a
soundtrack from filenames, metadata, or matching file hashes.
"""
from __future__ import annotations

import asyncio
import math
import shutil
import struct
import tempfile
from array import array
from pathlib import Path
from typing import Any

from packages.core.services.workspace_document_media import (
    WorkspaceDocumentMediaError,
    inspect_workspace_document_media,
)


class MediaSourceVerificationError(ValueError):
    """The supplied media cannot be deterministically certified."""


_SAMPLE_RATE = 8_000
_MAX_DURATION_SECONDS = 600.0
_DURATION_TOLERANCE_SECONDS = 0.25
_MAX_DURATION_TOLERANCE_SECONDS = 1.0
_MAX_LAG_SECONDS = 1.0
_MIN_RMS = 0.002
_MIN_ENVELOPE_CORRELATION = 0.88
_MIN_WAVEFORM_CORRELATION = 0.80


async def _run(args: list[str], *, timeout: float, pass_fds: tuple[int, ...] = ()) -> tuple[str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, pass_fds=pass_fds,
        )
    except FileNotFoundError as exc:
        raise MediaSourceVerificationError(f"Required media executable is unavailable: {args[0]}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise MediaSourceVerificationError("Media verification timed out.") from exc
    stdout_text, stderr_text = stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
    if process.returncode:
        raise MediaSourceVerificationError(f"Media decode failed: {stderr_text[-1000:] or stdout_text[-1000:]}")
    return stdout_text, stderr_text


async def _probe(ffprobe: str, path: str, *, pass_fds: tuple[int, ...]) -> dict[str, Any]:
    import json
    stdout, _ = await _run([
        ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_entries", "format=duration,size", "-show_entries",
        "stream=codec_type,codec_name,width,height,sample_rate,channels,duration", "-of", "json", path,
    ], timeout=60, pass_fds=pass_fds)
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MediaSourceVerificationError("ffprobe returned invalid media metadata.") from exc
    streams = [stream for stream in result.get("streams", []) if isinstance(stream, dict)]
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    try:
        duration = float((result.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"duration_seconds": duration, "video": video, "audio": audio, "size_bytes": (result.get("format") or {}).get("size")}


async def _decode_all(ffmpeg: str, path: str, *, has_video: bool, pass_fds: tuple[int, ...], timeout: float) -> None:
    maps = ["-map", "0:v:0"] if has_video else ["-map", "0:a:0"]
    if has_video:
        maps += ["-map", "0:a:0"]
    await _run([ffmpeg, "-xerror", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", path, *maps, "-f", "null", "-"], timeout=timeout, pass_fds=pass_fds)


async def _decode_pcm(
    ffmpeg: str, path: str, target: str, *, start_seconds: float, duration_seconds: float, pass_fds: tuple[int, ...],
) -> None:
    await _run([
        ffmpeg, "-xerror", "-v", "error", "-protocol_whitelist", "file,pipe", "-ss", f"{start_seconds:.6f}",
        "-t", f"{duration_seconds:.6f}", "-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE),
        "-f", "f32le", target,
    ], timeout=max(90.0, duration_seconds * 8 + 30), pass_fds=pass_fds)


def _read_pcm(path: str) -> array:
    raw = Path(path).read_bytes()
    if len(raw) < 4 or len(raw) % 4:
        raise MediaSourceVerificationError("Decoded audio is empty or malformed.")
    samples = array("f")
    samples.frombytes(raw)
    if samples.itemsize != 4:
        samples = array("f", struct.unpack(f"<{len(raw) // 4}f", raw))
    return samples


def _require_complete_final_span(
    final_duration_seconds: float,
    declared_duration_seconds: float,
    tolerance_seconds: float = _DURATION_TOLERANCE_SECONDS,
) -> None:
    """Require declared source evidence to cover the complete final video."""
    deviation = abs(final_duration_seconds - declared_duration_seconds)
    if deviation > tolerance_seconds:
        raise MediaSourceVerificationError(
            "The declared source span must cover the complete final video duration "
            f"(final {final_duration_seconds:.3f}s, declared {declared_duration_seconds:.3f}s, "
            f"tolerance {tolerance_seconds:.3f}s)."
        )


def _require_decoded_audio_coverage(
    pcm_path: str,
    declared_duration_seconds: float,
    *,
    role: str,
    tolerance_seconds: float = _DURATION_TOLERANCE_SECONDS,
) -> None:
    """Require decoded signal evidence for the full declared verification span."""
    decoded_duration_seconds = len(_read_pcm(pcm_path)) / _SAMPLE_RATE
    if decoded_duration_seconds + tolerance_seconds < declared_duration_seconds:
        raise MediaSourceVerificationError(
            f"Decoded {role} audio does not cover the complete declared duration."
        )


def _normalise(samples: array) -> list[float]:
    if not samples:
        raise MediaSourceVerificationError("Decoded audio is empty.")
    mean = sum(samples) / len(samples)
    centered = [float(value) - mean for value in samples]
    rms = math.sqrt(sum(value * value for value in centered) / len(centered))
    if not math.isfinite(rms) or rms < _MIN_RMS:
        raise MediaSourceVerificationError("Decoded audio has insufficient non-silent signal.")
    return [value / rms for value in centered]


def _correlation(left: list[float], right: list[float], lag: int, *, minimum_samples: int = _SAMPLE_RATE * 2) -> float:
    if lag >= 0:
        a, b = left[lag:], right[:len(left) - lag]
    else:
        a, b = left[:len(left) + lag], right[-lag:]
    if len(a) < minimum_samples:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    left_energy = sum(x * x for x in a)
    right_energy = sum(y * y for y in b)
    if left_energy <= 0 or right_energy <= 0:
        return -1.0
    return dot / math.sqrt(left_energy * right_energy)


def _lagged(left: list[float], right: list[float], lag: int) -> tuple[list[float], list[float]]:
    """Return the overlapping spans that _correlation compares at this lag."""
    if lag >= 0:
        return left[lag:], right[:len(left) - lag]
    return left[:len(left) + lag], right[-lag:]


def _envelope(samples: list[float], *, bucket: int = 80) -> list[float]:
    return [sum(abs(value) for value in samples[index:index + bucket]) / bucket for index in range(0, len(samples) - bucket + 1, bucket)]


def _has_distinct_envelope(values: list[float]) -> bool:
    if not values:
        return False
    mean = sum(values) / len(values)
    spread = math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))
    # Codec packet boundaries can introduce a little envelope noise in an
    # otherwise sustained tone.  Treat it as waveform-only evidence instead.
    return spread > max(abs(mean) * 0.05, 0.0001)


def _sample_windows(samples: list[float], *, seconds: int = 15) -> list[list[float]]:
    width = min(len(samples), _SAMPLE_RATE * seconds)
    if width < _SAMPLE_RATE * 2:
        return []
    positions = sorted({0, max(0, (len(samples) - width) // 2), max(0, len(samples) - width)})
    return [samples[position:position + width] for position in positions]


def _coarse_waveform_lag(
    left: list[float], right: list[float], *, expected_lag: int, residual: int,
) -> int:
    """Find a bounded millisecond-scale alignment before sample-accurate scoring."""
    # Searching at 1 kHz keeps the wide ±1 second recovery range inexpensive.
    # The later full-rate pass corrects the AAC/container offsets that do not
    # land on this coarse grid.
    left_coarse, right_coarse = left[::8], right[::8]
    candidates = [
        (lag, _correlation(left_coarse, right_coarse, lag, minimum_samples=2_000))
        for lag in range(expected_lag - residual, expected_lag + residual + 1, 5)
    ]
    return max(candidates, key=lambda item: item[1])[0]


def _refine_waveform_lag(
    left: list[float], right: list[float], center: int, *, residual_samples: int,
) -> tuple[int, float]:
    """Refine an approximate lag against the decoded 8 kHz waveform.

    AAC priming and muxing commonly yield a non-integer millisecond offset.
    Retaining the strict signed-waveform threshold therefore requires a
    sample-accurate final comparison rather than reducing the threshold.
    """
    width = min(len(left), len(right), _SAMPLE_RATE * 2)
    left, right = left[:width], right[:width]
    minimum_samples = max(
        _SAMPLE_RATE,
        min(_SAMPLE_RATE * 2, width - abs(center) - residual_samples),
    )
    candidates = [
        (lag, _correlation(left, right, lag, minimum_samples=minimum_samples))
        for lag in range(center - residual_samples, center + residual_samples + 1)
    ]
    return max(candidates, key=lambda item: item[1])


def compare_decoded_audio(video_pcm: str, source_pcm: str) -> dict[str, float | int]:
    """Return alignment evidence or raise when two decoded signals do not match.

    Envelope correlation tolerates encoding, gain and fades.  A second signed
    waveform check over several deterministic windows prevents two unrelated
    clips with similar loudness envelopes from being certified.
    """
    video, source = _normalise(_read_pcm(video_pcm)), _normalise(_read_pcm(source_pcm))
    length = min(len(video), len(source))
    if length < _SAMPLE_RATE * 2:
        raise MediaSourceVerificationError("Audio comparison needs at least two seconds of signal.")
    video, source = video[:length], source[:length]
    raw_envelopes = _envelope(video), _envelope(source)
    try:
        envelopes = (_normalise(array("f", raw_envelopes[0])), _normalise(array("f", raw_envelopes[1]))) \
            if _has_distinct_envelope(raw_envelopes[0]) and _has_distinct_envelope(raw_envelopes[1]) else None
    except MediaSourceVerificationError:
        # Sustained tones can have a constant energy envelope. Their signed
        # waveform still provides evidence, so do not treat a flat envelope as
        # a mismatch.
        envelopes = None
    max_envelope_lag = int(_MAX_LAG_SECONDS * _SAMPLE_RATE / 80)
    if envelopes:
        envelope_scores = [(lag, _correlation(envelopes[0], envelopes[1], lag, minimum_samples=20)) for lag in range(-max_envelope_lag, max_envelope_lag + 1)]
        envelope_lag, envelope_score = max(envelope_scores, key=lambda item: item[1])
    else:
        envelope_lag, envelope_score = 0, 1.0
    video_windows, source_windows = _sample_windows(video), _sample_windows(source)
    if len(video_windows) != len(source_windows) or not video_windows:
        raise MediaSourceVerificationError("Audio comparison has insufficient decoded evidence.")
    if envelopes:
        # The 100 Hz envelope supplies a stable 10 ms-scale alignment even
        # when direct 1 kHz samples land at different phases.  Refine a short
        # representative window at 8 kHz, then require the same exact lag to
        # score across the complete set of windows.
        middle = len(video_windows) // 2
        waveform_lag, _ = _refine_waveform_lag(
            video_windows[middle], source_windows[middle], envelope_lag * 80,
            residual_samples=160,
        )
        # An envelope frame is 10 ms, so a few-millisecond codec offset that the
        # waveform has already measured lowers the frame-level score of matching
        # audio. Score the envelope once both signals share that alignment.
        aligned_video, aligned_source = _lagged(video, source, waveform_lag)
        envelopes = (
            _normalise(array("f", _envelope(aligned_video))),
            _normalise(array("f", _envelope(aligned_source))),
        )
        envelope_score = max(
            _correlation(envelopes[0], envelopes[1], lag, minimum_samples=20) for lag in (-1, 0, 1)
        )
        if envelope_score < _MIN_ENVELOPE_CORRELATION:
            raise MediaSourceVerificationError("The final video soundtrack does not match the declared source audio.")
        waveform_scores = [
            _correlation(left, right, waveform_lag, minimum_samples=_SAMPLE_RATE * 2)
            for left, right in zip(video_windows, source_windows, strict=True)
        ]
    else:
        # With no usable envelope, retain the bounded one-second coarse
        # waveform recovery and refine each candidate at the decoded rate.
        waveform_lags: list[int] = []
        waveform_scores = []
        for left, right in zip(video_windows, source_windows, strict=True):
            coarse_lag = _coarse_waveform_lag(left, right, expected_lag=0, residual=int(_MAX_LAG_SECONDS * 1_000))
            lag, score = _refine_waveform_lag(left, right, coarse_lag * 8, residual_samples=40)
            waveform_lags.append(lag)
            waveform_scores.append(score)
        waveform_lag = sorted(waveform_lags)[len(waveform_lags) // 2]
    # A leading/trailing fade or AAC priming window can be materially quieter
    # than the matched span.  The envelope protects the whole span; the median
    # signed-window score requires the stable majority to match as well.
    waveform_score = sorted(waveform_scores)[len(waveform_scores) // 2]
    if waveform_score < _MIN_WAVEFORM_CORRELATION:
        raise MediaSourceVerificationError("Audio waveform evidence is not strong enough to certify the declared source.")
    return {"envelope_correlation": round(envelope_score, 5), "waveform_correlation": round(waveform_score, 5), "lag_seconds": round(waveform_lag / _SAMPLE_RATE, 4), "sample_rate": _SAMPLE_RATE}


_ALIGNMENT_MEDIA_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov", ".m4v", ".webm",
}


def _require_distinct_alignment_signal(pcm_path: str, *, role: str) -> None:
    """Reject spans whose energy shape cannot establish a useful time offset."""
    samples = _normalise(_read_pcm(pcm_path))
    if not _has_distinct_envelope(_envelope(samples)):
        raise MediaSourceVerificationError(
            f"Decoded {role} audio has no distinguishable energy envelope for local alignment."
        )


async def measure_media_audio_alignment(
    *, entity_id: str, user_id: str, workspace_id: str, conversation_id: str | None, tool_name: str,
    reference_document_id: str, target_document_id: str, reference_start_seconds: float = 0.0,
    target_start_seconds: float = 0.0, duration_seconds: float = 20.0,
) -> dict[str, Any]:
    """Measure a bounded local audio offset between two readable Workspace documents.

    This is deliberately evidence for editing and subtitle timing, rather than
    final-source verification. It only describes the requested span and does
    not certify that either complete asset uses the other as its soundtrack.
    """
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaSourceVerificationError("ffmpeg and ffprobe are required for audio alignment.")
    try:
        reference_start = float(reference_start_seconds)
        target_start = float(target_start_seconds)
        requested_duration = float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise MediaSourceVerificationError("Alignment span values must be finite numbers.") from exc
    if (
        not math.isfinite(reference_start) or reference_start < 0
        or not math.isfinite(target_start) or target_start < 0
        or not math.isfinite(requested_duration) or not 5 <= requested_duration <= 60
    ):
        raise MediaSourceVerificationError("The alignment span must be 5 to 60 seconds with non-negative starts.")

    with tempfile.TemporaryDirectory(prefix="manor-media-alignment-") as directory:
        reference_pcm = str(Path(directory) / "reference.f32")
        target_pcm = str(Path(directory) / "target.f32")

        async def inspect_span(path: str, *, start_seconds: float, pcm_path: str, role: str) -> dict[str, Any]:
            fd = int(Path(path).name)
            probe = await _probe(ffprobe, path, pass_fds=(fd,))
            if not probe["audio"] or probe["duration_seconds"] < start_seconds + requested_duration:
                raise MediaSourceVerificationError(
                    f"{role.capitalize()} media does not contain the requested alignment span."
                )
            await _decode_pcm(
                ffmpeg, path, pcm_path, start_seconds=start_seconds,
                duration_seconds=requested_duration, pass_fds=(fd,),
            )
            _require_decoded_audio_coverage(pcm_path, requested_duration, role=role)
            _require_distinct_alignment_signal(pcm_path, role=role)
            return {"report": probe}

        async def inspect_reference(path: str) -> dict[str, Any]:
            return await inspect_span(
                path, start_seconds=reference_start, pcm_path=reference_pcm, role="reference",
            )

        async def inspect_target(path: str) -> dict[str, Any]:
            return await inspect_span(path, start_seconds=target_start, pcm_path=target_pcm, role="target")

        try:
            reference = await inspect_workspace_document_media(
                {"source_kind": "workspace_document", "document_id": reference_document_id},
                entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
                workspace_id=workspace_id, tool_name=tool_name,
                allowed_suffixes=_ALIGNMENT_MEDIA_SUFFIXES, max_bytes=4 * 1024 * 1024 * 1024,
                inspect_snapshot=inspect_reference,
            )
            target = await inspect_workspace_document_media(
                {"source_kind": "workspace_document", "document_id": target_document_id},
                entity_id=entity_id, user_id=user_id, conversation_id=conversation_id,
                workspace_id=workspace_id, tool_name=tool_name,
                allowed_suffixes=_ALIGNMENT_MEDIA_SUFFIXES, max_bytes=4 * 1024 * 1024 * 1024,
                inspect_snapshot=inspect_target,
            )
        except WorkspaceDocumentMediaError as exc:
            raise MediaSourceVerificationError(str(exc)) from exc
        # compare_decoded_audio's lag is expressed as its first input relative
        # to its second. Keep reference first so the documented conversion
        # below is valid even when the decoded spans retain a residual codec or
        # edit delay.
        correlation = await asyncio.to_thread(compare_decoded_audio, reference_pcm, target_pcm)

    compare_lag = float(correlation["lag_seconds"])
    return {
        "status": "completed",
        "measured": True,
        "reference": {
            "document_id": reference["document_id"],
            "sha256": reference["source_sha256"],
            "duration_seconds": reference["report"]["duration_seconds"],
        },
        "target": {
            "document_id": target["document_id"],
            "sha256": target["source_sha256"],
            "duration_seconds": target["report"]["duration_seconds"],
        },
        "analysis_span": {
            "reference_start_seconds": reference_start,
            "target_start_seconds": target_start,
            "duration_seconds": requested_duration,
        },
        # target_time = reference_time + offset. With reference as the first
        # comparison input, compare_lag is the residual reference-vs-target
        # position after the requested starts.
        "target_time_offset_seconds": round(target_start - reference_start - compare_lag, 4),
        "audio_match": correlation,
        "search_radius_seconds": _MAX_LAG_SECONDS,
        "alignment_scope": "local_constant_span_only_not_final_source_verification",
    }


async def verify_media_sources(
    *, entity_id: str, user_id: str, workspace_id: str, conversation_id: str | None, tool_name: str,
    final_video_document_id: str, expected_final_video_sha256: str, source_audio_document_id: str,
    expected_source_audio_sha256: str, source_start_seconds: float = 0.0, duration_seconds: float = 0.0,
    expected_width: int | None = None, expected_height: int | None = None,
    duration_tolerance_seconds: float | None = None,
) -> dict[str, Any]:
    """Pin, decode and prove that a final video uses a declared source audio span."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MediaSourceVerificationError("ffmpeg and ffprobe are required for source verification.")
    try:
        start, requested_duration = float(source_start_seconds), float(duration_seconds)
    except (TypeError, ValueError) as exc:
        raise MediaSourceVerificationError("source_start_seconds and duration_seconds must be finite numbers.") from exc
    if not math.isfinite(start) or start < 0 or not math.isfinite(requested_duration) or not 0 < requested_duration <= _MAX_DURATION_SECONDS:
        raise MediaSourceVerificationError("The requested source span must be between 0 and 600 seconds.")
    tolerance = _DURATION_TOLERANCE_SECONDS if duration_tolerance_seconds is None else float(duration_tolerance_seconds)
    if not math.isfinite(tolerance) or not 0 <= tolerance <= _MAX_DURATION_TOLERANCE_SECONDS:
        raise MediaSourceVerificationError(
            f"duration_tolerance_seconds must be between 0 and {_MAX_DURATION_TOLERANCE_SECONDS:g} seconds."
        )
    for digest in (expected_final_video_sha256, expected_source_audio_sha256):
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
            raise MediaSourceVerificationError("Expected source hashes must be SHA-256 hex digests.")

    with tempfile.TemporaryDirectory(prefix="manor-media-source-") as directory:
        video_pcm, source_pcm = str(Path(directory) / "video.f32"), str(Path(directory) / "source.f32")

        async def inspect_video(path: str) -> dict[str, Any]:
            fd = int(Path(path).name)
            probe = await _probe(ffprobe, path, pass_fds=(fd,))
            video, audio = probe["video"], probe["audio"]
            if not video or not audio or probe["duration_seconds"] <= 0:
                raise MediaSourceVerificationError("Final media must contain decodable video and audio streams.")
            if probe["duration_seconds"] > _MAX_DURATION_SECONDS:
                raise MediaSourceVerificationError("Final video exceeds the 600-second verification limit.")
            width, height = int(video.get("width") or 0), int(video.get("height") or 0)
            if not width or not height or (expected_width is not None and width != expected_width) or (expected_height is not None and height != expected_height):
                raise MediaSourceVerificationError("Final video dimensions do not match the declared delivery dimensions.")
            _require_complete_final_span(probe["duration_seconds"], requested_duration, tolerance)
            await _decode_all(ffmpeg, path, has_video=True, pass_fds=(fd,), timeout=max(120.0, probe["duration_seconds"] * 8 + 30))
            await _decode_pcm(ffmpeg, path, video_pcm, start_seconds=0, duration_seconds=requested_duration, pass_fds=(fd,))
            _require_decoded_audio_coverage(video_pcm, requested_duration, role="final video", tolerance_seconds=tolerance)
            return {"report": probe}

        async def inspect_audio(path: str) -> dict[str, Any]:
            fd = int(Path(path).name)
            probe = await _probe(ffprobe, path, pass_fds=(fd,))
            if not probe["audio"] or probe["duration_seconds"] < start + requested_duration:
                raise MediaSourceVerificationError("Source audio does not contain the required verified span.")
            await _decode_pcm(ffmpeg, path, source_pcm, start_seconds=start, duration_seconds=requested_duration, pass_fds=(fd,))
            _require_decoded_audio_coverage(source_pcm, requested_duration, role="source", tolerance_seconds=tolerance)
            return {"report": probe}

        try:
            video = await inspect_workspace_document_media({"source_kind": "workspace_document", "document_id": final_video_document_id}, entity_id=entity_id, user_id=user_id, conversation_id=conversation_id, workspace_id=workspace_id, tool_name=tool_name, allowed_suffixes={".mp4", ".mov", ".m4v", ".webm"}, max_bytes=4 * 1024 * 1024 * 1024, expected_source_sha256=expected_final_video_sha256.lower(), inspect_snapshot=inspect_video)
            source = await inspect_workspace_document_media({"source_kind": "workspace_document", "document_id": source_audio_document_id}, entity_id=entity_id, user_id=user_id, conversation_id=conversation_id, workspace_id=workspace_id, tool_name=tool_name, allowed_suffixes={".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov", ".m4v", ".webm"}, max_bytes=2 * 1024 * 1024 * 1024, expected_source_sha256=expected_source_audio_sha256.lower(), inspect_snapshot=inspect_audio)
        except WorkspaceDocumentMediaError as exc:
            raise MediaSourceVerificationError(str(exc)) from exc
        correlation = await asyncio.to_thread(compare_decoded_audio, video_pcm, source_pcm)
    video_report, source_report = video["report"], source["report"]
    return {
        "status": "completed", "verified": True,
        "final_video": {"document_id": video["document_id"], "sha256": video["source_sha256"], "duration_seconds": video_report["duration_seconds"], "video": video_report["video"], "audio": video_report["audio"]},
        "source_audio": {"document_id": source["document_id"], "sha256": source["source_sha256"], "duration_seconds": source_report["duration_seconds"], "audio": source_report["audio"], "start_seconds": start},
        "verified_span": {"source_start_seconds": start, "duration_seconds": requested_duration},
        # The one duration authority for the content flow: review cites this receipt, never re-measures it.
        "duration_check": {
            "declared_seconds": requested_duration,
            "final_seconds": round(video_report["duration_seconds"], 3),
            "deviation_seconds": round(abs(video_report["duration_seconds"] - requested_duration), 3),
            "tolerance_seconds": tolerance,
        },
        "audio_match": correlation,
    }
