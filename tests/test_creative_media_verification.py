"""A clean probe must not certify media whose packets fail full decoding."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import creative_studio_tools as studio


@pytest.mark.media_integration
@pytest.mark.oss_regression
async def test_corrupt_audio_packets_fail_before_verification_receipt(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("The actual decode regression requires FFmpeg and ffprobe")
    from packages.core.config import get_settings

    monkeypatch.setattr(get_settings(), "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(get_settings(), "MANOR_FS_ENABLED", True)
    entity_root = tmp_path / "entity"

    fixture = Path(__file__).resolve().parents[1] / "apps/web/public/blueprints/creative/podcast-episode.mp3"
    audio = bytearray(fixture.read_bytes())
    middle = len(audio) // 2
    audio[middle:middle + 1024] = b"\x00" * 1024
    directory = "production/run"
    (entity_root / directory).mkdir(parents=True)
    audio_path = entity_root / directory / "episode.mp3"
    audio_path.write_bytes(audio)
    (entity_root / directory / "script.md").write_text("A fictional podcast test script.")
    snapshot = json.dumps({"workspace_id": "workspace", "mode": "podcast", "sources": [],
                           "output_directory": directory}).encode()
    (entity_root / directory / "brief.json").write_bytes(snapshot)

    # The same input still has a valid duration/stream and FFmpeg's default
    # recover-and-continue behavior exits successfully despite decoder errors.
    report = await studio.media._probe_media_report(shutil.which("ffprobe"), str(audio_path))
    assert report["has_audio"] and 20 <= report["duration_seconds"] <= 600
    _, stderr = await studio.media._run_process(
        [shutil.which("ffmpeg"), "-v", "error", "-i", str(audio_path), "-f", "null", "-"],
        timeout_seconds=30,
    )
    assert stderr.strip(), "The corrupt fixture must exercise actual decoder errors"

    async def source(reference, _context):
        return reference, str(entity_root / reference)

    async def scope(*_args):
        return str(entity_root), "production/review"

    class Session:
        async def scalar(self, _query):
            return SimpleNamespace(id="current-clean-document")

    @asynccontextmanager
    async def session():
        yield Session()

    receipts = []

    async def save(*args):
        receipts.append(args)
        return {"document_id": "receipt"}

    monkeypatch.setattr(studio, "_source_path", source)
    monkeypatch.setattr(studio, "_scope", scope)
    monkeypatch.setattr(studio, "_save", save)
    monkeypatch.setattr("packages.core.database.async_session", session)
    result = json.loads(await studio._verify_creative_studio_handler(
        entity_id="entity", workspace_id="workspace", snapshot_path=directory + "/brief.json",
        snapshot_sha256=hashlib.sha256(snapshot).hexdigest(),
        files=[directory + "/episode.mp3", directory + "/script.md"], output_directory="production/review",
    ))
    assert result["status"] == "failed", result
    assert "ffmpeg" in result["error"]
    assert "exit code" in result["error"] or "decode errors" in result["error"]
    assert not result.get("technical_checks_passed")
    assert not receipts
