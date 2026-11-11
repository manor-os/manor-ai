import pytest

from packages.core.services.generated_media_naming import (
    build_workspace_artifact_base_dir,
    build_generated_media_filename,
    build_generated_media_target,
    collision_safe_artifact_path,
    narrator_audio_output_name,
    scope_workspace_artifact_path,
    workspace_artifact_default_dir,
)


@pytest.mark.parametrize("requested, expected", [
    ("episode.mp3", "audio/puck/episode.mp3"),
    ("", "audio/puck/"),
    ("runs/topic/audio/segment.wav", "runs/topic/audio/puck/segment.wav"),
    ("runs/topic/audio/puck/segment.wav", "runs/topic/audio/puck/segment.wav"),
    ("production/runs/one/episode.mp3", "production/runs/one/puck/episode.mp3"),
    ("Workspaces/_by_id/folder/production/runs/one/episode.mp3",
     "Workspaces/_by_id/folder/production/runs/one/puck/episode.mp3"),
])
def test_narration_preserves_explicit_parent(requested, expected):
    from packages.core.ai.tools.extended_tools import _task_narrator_audio_output_name
    from packages.core.ai.tools.media_tools import _narrator_profile_audio_output_name

    assert narrator_audio_output_name(requested, "Puck") == expected
    assert _task_narrator_audio_output_name(requested, "Puck") == expected
    if requested:
        assert _narrator_profile_audio_output_name(requested, "Puck") == expected
        assert narrator_audio_output_name(expected, "Puck") == expected


@pytest.mark.parametrize("archive,requested,expected", [
    ("workflow/run", "production/sources", "sources"),
    ("workflow/run", "production/runs", "artifacts"),
    ("tasks/task", "production/deliveries", "artifacts/deliveries"),
    ("tasks/task", "brand", "brand"),
    ("workflow/run", "Product Videos/episode/technical", "Product Videos/episode/technical"),
])
def test_legacy_production_defaults_follow_the_execution_archive(archive, requested, expected):
    from packages.core.services.generated_media_naming import execution_output_directory
    assert execution_output_directory(requested, f"Workspaces/_by_id/folder/{archive}") == expected


@pytest.mark.parametrize("kind, extension, default_dir", [
    ("presentation", "pptx", "presentations"),
    ("spreadsheet", "xlsx", "spreadsheets"),
])
@pytest.mark.parametrize("nested", [False, True])
def test_native_office_receipt_matches_document_materializer(kind, extension, default_dir, nested):
    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileAuthorizationResourceFactory as Factory,
        WorkspaceFileResourceMatchKind,
    )

    operations = {"operations": [{"op": "slide.insert"}]}
    args = {"kind": kind, **({"params": operations} if nested else operations)}
    bare = Factory.generate_file_resources(args, f"deck.{extension}")
    # The operations route writes under the kind's own bucket, exactly as the
    # handler scopes it (both read generate_file_destination).
    assert bare[0].resource_id == f"{default_dir}/deck.{extension}"
    assert bare[0].match_kind is WorkspaceFileResourceMatchKind.EXACT
    assert Factory.generate_file_destination(args, f"deck.{extension}").directory == default_dir
    # An unnamed operations call gets the handler's exact default leaf, never a tree grant.
    assert Factory.generate_file_resources(args, None)[0].resource_id == (
        f"{default_dir}/generated-document.{extension}"
    )
    assert Factory.generate_file_resources(args, "deck")[0].resource_id == f"{default_dir}/deck.{extension}"
    full_path = f"Workspaces/_by_id/folder/production/runs/one/deck.{extension}"
    assert Factory.generate_file_resources(args, full_path)[0].resource_id == full_path
    assert Factory.generate_file_resources({"kind": kind}, f"deck.{extension}")[0].resource_id == f"{default_dir}/deck.{extension}"


