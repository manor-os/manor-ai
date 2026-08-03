"""Generated output lands in one place per task, and says where.

Before this, a workspace's generated media went into type buckets shared by
every run it had ever made — ``Workspaces/_by_id/<id>/{images,videos,audio}``
— so "which task produced this file" was unanswerable from disk. One staging
workspace ended up with five parallel layouts (``workspace_artifacts/``,
``videos/``, ``workspace/stickman_explainer/``, ``faceless_stickman_daily/``,
``daily_stickman_queue/``) plus 333MB of loose input snapshots.

Two properties are pinned here:

* every file a task produces is archived under ``tasks/<task_id>/`` — a retry
  of the SAME task resolves to the SAME directory, so the previous attempt's
  intermediate artifacts are found in place instead of regenerated;
* the resolved path is reported back to the model, so the next step can read
  what this one wrote.
"""
from __future__ import annotations

import pytest

from packages.core.services.generated_media_naming import (
    TASK_ARTIFACT_SEGMENT,
    workspace_artifact_default_dir,
    workspace_task_artifact_dir,
)

BASE = "Workspaces/_by_id/01FOLDERID"
TASK = "01KYH0V9WSY5WGGJQEPRJDRK32"


def test_output_is_archived_under_its_task():
    assert workspace_task_artifact_dir(BASE, TASK) == f"{BASE}/{TASK_ARTIFACT_SEGMENT}/{TASK}"


def test_type_buckets_live_inside_the_task_archive():
    task_dir = workspace_task_artifact_dir(BASE, TASK)
    for kind in ("images", "videos", "audio"):
        resolved = workspace_artifact_default_dir(task_dir, kind)
        assert resolved == f"{BASE}/{TASK_ARTIFACT_SEGMENT}/{TASK}/{kind}"


def test_a_retry_of_the_same_task_resolves_to_the_same_directory():
    """The reuse property: intermediate artifacts survive a retry in place."""
    first = workspace_task_artifact_dir(BASE, TASK)
    second = workspace_task_artifact_dir(BASE, TASK)
    assert first == second


def test_different_tasks_do_not_share_a_directory():
    other = "01KYGH41T0D0ACSNN9XKVSPP1B"
    assert workspace_task_artifact_dir(BASE, TASK) != workspace_task_artifact_dir(BASE, other)


@pytest.mark.parametrize("bad", [None, "", "   ", "../../etc/passwd", "a/b", "task-1", "..", "."])
def test_a_non_identifier_task_id_falls_back_to_the_workspace_root(bad):
    """Never invent a directory tree out of something that is not a task id."""
    resolved = workspace_task_artifact_dir(BASE, bad)
    assert resolved == BASE
    assert ".." not in resolved


def test_no_workspace_means_no_archive_directory():
    assert workspace_task_artifact_dir("", TASK) == ""
    assert workspace_task_artifact_dir(None, TASK) == ""


# ── The model must be told where the file landed ──────────────────────


def test_image_result_reports_the_saved_path():
    """The system chooses the path now; withholding it leaves the next step
    unable to find what this one produced. The old gate depended on the
    caller passing workspace_id in kwargs, which the runtime-context path
    does not."""
    from packages.core.ai.tools.extended_tools import _image_result_payload

    payload = _image_result_payload(
        image_url="/api/v1/fs/ent_1/Workspaces/_by_id/f1/tasks/T1/images/hero.png",
        prompt="a hero shot",
        size="1024x1024",
        model="gpt-image-1",
        entity_id="ent_1",
        include_fs_path=False,
        saved_to_knowledge=True,
    )

    assert payload["fs_path"] == "Workspaces/_by_id/f1/tasks/T1/images/hero.png"


def test_remote_url_result_has_no_entity_path_to_report():
    from packages.core.ai.tools.extended_tools import _image_result_payload

    payload = _image_result_payload(
        image_url="https://cdn.example.com/generated/hero.png",
        prompt="a hero shot",
        size="1024x1024",
        model="gpt-image-1",
        entity_id="ent_1",
    )
    assert not payload.get("fs_path")


# ── Write and read must agree on where files live ─────────────────────


def test_file_locator_accepts_the_task_scope():
    """The read path has to mirror the write path or a later step cannot
    open what an earlier one saved."""
    import inspect

    from packages.core.ai.tools.file_tools import (
        _locate_entity_file,
        _workspace_scoped_existing_path,
        _workspace_scoped_new_file_path,
    )

    for fn in (
        _locate_entity_file,
        _workspace_scoped_existing_path,
        _workspace_scoped_new_file_path,
    ):
        assert "task_id" in inspect.signature(fn).parameters, (
            f"{fn.__name__} must know the task scope"
        )


def test_base_dir_resolver_takes_the_task_scope():
    import inspect

    from packages.core.services.generated_media_naming import (
        resolve_workspace_artifact_base_dir,
    )

    assert "task_id" in inspect.signature(resolve_workspace_artifact_base_dir).parameters
