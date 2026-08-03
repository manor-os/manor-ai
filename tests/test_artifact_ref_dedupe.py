"""One file, one entry in the run output.

Staging task 01KYJZCEX9XJ1533579YKWKDA1 rendered a RUN OUTPUT panel listing
scene-02-monster.png twice, build_video.py twice, and "3 more" — twenty
rows for seven files. The step result held twenty refs: each of six PNGs
three times, from three collectors that each legitimately found it (the
tool payload, the step result, the evidence sweep) and one merge step that
appended without ever asking whether it already had that file.

The collectors are not the bug — finding the same artifact by several routes
is what makes the evidence channel robust. Appending without identity is.
"""
from __future__ import annotations

import pytest

from packages.core.workers.internal import (
    _artifact_ref_identity,
    _dedupe_artifact_refs,
    _merge_artifact_refs,
)

BASE = "Workspaces/_by_id/01KYJZB3FXKE1GT5EE46238JWA/daily_stickman/images/"
SCENES = [
    "scene-01-hook.png",
    "scene-02-monster.png",
    "scene-03-two-minutes.png",
    "scene-04-lower-friction.png",
    "scene-05-momentum.png",
    "scene-06-takeaway.png",
]


def test_the_incident_shape_collapses_to_the_real_file_count():
    refs = []
    for _ in range(3):  # three collectors each found every scene
        refs += [{"type": "artifact", "source": "fs_path", "fs_path": BASE + n} for n in SCENES]
    refs += [{"type": "artifact", "source": "fs_path", "fs_path": "ws/code/build_video.py"}] * 2

    out = _dedupe_artifact_refs(refs)

    assert len(refs) == 20
    assert len(out) == 7
    assert [r["fs_path"].rsplit("/", 1)[-1] for r in out] == SCENES + ["build_video.py"]


def test_identity_is_the_location_not_the_label():
    """The same file arrives with different type/source labels depending on
    which collector found it — those must not make it a different file."""
    a = {"type": "artifact", "source": "fs_path", "fs_path": BASE + "scene-01-hook.png"}
    b = {"type": "image", "source": "file_path", "fs_path": BASE + "scene-01-hook.png"}
    assert _artifact_ref_identity(a) == _artifact_ref_identity(b)
    assert len(_dedupe_artifact_refs([a, b])) == 1


def test_different_files_are_kept():
    refs = [
        {"fs_path": BASE + "scene-01-hook.png"},
        {"fs_path": BASE + "scene-02-monster.png"},
        {"document_id": "doc_1"},
        {"url": "https://cdn.example.com/a.mp4"},
    ]
    assert len(_dedupe_artifact_refs(refs)) == 4


def test_first_occurrence_wins_and_order_is_preserved():
    first = {"type": "video", "fs_path": "ws/final.mp4", "duration": 180}
    later = {"type": "artifact", "fs_path": "ws/final.mp4"}
    out = _dedupe_artifact_refs([{"fs_path": "ws/a.png"}, first, later])
    assert out == [{"fs_path": "ws/a.png"}, first]


def test_merging_into_an_existing_list_does_not_re_add():
    result = {"files": [{"fs_path": "ws/a.png"}]}
    merged = _merge_artifact_refs(result, [{"fs_path": "ws/a.png"}, {"fs_path": "ws/b.png"}])
    assert [r["fs_path"] for r in merged["files"]] == ["ws/a.png", "ws/b.png"]


@pytest.mark.parametrize("junk", [None, "not-a-dict", 42, {}, {"type": "artifact"}])
def test_refs_without_a_location_are_left_alone(junk):
    """No identity means we cannot prove it is a duplicate — keep it rather
    than silently dropping evidence."""
    assert _artifact_ref_identity(junk) is None
    assert _dedupe_artifact_refs([junk]) == [junk]


def test_executor_collection_is_deduped_too():
    from packages.core.plans.executor import _artifact_refs_from_result

    payload = {
        "created": True,
        "files": [
            {"type": "artifact", "source": "fs_path", "fs_path": BASE + "scene-01-hook.png"},
            {"type": "image", "source": "fs_path", "fs_path": BASE + "scene-01-hook.png"},
            {"type": "artifact", "source": "fs_path", "fs_path": BASE + "scene-02-monster.png"},
        ],
    }
    refs = _artifact_refs_from_result(payload)
    paths = {r.get("fs_path") for r in refs}
    assert len(paths) == 2