def test_collision_safe_artifact_path_is_stable_until_committed(tmp_path):
    target = tmp_path / "artifacts" / "report.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    first = collision_safe_artifact_path(str(tmp_path), "artifacts/report.pdf")
    retry = collision_safe_artifact_path(str(tmp_path), "artifacts/report.pdf")

    assert first == "artifacts/report_1.pdf"
    assert retry == first

    (tmp_path / first).write_bytes(b"committed")
    assert collision_safe_artifact_path(
        str(tmp_path),
        "artifacts/report.pdf",
    ) == "artifacts/report_2.pdf"


def test_explicit_media_name_wins_without_opaque_prefix(tmp_path):
    filename = build_generated_media_filename(
        prompt="ancient mountain storm scene",
        desired_name="Mountain Storm.mp4",
        ext=".mp4",
        unique_dir=str(tmp_path),
    )

    assert filename == "mountain-storm.mp4"


def test_prompt_media_name_is_readable_and_deduped(tmp_path):
    prompt = "Ancient mountain storm scene with lightning and pine forest"
    first = build_generated_media_filename(
        prompt=prompt,
        ext=".png",
        fallback="generated-image",
        unique_dir=str(tmp_path),
    )
    (tmp_path / first).write_text("exists")

    second = build_generated_media_filename(
        prompt=prompt,
        ext=".png",
        fallback="generated-image",
        unique_dir=str(tmp_path),
    )

    assert first.startswith("ancient-mountain-storm-scene")
    assert not first.startswith("gen_")
    assert second.endswith("-2.png")


def test_prompt_media_name_is_byte_limited_for_unicode_tmp_files(tmp_path):
    prompt = (
        "用第七首帧作为视频首帧，延续上一段画面，保持刘邦的面部特征、服装、"
        "发型、身后大汉甲、火把、帐篷和夜晚森林背景完全一致，中景，画面立刻进入闪回，火光"
    )

    filename = build_generated_media_filename(
        prompt=prompt,
        ext=".mp4",
        fallback="generated-video",
        unique_dir=str(tmp_path),
    )

    assert filename.endswith(".mp4")
    assert len(filename.encode("utf-8")) <= 180
    assert len(filename[:-4].rsplit("-", 1)[-1]) == 8


def test_explicit_media_name_is_byte_limited_for_unicode(tmp_path):
    filename = build_generated_media_filename(
        prompt="fallback prompt",
        desired_name=f"{'刘邦夜晚森林闪回火光帐篷' * 12}.mp4",
        ext=".mp4",
        unique_dir=str(tmp_path),
    )

    assert filename.endswith(".mp4")
    assert len(filename.encode("utf-8")) <= 180


def test_media_target_preserves_requested_folder_path(tmp_path):
    target = build_generated_media_target(
        prompt="tired office cat eats ramen",
        desired_name="猫咪打工人动漫/videos/EP03_加班拉面.mp4",
        ext=".mp4",
        fallback="generated-video",
        default_dir="videos",
        entity_root=str(tmp_path),
    )

    assert target.rel_dir == "猫咪打工人动漫/videos"
    assert target.filename == "ep03-加班拉面.mp4"
    assert target.rel_path == "猫咪打工人动漫/videos/ep03-加班拉面.mp4"
    assert target.abs_path == str(tmp_path / "猫咪打工人动漫" / "videos" / "ep03-加班拉面.mp4")


def test_media_target_defaults_to_storage_dir_for_plain_filename(tmp_path):
    target = build_generated_media_target(
        prompt="ramen close up",
        desired_name="素材-拉面特写.jpg",
        ext=".jpg",
        fallback="generated-image",
        default_dir="images",
        entity_root=str(tmp_path),
    )

    assert target.rel_dir == "images"
    assert target.rel_path == "images/素材-拉面特写.jpg"


def test_workspace_artifact_base_dir_uses_stable_folder_id():
    base = build_workspace_artifact_base_dir(
        workspace_name="桌面耳机支架工业设计项目",
        workspace_id="01KQ9CDDBTNWEZJQC7KW18NYGR",
        artifact_folder_id="01KQ9FOLDER8WJQC7KW18NYGR",
    )

    assert base == "Workspaces/_by_id/01KQ9FOLDER8WJQC7KW18NYGR"


