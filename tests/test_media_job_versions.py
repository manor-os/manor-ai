"""A finished video clip can be redone as a new version, and viewers see a job's phases.

Regenerating replays the earlier clip's settings through the same runtime
path agents use (credits and routing are decided there, so it is faked at that
boundary), chains the versions under one lineage root, and is limited to the
clip's maker or the writers of its Workspace.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import packages.core.database as db_module
from packages.core.models.base import generate_ulid
from packages.core.models.media_job import MediaJob
from packages.core.models.workspace import Workspace, WorkspaceStaff
from packages.core.tasks import media_tasks
from tests.test_document_permissions import _auth, _create_entity_user


async def _me(client: AsyncClient, headers: dict) -> dict:
    response = await client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _clip(entity_id: str, user_id: str, *, status: str = "completed", **params) -> str:
    async with db_module.async_session() as db:
        job = MediaJob(
            id=generate_ulid(), entity_id=entity_id, user_id=user_id, kind="video", status=status,
            prompt="A cat on a roof, cinematic light", model="bytedance/seedance-2.0",
            params={
                "duration": 5, "resolution": "1080p", "aspect_ratio": "9:16", "seed": 42,
                "first_frame_url": "/api/v1/fs/raw/cat.png", "source_prompt": "A cat on a roof",
                "billing_mode": "platform", **params,
            },
            result_url="/api/v1/fs/raw/cat.mp4" if status == "completed" else None,
        )
        db.add(job)
        await db.commit()
        return job.id


@pytest.fixture
def started_jobs(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stand in for the runtime video boundary and record what it was asked."""
    calls: list[dict] = []

    async def fake_runtime(**kwargs):
        calls.append(kwargs)
        async with db_module.async_session() as db:
            job = MediaJob(
                id=generate_ulid(), entity_id=kwargs["entity_id"], user_id=kwargs["user_id"],
                kind="video", status="pending", prompt=kwargs["prompt"],
                model=kwargs["params"].get("model"),
                params={**kwargs["params"], **(kwargs.get("lineage") or {})},
            )
            db.add(job)
            await db.commit()
        return json.dumps({"status": "pending", "job_id": job.id})

    monkeypatch.setattr("packages.core.ai.runtime.runtime_generate_video_media", fake_runtime)
    return calls


async def _finish(job_id: str) -> None:
    async with db_module.async_session() as db:
        job = (await db.execute(select(MediaJob).where(MediaJob.id == job_id))).scalar_one()
        job.status = "completed"
        await db.commit()


@pytest.mark.asyncio
async def test_regenerating_replays_the_clip_and_chains_its_versions(
    client: AsyncClient, started_jobs: list[dict],
):
    headers = await _auth(client, "media_versions_owner")
    me = await _me(client, headers)
    first = await _clip(me["entity_id"], me["id"], run_id="run_1", unit_id="shot_02")

    another_take = await client.post(f"/api/v1/media/jobs/{first}/regenerate", headers=headers, json={})
    assert another_take.status_code == 201, another_take.text
    replayed = started_jobs[0]
    assert replayed["prompt"] == "A cat on a roof"
    assert replayed["params"] == {
        "duration": 5, "resolution": "1080p", "aspect_ratio": "9:16", "seed": 42,
        "first_frame_url": "/api/v1/fs/raw/cat.png", "model": "bytedance/seedance-2.0",
    }
    assert replayed["lineage"] == {"regenerated_from": first, "lineage_root": first}
    assert replayed["workflow_context"] == {
        "_workflow_run_id_from_context": "run_1", "_workflow_scene_id_from_context": "shot_02",
    }
    second = another_take.json()
    assert second["regenerated_from"] == first and second["lineage_root"] == first

    await _finish(second["id"])
    edited = await client.post(
        f"/api/v1/media/jobs/{second['id']}/regenerate", headers=headers,
        json={"prompt": "A cat on a roof at dusk", "duration": 10, "keep_seed": False},
    )
    assert edited.status_code == 201, edited.text
    assert started_jobs[1]["prompt"] == "A cat on a roof at dusk"
    assert started_jobs[1]["params"]["duration"] == 10
    assert "seed" not in started_jobs[1]["params"]
    # Every later version keeps the first clip as its root.
    assert started_jobs[1]["lineage"] == {"regenerated_from": second["id"], "lineage_root": first}

    versions = await client.get(f"/api/v1/media/jobs?lineage_root={first}", headers=headers)
    assert versions.status_code == 200, versions.text
    assert [job["id"] for job in versions.json()] == [first, second["id"], edited.json()["id"]]


