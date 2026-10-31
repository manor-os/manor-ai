"""Regression coverage for decoded soundtrack matching used by Workspace media gates."""
from __future__ import annotations

import asyncio
import math
import os
import random
import shutil
import subprocess
from array import array
from pathlib import Path
from typing import Any

import pytest

from packages.core.services.workspace_document_media import WorkspaceDocumentMediaError
from packages.core.services.workspace_media_source_verification import (
    MediaSourceVerificationError,
    _decode_pcm,
    _require_complete_final_span,
    _require_decoded_audio_coverage,
    compare_decoded_audio,
    measure_media_audio_alignment,
    verify_media_sources,
)


def _write_pcm(path: Path, values: list[float]) -> None:
    path.write_bytes(array("f", values).tobytes())


def test_decoded_audio_comparison_is_gain_and_small_alignment_tolerant_but_fails_closed_for_silence(tmp_path):
    samples = [(0.25 + 0.75 * (index % 4_000) / 4_000) * (math.sin(index / 17) + 0.3 * math.sin(index / 7)) for index in range(8_000 * 3)]
    # A re-encoder can delay a stream; this has a 100 ms leading offset and lower gain.
    shifted = [0.0] * 800 + [value * 0.35 for value in samples[:-800]]
    video, source = tmp_path / "video.f32", tmp_path / "source.f32"
    _write_pcm(video, shifted)
    _write_pcm(source, samples)
    assert compare_decoded_audio(str(video), str(source))["waveform_correlation"] >= 0.80
    _write_pcm(source, [0.0] * len(samples))
    with pytest.raises(MediaSourceVerificationError, match="insufficient non-silent"):
        compare_decoded_audio(str(video), str(source))


def test_decoded_audio_comparison_refines_non_integer_millisecond_codec_offsets(tmp_path):
    # AAC priming/muxing can move a matching soundtrack by a fraction of the
    # verifier's former 5 ms coarse-search grid. The strict waveform threshold
    # must still certify the source at the decoded sample offset.
    generator = random.Random(0x5EED)
    samples = []
    for index in range(8_000 * 4):
        noise = generator.uniform(-1.0, 1.0)
        samples.append((0.4 + 0.6 * math.sin(index / 2_417)) * noise)
    offset = 171  # 21.375 ms at the verifier's 8 kHz decoded rate.
    video, source, wrong = tmp_path / "video.f32", tmp_path / "source.f32", tmp_path / "wrong.f32"
    _write_pcm(video, samples[offset:] + [0.0] * offset)
    _write_pcm(source, samples)
    _write_pcm(wrong, list(reversed(samples)))

    evidence = compare_decoded_audio(str(video), str(source))

    assert evidence["waveform_correlation"] >= 0.80
    assert evidence["lag_seconds"] == pytest.approx(-offset / 8_000, abs=0.0002)
    with pytest.raises(MediaSourceVerificationError):
        compare_decoded_audio(str(video), str(wrong))


def test_decoded_audio_envelope_is_scored_after_a_sub_frame_codec_offset(tmp_path):
    # A 5 ms codec delay is half an envelope frame (10 ms). Scoring the envelope
    # before the signals share that alignment rejected this matching soundtrack.
    generator = random.Random(0x5EED)
    gate, level = [], 1.0
    for index in range(8_000 * 6):
        if index % 23 == 0:
            level = generator.choice([0.02, 1.0])
        gate.append(level)
    samples = [gain * generator.uniform(-1.0, 1.0) for gain in gate]
    offset = 40  # 5 ms at the verifier's 8 kHz decoded rate.
    video, source = tmp_path / "video.f32", tmp_path / "source.f32"
    _write_pcm(video, samples[offset:] + [0.0] * offset)
    _write_pcm(source, samples)

    evidence = compare_decoded_audio(str(video), str(source))

    assert evidence["envelope_correlation"] >= 0.88
    assert evidence["lag_seconds"] == pytest.approx(-offset / 8_000, abs=0.0002)


