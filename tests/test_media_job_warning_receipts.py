"""A completed provider job retains its generation limitations for callers."""

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import media_tools


def _job(*, status="completed", params=None):
    return SimpleNamespace(
        id="job_video",
        kind="video",
        status=status,
        model="test/video",
        params=params or {},
        result_url="/api/v1/fs/entity123/Videos/picture.mp4",
        file_size=1234,
        duration_seconds=15,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "processing", "completed", "failed"])
async def test_media_job_payload_preserves_only_public_generation_limits(status):
    params = {
        "route_warnings": ["Native narration was omitted on this provider route."],
        "audio_policy": "silent_picture_only",
        "omitted_seedance_inputs": ["generate_audio"],
        "post_production_intent": "narration",
        "source_prompt": "private prompt",
        "reference_urls": ["https://example.invalid/private?signature=secret"],
        "credential_source": "byok",
    }

    payload, = await media_tools._jobs_to_payload("entity123", [_job(status=status, params=params)])

    assert payload["status"] == status
    assert payload["warnings"] == params["route_warnings"]
    for key in ("audio_policy", "omitted_seedance_inputs", "post_production_intent"):
        assert payload[key] == params[key]
    assert not {"params", "route_warnings", "source_prompt", "reference_urls", "credential_source"} & payload.keys()
    assert payload["fs_path"] == "Videos/picture.mp4"
    assert payload["duration_seconds"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"route_warnings": None, "audio_policy": None}])
async def test_legacy_media_job_payload_does_not_invent_generation_limits(params):
    payload, = await media_tools._jobs_to_payload("entity123", [_job(params=params)])

    assert not {"warnings", "audio_policy", "omitted_seedance_inputs", "post_production_intent"} & payload.keys()
    assert payload["status"] == "completed"


@pytest.mark.asyncio
async def test_wait_media_jobs_keeps_picture_limits_in_completed_receipt(monkeypatch):
    job = _job(params={
        "route_warnings": ["Native narration was omitted on this provider route."],
        "audio_policy": "silent_picture_only",
        "omitted_seedance_inputs": ["generate_audio"],
        "post_production_intent": "narration",
    })

    async def load_jobs(entity_id, ids):
        assert entity_id == "entity123"
        assert ids == [job.id]
        return [job], []

    monkeypatch.setattr(media_tools, "_load_media_jobs", load_jobs)

    payload = json.loads(await media_tools._wait_media_jobs_handler(
        entity_id="entity123", job_ids=[job.id],
    ))

    assert payload["status"] == "completed"
    assert payload["completed_count"] == 1
    assert payload["pending_job_ids"] == []
    assert payload["failed_job_ids"] == []
    assert payload["jobs"][0]["warnings"] == job.params["route_warnings"]
    assert payload["jobs"][0]["audio_policy"] == "silent_picture_only"
    assert payload["jobs"][0]["omitted_seedance_inputs"] == ["generate_audio"]
    assert payload["jobs"][0]["post_production_intent"] == "narration"
