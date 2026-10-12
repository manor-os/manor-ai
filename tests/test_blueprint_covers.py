"""Marketplace concept covers stay separate from real execution evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from packages.core.blueprints.catalogue import get_platform_blueprints


PUBLIC = Path(__file__).resolve().parents[1] / "apps/web/public"
EVIDENCE = {
    "recording-shorts-v1": [
        "/blueprints/recording/coffee-shorts-preview.png",
        "/blueprints/recording/coffee-shorts-review.jpg",
        "/blueprints/recording/coffee-shorts-intake.mp4",
        "/blueprints/recording/coffee-shorts-pickup.mp4",
    ],
    "recording-tutorial-v1": [
        "/blueprints/recording/coffee-tutorial-playback.jpg",
        "/blueprints/recording/coffee-tutorial-editor.jpg",
        "/blueprints/recording/coffee-tutorial.mp4",
    ],
    "creative-presentation-v1": [
        "/blueprints/creative/presentation-editable.jpg",
        "/blueprints/creative/presentation-slide-03.jpg",
    ],
    "creative-podcast-v1": ["/blueprints/creative/podcast-playback.jpg"],
    "creative-carousel-v1": [f"/blueprints/creative/carousel-page-{index:02}.jpg" for index in range(1, 4)],
    "creative-dubbing-v1": [
        "/blueprints/creative/dubbing-english-playback.jpg",
        "/blueprints/creative/cedar-cup-english.mp4",
    ],
    "creative-handbook-v1": [
        "/blueprints/creative/handbook-pdf-cover.jpg",
        "/blueprints/creative/handbook-page-04.jpg",
    ],
}


def test_all_published_blueprints_have_distinct_concept_covers():
    payloads = get_platform_blueprints()
    assert {payload["manifest"]["slug"] for payload in payloads} == set(EVIDENCE)
    assert len(payloads) == len(EVIDENCE)
    urls = [payload["manifest"]["cover_image_url"] for payload in payloads]
    assert len(set(urls)) == len(EVIDENCE)
    digests = [hashlib.sha256((PUBLIC / url.lstrip("/")).read_bytes()).hexdigest() for url in urls]
    assert len(set(digests)) == len(EVIDENCE)


@pytest.mark.parametrize("slug", EVIDENCE)
def test_concept_cover_is_first_real_image_and_retains_the_evidence_gallery(slug):
    payload = next(item for item in get_platform_blueprints() if item["manifest"]["slug"] == slug)
    manifest = payload["manifest"]
    assets = manifest["showcase_assets"]
    expected_url = f"/blueprints/covers/{slug}.webp"
    assert manifest["cover_image_url"] == expected_url
    assert 1 <= len(assets) <= 6
    cover = assets[0]
    assert cover["id"] == "concept-cover"
    assert cover["kind"] == "image" and cover["url"] == expected_url
    assert cover["mime_type"] == "image/webp"
    if slug == "jr-ai-music-first-release-automatic-goals-v1":
        assert cover["alt_text"] == f"Illustrated cover for {manifest['title']}"
        assert cover["caption"] == f"AI-generated concept artwork for {manifest['title']}."
    else:
        assert cover["alt_text"] == f"{manifest['title']} — AI-illustrated concept cover"
        assert cover["caption"] == (
            f"AI-generated concept cover for {manifest['title']}. "
            "Illustrative artwork, not a screenshot or a verified Workspace output. "
            "Actual browser-tested examples, where available, remain in the following gallery items."
        )
    path = PUBLIC / expected_url.lstrip("/")
    assert path.is_file() and path.stat().st_size > 10000
    with Image.open(path) as image:
        assert image.format == "WEBP"
        assert image.width > image.height and image.width >= 1024 and image.height >= 512
        image.verify()

    assert [asset["url"] for asset in assets[1:]] == EVIDENCE[slug]
    assert len({asset["id"] for asset in assets}) == len(assets)
    for asset in assets[1:]:
        evidence_path = PUBLIC / asset["url"].lstrip("/")
        assert evidence_path.is_file() and evidence_path.stat().st_size > 10000
        assert asset["id"] == evidence_path.stem
        assert asset["kind"] == ("video" if evidence_path.suffix == ".mp4" else "image")
        assert "concept cover" not in asset["caption"].lower()
    # Artwork is listing metadata, never a Workspace starter/runtime artifact.
    runtime_payload = {key: value for key, value in payload.items() if key != "manifest"}
    assert "/blueprints/covers/" not in json.dumps(runtime_payload)


def test_dubbing_public_demo_is_english_and_matches_authoring_metadata():
    from scripts.blueprints.creative_studios import STUDIOS, listing_description, showcase_assets

    spec = next(item for item in STUDIOS if item.key == "dubbing")
    payload = next(item for item in get_platform_blueprints() if item["manifest"]["slug"] == spec.slug)
    manifest = payload["manifest"]
    simulation = payload["recipe"]["simulation_experience"]
    example = (
        "Cedar Cup, a fictional cafe, turns a Mandarin-narrated training clip into an English voiceover "
        "and English subtitles."
    )
    assert spec.example == simulation["sample_prompt"] == simulation["stages"][0]["body"] == example
    assert example in manifest["description"]
    assert manifest["description"] == listing_description(spec)
    assert manifest["showcase_assets"] == showcase_assets(spec)
    evidence = manifest["showcase_assets"][1:]
    assert [(asset["url"], asset["mime_type"]) for asset in evidence] == [
        ("/blueprints/creative/dubbing-english-playback.jpg", "image/jpeg"),
        ("/blueprints/creative/cedar-cup-english.mp4", "video/mp4"),
    ]
    for asset in evidence:
        assert "English" in asset["alt_text"] and "Mandarin" not in asset["alt_text"]
        assert "existing AI Mandarin narration and matching SRT" in asset["caption"]
        assert "English AI voiceover and English subtitles with proportional timing" in asset["caption"]
        assert "do not certify listening quality or translation fidelity" in asset["caption"]
    public_demo = json.dumps({"manifest": manifest, "simulation": simulation})
    assert "cedar-cup-mandarin" not in public_demo
    assert "dubbing-playback.jpg" not in public_demo
    assert "makes a Mandarin version" not in public_demo
    # Retired non-English demos must not remain reachable as public static files.
    assert not (PUBLIC / "blueprints/creative/cedar-cup-mandarin.mp4").exists()
    assert not (PUBLIC / "blueprints/creative/dubbing-playback.jpg").exists()




def test_public_showcase_provenance_keeps_verification_limits_self_contained():
    provenance = " ".join((PUBLIC / "blueprints/creative/README.md").read_text().split())
    assert "docs/creative-studios-browser-verification.md" not in provenance
    assert "All public demos use English copy, visible text, subtitles and AI narration" in provenance
    assert "This showcase policy does not restrict Workspace languages" in provenance
    assert "all five 1.0.6 candidates were freshly installed Live through the browser" in provenance
    assert "not an all-green acceptance claim or a production deployment" in provenance
    assert "historical tests were not rewritten as English" in provenance
    assert "The replacement English dubbing run is `01M2FD6JF0CFY5PCDN70PX0YR7`" in provenance
    assert "video Document `01M2FDGT8M9WY6F3MQB1WKTW4F`" in provenance
    assert "fresh uninterrupted playback from the beginning to 56.033008 seconds" in provenance
    assert "`ended=true` and no player error" in provenance
    for filename in ("cedar-cup-english.mp4", "dubbing-english-playback.jpg", "podcast-episode.mp3"):
        digest = hashlib.sha256((PUBLIC / "blueprints/creative" / filename).read_bytes()).hexdigest()
        assert f"{filename}: `{digest}`" in provenance
    assert "existing AI Mandarin narration and its matching Chinese SRT" in provenance
    assert "cropped from 1280×720 to 1280×600" in provenance
    assert "200-pixel dark caption band" in provenance
    assert "not a full continuous UI recording or customer footage" in provenance
    assert "generated a new English AI voiceover and English SRT" in provenance
    assert "The run remains at the operator review gate" in provenance
    assert "Playback and decoding do not certify speech intelligibility, pronunciation or translation fidelity" in provenance
    assert "no human listening approval was given" in provenance
    assert "no-reference STT was unavailable" in provenance
    assert "the no-SRT transcription route was not browser-verified" in provenance
    assert "Subtitle timing is proportional, not word-aligned" in provenance