def test_declared_source_span_must_cover_the_entire_final_video():
    _require_complete_final_span(30.18, 30.0)
    with pytest.raises(MediaSourceVerificationError, match="complete final video duration"):
        _require_complete_final_span(30.3, 3.0)
    # The tolerance is the caller's contract: the same deviation passes or fails by it.
    _require_complete_final_span(29.9, 30.0, 0.25)
    with pytest.raises(MediaSourceVerificationError, match="complete final video duration"):
        _require_complete_final_span(29.9, 30.0, 0.05)


def test_duration_tolerance_is_bounded_before_any_media_is_read(monkeypatch):
    from packages.core.services import workspace_media_source_verification as verification

    monkeypatch.setattr(verification.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(MediaSourceVerificationError, match="duration_tolerance_seconds must be between"):
        asyncio.run(verify_media_sources(
            entity_id="entity-1", user_id="user-1", workspace_id="workspace-1", conversation_id=None,
            tool_name="verify_media_sources", final_video_document_id="document-final",
            expected_final_video_sha256="a" * 64, source_audio_document_id="document-source",
            expected_source_audio_sha256="b" * 64, source_start_seconds=0.0, duration_seconds=30.0,
            duration_tolerance_seconds=2.0,
        ))


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("ffmpeg is required for decoded media verification")
    try:
        subprocess.run([executable, "-version"], check=True, capture_output=True)
    except OSError:
        pytest.skip("the installed ffmpeg is not executable on this host")
    return executable


def _ffmpeg(*args: str) -> None:
    executable = _ffmpeg_executable()
    subprocess.run([executable, "-y", "-v", "error", *args], check=True, capture_output=True)


async def _decode(input_path: Path, output_path: Path, *, start: float = 0, duration: float = 3) -> None:
    executable = _ffmpeg_executable()
    await _decode_pcm(executable, str(input_path), str(output_path), start_seconds=start, duration_seconds=duration, pass_fds=())


@pytest.mark.media_integration
def test_decoded_audio_match_tolerates_aac_reencode_and_source_offset_but_rejects_wrong_or_silent_audio(tmp_path):
    _ffmpeg_executable()
    source = tmp_path / "source.wav"
    video = tmp_path / "video.mp4"
    wrong = tmp_path / "wrong.wav"
    silent = tmp_path / "silent.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=431:sample_rate=48000:duration=6", str(source))
    # The final starts one second into the source and AAC re-encodes the audio.
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3", "-ss", "1", "-t", "3", "-i", str(source), "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=997:sample_rate=48000:duration=3", str(wrong))
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "3", str(silent))

    video_pcm, source_pcm = tmp_path / "video.f32", tmp_path / "source.f32"
    asyncio.run(_decode(video, video_pcm))
    asyncio.run(_decode(source, source_pcm, start=1))
    evidence = compare_decoded_audio(str(video_pcm), str(source_pcm))
    assert evidence["envelope_correlation"] >= 0.88
    assert evidence["waveform_correlation"] >= 0.80

    wrong_pcm, silent_pcm = tmp_path / "wrong.f32", tmp_path / "silent.f32"
    asyncio.run(_decode(wrong, wrong_pcm))
    asyncio.run(_decode(silent, silent_pcm))
    with pytest.raises(MediaSourceVerificationError):
        compare_decoded_audio(str(video_pcm), str(wrong_pcm))
    with pytest.raises(MediaSourceVerificationError):
        compare_decoded_audio(str(video_pcm), str(silent_pcm))