@pytest.mark.asyncio
async def test_only_the_maker_can_regenerate_a_finished_personal_clip(
    client: AsyncClient, started_jobs: list[dict],
):
    headers = await _auth(client, "media_versions_maker")
    me = await _me(client, headers)
    finished = await _clip(me["entity_id"], me["id"])
    running = await _clip(me["entity_id"], me["id"], status="processing")
    colleague = await _create_entity_user(me["entity_id"], "media_versions_colleague")
    outsider_headers = await _auth(client, "media_versions_outsider")

    assert (await client.post(
        f"/api/v1/media/jobs/{finished}/regenerate", headers=colleague["headers"], json={},
    )).status_code == 403
    assert (await client.post(
        f"/api/v1/media/jobs/{finished}/regenerate", headers=outsider_headers, json={},
    )).status_code == 404
    assert (await client.post(
        f"/api/v1/media/jobs/{running}/regenerate", headers=headers, json={},
    )).status_code == 409
    assert started_jobs == []


@pytest.mark.asyncio
async def test_generating_now_into_a_workspace_needs_write_access(
    client: AsyncClient, started_jobs: list[dict],
):
    headers = await _auth(client, "media_versions_ws_owner")
    me = await _me(client, headers)
    viewer = await _create_entity_user(me["entity_id"], "media_versions_ws_viewer")
    async with db_module.async_session() as db:
        workspace = Workspace(entity_id=me["entity_id"], name="Studio", settings={"access_mode": "members_only"})
        db.add(workspace)
        await db.flush()
        db.add_all([
            WorkspaceStaff(workspace_id=workspace.id, staff_id=None, user_id=me["id"], role="owner", status="active"),
            WorkspaceStaff(workspace_id=workspace.id, staff_id=None, user_id=viewer["id"], role="viewer", status="active"),
        ])
        await db.commit()
        workspace_id = workspace.id
    request = {"prompt": "Rain on a street corner", "workspace_id": workspace_id, "duration": 5, "aspect_ratio": "9:16"}

    assert (await client.post("/api/v1/media/jobs", headers=viewer["headers"], json=request)).status_code == 403
    assert started_jobs == []

    created = await client.post("/api/v1/media/jobs", headers=headers, json=request)
    assert created.status_code == 201, created.text
    assert started_jobs[0]["workspace_id"] == workspace_id
    assert started_jobs[0]["params"] == {"duration": 5, "aspect_ratio": "9:16"}
    assert started_jobs[0]["lineage"] is None


@pytest.mark.asyncio
async def test_a_video_job_reports_each_phase_and_keeps_its_other_params(db_session, monkeypatch):
    sent: list[tuple[str, dict, str | None]] = []

    async def capture(entity_id, job, *, workspace_id=None):
        sent.append((entity_id, job, workspace_id))

    async def nothing(*_args):
        pass

    monkeypatch.setattr("packages.core.services.realtime.broadcast_media_job_update", capture)
    monkeypatch.setattr(media_tasks, "_push_notification", nothing)
    monkeypatch.setattr(media_tasks, "_project_group_media_if_ready", nothing)
    job = MediaJob(
        id=generate_ulid(), entity_id=generate_ulid(), kind="video", status="pending",
        prompt="A clip", model="bytedance/seedance-2.0", params={"workspace_id": "ws_1"}, byok=True,
    )
    db_session.add(job)
    await db_session.commit()

    assert await media_tasks._mark_video_job_processing(job.id) is True
    await media_tasks._remember_provider_poll(job.id, "kling", "https://provider.test/poll/1")
    await media_tasks._record_video_progress(job.id, "saving")
    await db_session.refresh(job)
    assert job.params["progress"]["phase"] == "saving"
    assert job.params["provider_poll"]["poll_url"] == "https://provider.test/poll/1"
    assert job.params["workspace_id"] == "ws_1" and job.params["worker_lease_at"]
    assert [(event[1]["progress"]["phase"], event[2]) for event in sent] == [
        ("submitting", "ws_1"), ("generating", "ws_1"), ("saving", "ws_1"),
    ]

    assert await media_tasks._finalize_video_job(job.id, {"result_url": "/clip.mp4"})
    await db_session.refresh(job)
    assert job.params["progress"]["phase"] == "done"


@pytest.mark.asyncio
async def test_a_job_lineage_comes_only_from_server_code(monkeypatch):
    from packages.core.ai.runtime import runtime_generate_video_media
    from packages.core.ai.tools import extended_tools

    received: list[dict] = []

    async def capture(**kwargs):
        received.append(kwargs)
        return "{}"

    monkeypatch.setattr(extended_tools, "_generate_video_handler", capture)
    await runtime_generate_video_media(
        entity_id="ent", user_id="usr", prompt="p", name="",
        params={"_media_lineage_from_context": {"lineage_root": "forged"}, "duration": 5},
    )
    assert received[0]["_media_lineage_from_context"] == {}
    assert received[0]["duration"] == 5

    assert extended_tools._video_job_origin({
        "_workflow_run_id_from_context": "run_1",
        "_workflow_scene_id_from_context": "shot_02",
        "_media_lineage_from_context": {"regenerated_from": "job_2", "lineage_root": "job_1"},
    }) == {"run_id": "run_1", "unit_id": "shot_02", "regenerated_from": "job_2", "lineage_root": "job_1"}