def test_workspace_artifact_default_dir_does_not_create_a_shared_type_bucket():
    assert (
        workspace_artifact_default_dir("Workspaces/桌面耳机支架工业设计项目", "images")
        == "Workspaces/桌面耳机支架工业设计项目"
    )


def test_unscoped_media_is_saved_at_knowledge_root(tmp_path):
    from packages.core.ai.runtime.media_outputs import runtime_generated_media_target

    target = runtime_generated_media_target(
        kind="image", entity_id="entity", workspace_base_dir="", ext=".png",
        prompt="cover", output_name="cover.png", entity_root=str(tmp_path),
    )
    assert target.rel_path == "cover.png"
    assert target.abs_path == str(tmp_path / "cover.png")


@pytest.mark.parametrize("archive", ["tasks/TASK1", "workflow/RUN1"])
def test_execution_archives_keep_local_media_buckets(archive):
    base = f"Workspaces/_by_id/FOLDER1/{archive}"
    assert workspace_artifact_default_dir(base, "images") == f"{base}/images"


def test_new_workflow_archive_uses_stable_lineage_instead_of_task():
    from packages.core.services.generated_media_naming import workspace_execution_artifact_dir

    base = "Workspaces/_by_id/FOLDER1"
    first = workspace_execution_artifact_dir(
        base, task_id="TASK1", workflow_run_id="RUN1", workflow_lineage_root_run_id="RUN1",
    )
    retry = workspace_execution_artifact_dir(
        base, task_id="TASK1", workflow_run_id="RUN2", workflow_lineage_root_run_id="RUN1",
    )
    assert first == retry == f"{base}/workflow/RUN1"


def test_workflow_project_run_saves_into_the_project_folder():
    """A file a WorkflowProject run saves lands where the project's folder view finds it."""
    from packages.core.services.generated_media_naming import workspace_execution_artifact_dir

    base = "Workspaces/_by_id/FOLDER1"
    archive = workspace_execution_artifact_dir(
        base, workflow_run_id="RUN2", workflow_lineage_root_run_id="RUN1",
        workflow_project_root="projects/onboarding-article",
    )
    assert archive == f"{base}/projects/onboarding-article"
    assert scope_workspace_artifact_path("final.docx", archive, default_subdir="artifacts") == (
        f"{base}/projects/onboarding-article/final.docx"
    )
    # Only a runtime-assigned project folder counts, and only for a Workflow run.
    assert workspace_execution_artifact_dir(
        base, workflow_run_id="RUN1", workflow_project_root="briefs/a",
    ) == f"{base}/workflow/RUN1"
    assert workspace_execution_artifact_dir(
        base, task_id="TASK1", workflow_project_root="projects/onboarding-article",
    ) == f"{base}/tasks/TASK1"


def test_a_project_run_keeps_a_file_it_shares_in_the_project_folder():
    """"Make the handoff note available" (storage_scope workspace) inside a
    WorkflowProject run still lands where the project's App and reviews look."""
    from packages.core.ai.runtime.tool_context import RuntimeToolCallContext
    from packages.core.ai.runtime.tool_execution import _output_scope, _runtime_project_storage_scope
    from packages.core.services.generated_media_naming import workspace_execution_artifact_dir

    arguments = {"kind": "document", "name": "handoff-note.md", "storage_scope": "workspace"}
    in_project = RuntimeToolCallContext(
        entity_id="ENT", workspace_id="WS", workflow_run_id="RUN1",
        workflow_lineage_root_run_id="RUN1", workflow_project_root="projects/onboarding-article",
    )
    scope = _output_scope(in_project, "generate_file", _runtime_project_storage_scope(arguments, in_project))
    assert scope.workspace_shared is False
    assert workspace_execution_artifact_dir(
        "Workspaces/_by_id/FOLDER1", workflow_run_id=scope.workflow_run_id,
        workflow_project_root=scope.workflow_project_root,
    ) == "Workspaces/_by_id/FOLDER1/projects/onboarding-article"
    # Without a project the Workspace still shares it from its home; a named asset stays shared.
    plain = RuntimeToolCallContext(entity_id="ENT", workspace_id="WS", workflow_run_id="RUN1")
    assert _output_scope(plain, "generate_file", _runtime_project_storage_scope(arguments, plain)).workspace_shared
    asset = {"kind": "image", "prompt": "logo", "storage_scope": "workspace", "params": {"workspace_asset_key": "logo"}}
    assert _runtime_project_storage_scope(asset, in_project) == asset