@pytest.mark.media_integration
def test_decoded_audio_coverage_rejects_a_final_video_with_a_short_audio_tail(tmp_path):
    _ffmpeg_executable()
    source = tmp_path / "source.wav"
    video = tmp_path / "short-audio.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i",
        "aevalsrc=0.5*(0.2+0.8*sin(2*PI*t/3))*sin(2*PI*431*t):s=48000:d=10",
        str(source),
    )
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=10",
        "-t", "2", "-i", str(source), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-c:a", "aac", str(video),
    )

    video_pcm, source_pcm = tmp_path / "video.f32", tmp_path / "source.f32"
    asyncio.run(_decode(video, video_pcm, duration=10))
    asyncio.run(_decode(source, source_pcm, duration=10))

    # The shared prefix matches; without coverage validation this would certify
    # a ten-second final video whose soundtrack ends after two seconds.
    assert compare_decoded_audio(str(video_pcm), str(source_pcm))["waveform_correlation"] >= 0.80
    with pytest.raises(MediaSourceVerificationError, match="final video audio does not cover"):
        _require_decoded_audio_coverage(str(video_pcm), 10, role="final video")


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace_media_snapshot_stub(documents: dict[str, Path]):
    async def inspect(reference: Any, **kwargs: Any) -> dict[str, Any]:
        document_id = reference["document_id"]
        path = documents[document_id]
        if path.suffix.lower() not in kwargs["allowed_suffixes"]:
            raise WorkspaceDocumentMediaError("The selected Workspace document has an unsupported media type.")
        expected_hash = kwargs.get("expected_source_sha256")
        digest = _sha256(path)
        if expected_hash != digest:
            raise WorkspaceDocumentMediaError("The Workspace Knowledge media has changed.")
        with path.open("rb") as handle:
            details = await kwargs["inspect_snapshot"](f"/dev/fd/{handle.fileno()}")
        return {
            "document_id": document_id,
            "source_sha256": digest,
            "size_bytes": path.stat().st_size,
            **details,
        }

    return inspect


