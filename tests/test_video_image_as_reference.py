"""generate_file kind=video must treat a source/title-card image as a REFERENCE
for real video generation — not loop it into a near-static clip.

Previously, any source_image_url / source_image_path / title_card_image_url
routed to render_image_video, which loops one still image with a 3.5% zoom —
visually "a video that is just one image". Instead, the image should be passed
as reference_urls into the real video generator, combined with the prompt.
"""

from __future__ import annotations

import asyncio
import json

import packages.core.ai.tools.generate_file.video as videomod


def _run(monkeypatch, params):
    calls = {}

    async def fake_generate(**kw):
        calls["generate"] = kw
        return json.dumps({"ok": True})

    async def fake_render(**kw):  # the static path — must NOT be used
        calls["render"] = kw
        return json.dumps({"static": True})

    monkeypatch.setattr(videomod, "runtime_generate_video_media", fake_generate)
    # render path may be removed entirely by the fix; tolerate its absence.
    monkeypatch.setattr(videomod, "runtime_render_image_video_media", fake_render, raising=False)

    asyncio.run(
        videomod.handle_video(
            entity_id="e",
            user_id="u",
            conversation_id="c",
            prompt="epic hype intro",
            name="vid",
            params=params,
            kwargs={},
            agent_id=None,
        )
    )
    return calls


def test_source_image_becomes_reference_for_real_generation(monkeypatch):
    # source_image_url is the back-compat alias; it must fold into reference_urls
    # and go to the real generator, never the (now-removed) static path.
    calls = _run(monkeypatch, {"source_image_url": "Workspaces/x/images/title.png"})
    assert "render" not in calls, "must not use the static image-loop path"
    assert "generate" in calls, "must call the real video generator"
    refs = calls["generate"]["params"].get("reference_urls") or []
    assert "Workspaces/x/images/title.png" in refs


def test_plain_prompt_video_still_uses_real_generator(monkeypatch):
    calls = _run(monkeypatch, {})
    assert "generate" in calls
    assert "render" not in calls
