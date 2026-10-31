"""Uploaded images, video and audio are described once in the background and found by content."""
from __future__ import annotations

import io
import json
import shutil
import subprocess

import pytest
from httpx import AsyncClient

from packages.core.services import knowledge_media_analysis as kma
from packages.core.services.media_analysis import (
    MediaAnalysisSkipped,
    analyze_media,
    media_kind,
    parse_analysis,
    search_text,
)
from tests.test_document_permissions import _auth
from tests.test_fs_document_visibility import fs_enabled  # noqa: F401

_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_DESCRIPTION = {
    "kind": "receipt",
    "summary_zh": "鲜丰生鲜超市购物小票，合计 $44.21",
    "summary_en": "Grocery receipt from Fresh Harvest Market totalling $44.21",
    "description_zh": "白底热敏小票，列出五花肉、大白菜等商品。",
    "description_en": "Thermal receipt listing pork belly, napa cabbage and more.",
    "visible_text": ["鲜丰生鲜超市", "五花肉 2.3lb $13.78", "合计 $44.21"],
    "keywords": "小票, receipt, 超市, grocery",
}


def _png(width: int = 200, height: int = 120) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (240, 240, 230)).save(buf, format="PNG")
    return buf.getvalue()


def test_model_output_is_parsed_leniently():
    raw = "Here you go:\n```json\n" + json.dumps(_DESCRIPTION, ensure_ascii=False) + "\n```"
    record = parse_analysis(raw)
    assert record["visible_text"] == "鲜丰生鲜超市\n五花肉 2.3lb $13.78\n合计 $44.21"
    assert record["keywords"] == ["小票", "receipt", "超市", "grocery"]
    text = search_text(record)
    assert "合计 $44.21" in text and "grocery" in text
    with pytest.raises(ValueError):
        parse_analysis("I cannot describe this image.")


def test_only_uploaded_media_is_queued():
    assert media_kind("image/heic") == "image" and media_kind(None, "mov") == "video"
    assert media_kind("audio/mp4", None, "memo.m4a") == "audio"
    assert media_kind("image/svg+xml", "svg") is None and media_kind("application/pdf", "pdf") is None
    assert kma.analysis_kind_for(source="upload", mime_type="image/png", file_type="png", name="a.png") == "image"
    # Generated media already carries its prompt; describing it would bill twice.
    assert kma.analysis_kind_for(source="ai_generated", mime_type="image/png", file_type="png", name="a.png") is None
    assert kma.media_analysis_disabled({"media_analysis": {"enabled": False}})
    assert not kma.media_analysis_disabled({"media_analysis": {"enabled": True}})
    assert not kma.media_analysis_disabled(None)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.asyncio
async def test_each_media_kind_costs_one_description_call(tmp_path):
    calls: list[list[dict]] = []
    transcribed: list[float] = []

    async def complete(messages, max_tokens):
        calls.append(messages)
        return json.dumps(_DESCRIPTION, ensure_ascii=False), {"cost_usd": 0.0003}

    async def transcribe(audio, duration):
        transcribed.append(duration)
        return "大家好，今天我们来介绍新品芒果绿茶，每杯五美元。", {"cost_usd": 0.001}

    image = tmp_path / "photo.png"
    image.write_bytes(_png())
    record = await analyze_media(str(image), "image", complete=complete, transcribe=transcribe)
    assert record["width"] == 200 and record["summary_en"].startswith("Grocery receipt")
    assert calls[-1][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    silent = tmp_path / "silence.mp3"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "3", str(silent)], check=True)
    before = len(calls)
    record = await analyze_media(str(silent), "audio", complete=complete, transcribe=transcribe)
    # Silence is recognised locally: no transcription and no model call.
    assert record["speech"] == "silent" and not transcribed and len(calls) == before

    video = tmp_path / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-f", "lavfi",
        "-i", "sine=frequency=440", "-t", "4", "-shortest", "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    record = await analyze_media(str(video), "video", complete=complete, transcribe=transcribe)
    assert record["speech"] == "speech" and "芒果绿茶" in record["transcript"]
    assert record["width"] == 320 and record["duration_seconds"] == pytest.approx(4, abs=0.2)
    # One contact sheet + the transcript in a single vision call.
    assert len(calls) == before + 1
    content = calls[-1][0]["content"]
    assert "芒果绿茶" in content[0]["text"] and content[1]["type"] == "image_url"

    # A truncated upload (no moov atom) is skipped, not stored as an empty description.
    truncated = tmp_path / "broken.mov"
    truncated.write_bytes(video.read_bytes()[:2000])
    with pytest.raises(MediaAnalysisSkipped):
        await analyze_media(str(truncated), "video", complete=complete, transcribe=transcribe)
    assert len(calls) == before + 1


def test_mini_transcribe_is_billed_at_its_own_rate():
    from packages.core.services.voice.whisper import whisper_cost_usd

    assert whisper_cost_usd(60, "openai/gpt-4o-mini-transcribe") == pytest.approx(0.003)
    assert whisper_cost_usd(60, "openai/whisper-1") == pytest.approx(0.006)