def _alignment_environment(monkeypatch, tmp_path, tracks: dict[str, list[float]]):
    """Use pinned descriptor paths while keeping alignment tests independent of ffmpeg."""
    documents = {}
    calls = []
    for document_id in tracks:
        path = tmp_path / f"{document_id}.wav"
        path.write_text(document_id)
        documents[document_id] = path

    async def inspect(reference: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((reference, kwargs))
        document_id = reference["document_id"]
        path = documents[document_id]
        if path.suffix.lower() not in kwargs["allowed_suffixes"]:
            raise WorkspaceDocumentMediaError("The selected Workspace document has an unsupported media type.")
        with path.open("rb") as handle:
            details = await kwargs["inspect_snapshot"](f"/dev/fd/{handle.fileno()}")
        return {"document_id": document_id, "source_sha256": _sha256(path), **details}

    async def probe(_ffprobe: str, descriptor_path: str, *, pass_fds: tuple[int, ...]):
        descriptor = int(Path(descriptor_path).name)
        assert pass_fds == (descriptor,)
        os.lseek(descriptor, 0, os.SEEK_SET)
        document_id = Path(descriptor_path).read_text()
        os.lseek(descriptor, 0, os.SEEK_SET)
        return {"duration_seconds": len(tracks[document_id]) / 8_000, "audio": {"codec_name": "pcm_f32le"}, "video": None}

    async def decode(_ffmpeg: str, descriptor_path: str, pcm_path: str, *, start_seconds: float, duration_seconds: float, pass_fds: tuple[int, ...]):
        descriptor = int(Path(descriptor_path).name)
        assert pass_fds == (descriptor,)
        os.lseek(descriptor, 0, os.SEEK_SET)
        track = tracks[Path(descriptor_path).read_text()]
        start, end = int(start_seconds * 8_000), int((start_seconds + duration_seconds) * 8_000)
        _write_pcm(Path(pcm_path), track[start:end])

    monkeypatch.setattr("packages.core.services.workspace_media_source_verification.inspect_workspace_document_media", inspect)
    monkeypatch.setattr("packages.core.services.workspace_media_source_verification._probe", probe)
    monkeypatch.setattr("packages.core.services.workspace_media_source_verification._decode_pcm", decode)
    monkeypatch.setattr("packages.core.services.workspace_media_source_verification.shutil.which", lambda _name: "/test/media-tool")
    return calls, documents


@pytest.mark.parametrize(("offset_seconds", "gain"), [(2.0, 0.35), (-2.0, 0.65)])
def test_local_media_alignment_reports_signed_offset_from_pinned_recording_and_screenclip(
    monkeypatch, tmp_path, offset_seconds, gain,
):
    generator = random.Random(7721)
    reference = [
        (0.15 + 0.85 * ((index % 9_001) / 9_001)) * generator.uniform(-1.0, 1.0)
        for index in range(8_000 * 40)
    ]
    shift = int(abs(offset_seconds) * 8_000)
    target = (
        [0.0] * shift + [sample * gain for sample in reference[:-shift]]
        if offset_seconds > 0
        else [sample * gain for sample in reference[shift:]] + [0.0] * shift
    )
    calls, documents = _alignment_environment(monkeypatch, tmp_path, {
        "field-recording": reference,
        "screenclip": target,
    })

    result = asyncio.run(measure_media_audio_alignment(
        entity_id="entity", user_id="user", workspace_id="workspace", conversation_id="conversation",
        tool_name="measure_media_audio_alignment", reference_document_id="field-recording",
        target_document_id="screenclip", reference_start_seconds=8,
        target_start_seconds=8 + offset_seconds, duration_seconds=6,
    ))

    assert result["status"] == "completed"
    assert result["measured"] is True
    assert result["target_time_offset_seconds"] == pytest.approx(offset_seconds, abs=0.002)
    assert result["reference"]["sha256"] == _sha256(documents["field-recording"])
    assert result["target"]["sha256"] == _sha256(documents["screenclip"])
    assert result["audio_match"]["waveform_correlation"] >= 0.80
    assert result["search_radius_seconds"] == 1.0
    assert result["alignment_scope"] == "local_constant_span_only_not_final_source_verification"
    assert [call[0]["document_id"] for call in calls] == ["field-recording", "screenclip"]
    assert all(call[1]["workspace_id"] == "workspace" for call in calls)


@pytest.mark.parametrize(
    ("reference_start", "target_start", "expected_offset"),
    [(10.0, 12.0, 2.25), (10.0, 8.0, -1.75)],
)
def test_local_media_alignment_combines_unequal_anchors_with_remaining_decode_lag(
    monkeypatch, tmp_path, reference_start, target_start, expected_offset,
):
    generator = random.Random(917)
    reference = [
        (0.25 + 0.75 * ((index % 7_003) / 7_003)) * generator.uniform(-1.0, 1.0)
        for index in range(8_000 * 45)
    ]
    shift = int(expected_offset * 8_000)
    target = (
        [0.0] * shift + reference[:-shift]
        if shift > 0
        else reference[-shift:] + [0.0] * -shift
    )
    _alignment_environment(monkeypatch, tmp_path, {"recording": reference, "screenclip": target})

    result = asyncio.run(measure_media_audio_alignment(
        entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
        tool_name="measure_media_audio_alignment", reference_document_id="recording",
        target_document_id="screenclip", reference_start_seconds=reference_start,
        target_start_seconds=target_start, duration_seconds=6,
    ))

    # The target span leads the requested reference span by 250 ms. The
    # returned comparison lag must compensate that residual rather than merely
    # reporting the difference between requested starts.
    assert result["audio_match"]["lag_seconds"] == pytest.approx(-0.25, abs=0.002)
    assert result["target_time_offset_seconds"] == pytest.approx(expected_offset, abs=0.002)


def test_local_media_alignment_rejects_ambiguous_or_unmatched_audio(monkeypatch, tmp_path):
    silence = [0.0] * (8_000 * 15)
    _alignment_environment(monkeypatch, tmp_path, {"recording": silence, "screenclip": silence})
    with pytest.raises(MediaSourceVerificationError, match="insufficient non-silent"):
        asyncio.run(measure_media_audio_alignment(
            entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
            tool_name="measure_media_audio_alignment", reference_document_id="recording",
            target_document_id="screenclip", duration_seconds=5,
        ))

    tone = [math.sin(2 * math.pi * 431 * index / 8_000) for index in range(8_000 * 15)]
    _alignment_environment(monkeypatch, tmp_path, {"recording": tone, "screenclip": tone})
    with pytest.raises(MediaSourceVerificationError, match="distinguishable energy envelope"):
        asyncio.run(measure_media_audio_alignment(
            entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
            tool_name="measure_media_audio_alignment", reference_document_id="recording",
            target_document_id="screenclip", duration_seconds=5,
        ))

    generator = random.Random(31)
    reference = [generator.uniform(-1.0, 1.0) for _ in range(8_000 * 15)]
    target = list(reversed(reference))
    _alignment_environment(monkeypatch, tmp_path, {"recording": reference, "screenclip": target})
    with pytest.raises(MediaSourceVerificationError, match="does not match|not strong enough"):
        asyncio.run(measure_media_audio_alignment(
            entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
            tool_name="measure_media_audio_alignment", reference_document_id="recording",
            target_document_id="screenclip", duration_seconds=5,
        ))


@pytest.mark.media_integration
@pytest.mark.parametrize("source_suffix", [".mp4", ".mov", ".m4v"])
def test_video_containers_with_a_decodable_interview_soundtrack_are_valid_source_audio(
    monkeypatch, tmp_path, source_suffix,
):
    _ffmpeg_executable()
    source = tmp_path / f"interview-original-soundtrack{source_suffix}"
    final = tmp_path / "interview-promo.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=431:sample_rate=48000:duration=3",
        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
    )
    _ffmpeg("-i", str(source), "-c", "copy", str(final))
    documents = {"final": final, "source": source}
    monkeypatch.setattr(
        "packages.core.services.workspace_media_source_verification.inspect_workspace_document_media",
        _workspace_media_snapshot_stub(documents),
    )

    result = asyncio.run(verify_media_sources(
        entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
        tool_name="verify_media_sources", final_video_document_id="final",
        expected_final_video_sha256=_sha256(final), source_audio_document_id="source",
        expected_source_audio_sha256=_sha256(source), duration_seconds=3,
    ))

    assert result["verified"] is True
    assert result["source_audio"]["document_id"] == "source"
    assert result["audio_match"]["waveform_correlation"] >= 0.80


