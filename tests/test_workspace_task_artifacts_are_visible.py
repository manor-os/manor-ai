"""A produced workspace artifact must be registerable in Knowledge.

Task 01KZ8A43NZSZCYNTD46A2G8C24 rendered a 16.5 MB MP4 to

    Workspaces/_by_id/<workspace>/tasks/<task>/videos/<file>.mp4

The file was on disk. No Document row was ever created for it, so the chat's
file card had nothing to resolve and dropped the user on an empty Knowledge
page — the produced video was, from the product's point of view, lost.

``HIDDEN_PREFIXES`` lists ``tasks/`` to hide the entity-ROOT task storage.
The visibility check applied those names to every path SEGMENT at any depth,
so the ``tasks`` segment inside the canonical workspace artifact path matched
the root-level rule and knowledge sync refused to register the file.
``STORAGE_ONLY_PREFIXES`` says the opposite about these paths: they sync, and
only their physical folders stay out of the tree.
"""
from __future__ import annotations

import pytest

from packages.core.services.knowledge_visibility import (
    ANYWHERE_HIDDEN_DIR_NAMES,
    is_user_visible_path,
    is_user_visible_folder_path,
)

WS = "01KZ8A2FH1FDDVKV807DTQ4CMT"
TASK = "01KZ8A43NZSZCYNTD46A2G8C24"
ARTIFACT = f"Workspaces/_by_id/{WS}/tasks/{TASK}/videos/2026-08-05-the-10-minute-reset.mp4"


def test_the_produced_video_can_be_registered():
    assert is_user_visible_path(ARTIFACT) is True


@pytest.mark.parametrize("kind", ["videos", "images", "audio", "documents"])
def test_every_workspace_task_artifact_kind_can_be_registered(kind):
    assert is_user_visible_path(
        f"Workspaces/_by_id/{WS}/tasks/{TASK}/{kind}/deliverable.bin"
    ) is True


def test_the_physical_storage_folder_still_stays_out_of_the_tree():
    """Visible as a Document, not as a browsable folder — that is what
    STORAGE_ONLY_PREFIXES means, and the fix must not change it."""
    assert is_user_visible_folder_path(f"Workspaces/_by_id/{WS}/tasks/{TASK}/videos") is False


@pytest.mark.parametrize(
    "path",
    [
        "uploads/media-references/01J/frame.png",
        "avatars/me.png",
        "tmp/partial.mp4",
    ],
)
def test_runtime_input_storage_is_still_hidden(path):
    """Provider reference frames and avatars are inputs and chrome, not
    deliverables."""
    assert is_user_visible_path(path) is False


def test_tasks_is_a_root_prefix_not_a_segment_anywhere():
    """``tasks/`` must keep blocking at the entity root — knowledge URL
    signing uses the same rule to bound what browser tools may be handed —
    while never matching the ``tasks`` segment inside a workspace artifact
    path. Position, not name, is what the rule is about."""
    assert is_user_visible_path("tasks/run/result.json") is False
    assert is_user_visible_path(f"Workspaces/_by_id/{WS}/tasks/{TASK}/report.pdf") is True


@pytest.mark.parametrize(
    "path",
    [
        f"Workspaces/_by_id/{WS}/.cache/blob.bin",
        f"Workspaces/_by_id/{WS}/tasks/{TASK}/sandbox_output/scratch.txt",
        f"Workspaces/_by_id/{WS}/tasks/{TASK}/__pycache__/x.pyc",
        f"Workspaces/_by_id/{WS}/svg_output/slide_1.svg",
    ],
)
def test_scratch_directories_stay_hidden_at_any_depth(path):
    """These names mean "internal" wherever they appear — unlike ``tasks``,
    which names a position."""
    assert is_user_visible_path(path) is False


def test_position_dependent_names_are_not_in_the_anywhere_list():
    """The regression guard: putting ``tasks`` (or ``uploads``) back into the
    any-depth set re-hides every workspace artifact."""
    for name in ("uploads", "avatars"):
        assert name not in ANYWHERE_HIDDEN_DIR_NAMES