def test_unscoped_task_does_not_create_an_entity_root_task_archive():
    from packages.core.services.generated_media_naming import workspace_execution_artifact_dir

    assert workspace_execution_artifact_dir("", task_id="TASK1") == ""


def test_workspace_artifact_path_prefixes_explicit_folder():
    path = scope_workspace_artifact_path(
        "images/hs-01-正视图-front-view.png",
        "Workspaces/桌面耳机支架工业设计项目",
        preserve_leaf_default=True,
    )

    assert path == "Workspaces/桌面耳机支架工业设计项目/images/hs-01-正视图-front-view.png"


def test_workspace_artifact_path_leaves_leaf_for_media_default_dir():
    path = scope_workspace_artifact_path(
        "hs-01-正视图-front-view.png",
        "Workspaces/桌面耳机支架工业设计项目",
        preserve_leaf_default=True,
    )

    assert path == "hs-01-正视图-front-view.png"


def test_workspace_artifact_path_keeps_plain_documents_at_workspace_root():
    path = scope_workspace_artifact_path(
        "quote.pdf",
        "Workspaces/销售方案",
        default_subdir="documents",
    )

    assert path == "Workspaces/销售方案/quote.pdf"


def test_canonical_workspace_path_is_not_nested_again_under_a_task():
    path = "Workspaces/_by_id/FOLDER1/brand/guide.md"
    assert scope_workspace_artifact_path(path, "Workspaces/_by_id/FOLDER1/tasks/TASK1") == path


def test_workflow_relative_prefix_is_not_nested_twice():
    base = "Workspaces/_by_id/FOLDER1/workflow/RUN1"
    assert scope_workspace_artifact_path("workflow/RUN1/images/cover.png", base) == f"{base}/images/cover.png"


def test_shared_ledger_keeps_workspace_scope_inside_task():
    assert scope_workspace_artifact_path(
        "topic-ledger/events/event.json", "Workspaces/_by_id/FOLDER1/tasks/TASK1",
    ) == "Workspaces/_by_id/FOLDER1/topic-ledger/events/event.json"


def test_runtime_artifact_scope_is_bounded_and_restored():
    from packages.core.services.generated_media_naming import (
        WorkspaceArtifactScope, current_workspace_artifact_scope, workspace_artifact_scope,
    )

    scope = WorkspaceArtifactScope("entity", "workspace", "RUN2", "RUN1")
    assert current_workspace_artifact_scope("entity", "workspace") is None
    with workspace_artifact_scope(scope):
        assert current_workspace_artifact_scope("entity", "workspace") == scope
        assert current_workspace_artifact_scope("other", "workspace") is None
        assert current_workspace_artifact_scope("entity", "other") is None
        with workspace_artifact_scope(None):
            assert current_workspace_artifact_scope("entity", "workspace") is None
        assert current_workspace_artifact_scope("entity", "workspace") == scope
    assert current_workspace_artifact_scope("entity", "workspace") is None


def test_workspace_artifact_path_rescopes_legacy_workspace_path():
    path = scope_workspace_artifact_path(
        "Workspaces/旧名称/documents/quote.pdf",
        "Workspaces/_by_id/01KQ9FOLDER8WJQC7KW18NYGR",
    )

    assert path == "Workspaces/_by_id/01KQ9FOLDER8WJQC7KW18NYGR/documents/quote.pdf"


def test_workspace_artifact_path_cannot_escape_to_another_workspace():
    path = scope_workspace_artifact_path(
        "Workspaces/_by_id/OTHERFOLDER/images/hero.png",
        "Workspaces/_by_id/01KQ9FOLDER8WJQC7KW18NYGR",
    )

    assert path == "Workspaces/_by_id/01KQ9FOLDER8WJQC7KW18NYGR/images/hero.png"