@pytest.mark.media_integration
def test_source_video_without_an_audio_stream_is_rejected(monkeypatch, tmp_path):
    _ffmpeg_executable()
    final = tmp_path / "final.mp4"
    silent_source = tmp_path / "silent-interview.mov"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=431:sample_rate=48000:duration=3",
        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(final),
    )
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3", "-c:v", "libx264", str(silent_source))
    documents = {"final": final, "source": silent_source}
    monkeypatch.setattr(
        "packages.core.services.workspace_media_source_verification.inspect_workspace_document_media",
        _workspace_media_snapshot_stub(documents),
    )

    with pytest.raises(MediaSourceVerificationError, match="Source audio does not contain"):
        asyncio.run(verify_media_sources(
            entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
            tool_name="verify_media_sources", final_video_document_id="final",
            expected_final_video_sha256=_sha256(final), source_audio_document_id="source",
            expected_source_audio_sha256=_sha256(silent_source), duration_seconds=3,
        ))


@pytest.mark.media_integration
def test_unknown_source_media_suffix_is_rejected_before_decoding(monkeypatch, tmp_path):
    _ffmpeg_executable()
    final = tmp_path / "final.mp4"
    unsupported_source = tmp_path / "interview-source.bin"
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=431:sample_rate=48000:duration=3",
        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(final),
    )
    unsupported_source.write_bytes(final.read_bytes())
    documents = {"final": final, "source": unsupported_source}
    monkeypatch.setattr(
        "packages.core.services.workspace_media_source_verification.inspect_workspace_document_media",
        _workspace_media_snapshot_stub(documents),
    )

    with pytest.raises(MediaSourceVerificationError, match="unsupported media type"):
        asyncio.run(verify_media_sources(
            entity_id="entity", user_id="user", workspace_id="workspace", conversation_id=None,
            tool_name="verify_media_sources", final_video_document_id="final",
            expected_final_video_sha256=_sha256(final), source_audio_document_id="source",
            expected_source_audio_sha256=_sha256(unsupported_source), duration_seconds=3,
        ))
