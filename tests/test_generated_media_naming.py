from packages.core.services.generated_media_naming import (
    build_workspace_artifact_base_dir,
    build_generated_media_filename,
    build_generated_media_target,
    scope_workspace_artifact_path,
    workspace_artifact_default_dir,
)


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


def test_workspace_artifact_base_dir_uses_workspace_name():
    base = build_workspace_artifact_base_dir(
        workspace_name="桌面耳机支架工业设计项目",
        workspace_id="01KQ9CDDBTNWEZJQC7KW18NYGR",
    )

    assert base == "Workspaces/桌面耳机支架工业设计项目"


def test_workspace_artifact_default_dir_nests_storage_folder():
    assert (
        workspace_artifact_default_dir("Workspaces/桌面耳机支架工业设计项目", "images")
        == "Workspaces/桌面耳机支架工业设计项目/images"
    )


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


def test_workspace_artifact_path_uses_default_subdir_for_documents():
    path = scope_workspace_artifact_path(
        "quote.pdf",
        "Workspaces/销售方案",
        default_subdir="documents",
    )

    assert path == "Workspaces/销售方案/documents/quote.pdf"