async def _upload_image(client: AsyncClient, headers: dict, name: str = "IMG_2041.png") -> dict:
    response = await client.post(
        "/api/v1/documents/upload", headers=headers,
        files={"file": (name, _png(), "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _record(db_session, document_id: str) -> dict:
    from packages.core.models.document import Document

    db_session.expire_all()
    doc = await db_session.get(Document, document_id)
    return dict((doc.metadata_ or {}).get(kma.METADATA_KEY) or {})


@pytest.fixture
def fake_models(monkeypatch):
    """Provider and credit gate replaced; the claim/store/search path stays real."""
    billed = {"completions": 0, "credit_checks": 0, "credit": True}

    def callers(facts, billed_out):
        async def complete(messages, max_tokens):
            billed["completions"] += 1
            billed_out["model"] = "test/vision"
            return json.dumps(_DESCRIPTION, ensure_ascii=False), {"cost_usd": 0.0003}

        async def transcribe(audio, duration):
            raise AssertionError("images are not transcribed")

        return complete, transcribe

    async def credit(entity_id, **kwargs):
        from packages.core.ai.llm_client import CreditExhaustedError

        billed["credit_checks"] += 1
        if not billed["credit"]:
            raise CreditExhaustedError("out of credit")

    monkeypatch.setattr(kma, "_model_callers", callers)
    monkeypatch.setattr("packages.core.ai.runtime.runtime_assert_credit_available", credit)
    monkeypatch.setattr(kma, "schedule_media_analysis", lambda document_id: None)
    return billed


@pytest.mark.asyncio
async def test_uploaded_image_becomes_findable_by_what_it_shows(client: AsyncClient, db_session, fs_enabled, fake_models):
    from packages.core.ai.runtime.document_actions import runtime_search_documents_action

    owner = await _auth(client, "kma_owner")
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    doc = await _upload_image(client, owner)
    assert (await _record(db_session, doc["id"]))["status"] == "pending"

    assert await kma.analyze_document_media(doc["id"]) == "ready"
    record = await _record(db_session, doc["id"])
    assert record["status"] == "ready" and record["model"] == "test/vision" and record["attempts"] == 1
    # A redelivered task finds nothing to claim: one upload is billed once.
    assert await kma.analyze_document_media(doc["id"]) == "not_claimed"
    assert fake_models["completions"] == 1

    result = json.loads(await runtime_search_documents_action(
        entity_id=me["entity_id"], user_id=me["id"], params={"query": "超市小票"}, db=db_session,
    ))
    top = result["content_matches"][0]
    assert top["name"] == "IMG_2041.png" and "44.21" in top["snippet"]


@pytest.mark.asyncio
async def test_waiting_credit_and_replaced_files_never_store_a_stale_description(
    client: AsyncClient, db_session, fs_enabled, fake_models,
):
    import os
    from datetime import datetime, timedelta, timezone

    from packages.core.models.document import Document
    from packages.core.services.entity_fs import get_entity_root
    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    owner = await _auth(client, "kma_credit")
    doc = await _upload_image(client, owner, "menu.png")

    fake_models["credit"] = False
    assert await kma.analyze_document_media(doc["id"]) == "waiting_credit"
    record = await _record(db_session, doc["id"])
    assert record["status"] == "waiting_credit" and record["attempts"] == 0 and fake_models["completions"] == 0
    assert doc["id"] not in await kma.pending_media_analysis_ids(db_session, limit=500)

    # After the retry window the sweep hands it back; with credit it completes.
    row = await db_session.get(Document, doc["id"])
    meta = dict(row.metadata_)
    meta[kma.METADATA_KEY] = {**record, "retry_after": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
    row.metadata_ = meta
    await db_session.commit()
    assert doc["id"] in await kma.pending_media_analysis_ids(db_session, limit=500)

    # The file is replaced while the analysis runs: the newer version's pending
    # mark wins and the old description is discarded.
    fake_models["credit"] = True
    claim = await kma._claim(doc["id"])
    assert claim is not None
    facts, claimed = claim
    root = get_entity_root(facts["entity_id"])
    path = os.path.join(root, facts["fs_path"])
    with open(path, "wb") as handle:
        handle.write(_png(320, 200))
    synced = await sync_file_to_knowledge(
        entity_id=facts["entity_id"], abs_path=path, entity_root=root, source="upload", force=True,
    )
    assert synced.content_changed
    assert not await kma._finish(doc["id"], claimed, {**claimed, "status": "ready", "summary_en": "stale"})
    assert (await _record(db_session, doc["id"]))["status"] == "pending"


@pytest.mark.asyncio
async def test_account_opt_out_skips_the_model(client: AsyncClient, db_session, fs_enabled, fake_models):
    from packages.core.models.user import Entity

    owner = await _auth(client, "kma_optout")
    me = (await client.get("/api/v1/auth/me", headers=owner)).json()
    entity = await db_session.get(Entity, me["entity_id"])
    entity.settings = {**(entity.settings or {}), "media_analysis": {"enabled": False}}
    await db_session.commit()

    doc = await _upload_image(client, owner)
    assert await kma.analyze_document_media(doc["id"]) == "disabled"
    assert (await _record(db_session, doc["id"]))["status"] == "disabled"
    assert fake_models["completions"] == 0 and fake_models["credit_checks"] == 0
