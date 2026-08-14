from packages.core.services.blueprint_cover_service import (
    build_blueprint_cover_template,
)


def _template(
    *,
    title: str = "Workspace OS",
    description: str | None = None,
    tags: list[str] | None = None,
    identity: str = "blueprint-1",
):
    return build_blueprint_cover_template(
        title=title,
        description=description,
        tags=tags or [],
        identity=identity,
    )


def test_blueprint_cover_template_uses_description_for_the_subject():
    assert _template(
        description="Dispatch approved posts to LinkedIn and a newsletter.",
    )["motif"] == "distribution"
    assert _template(
        description="Sell digital products with inventory and checkout.",
    )["motif"] == "commerce"
    assert _template(
        description="Plan, capture, edit, and review browser product videos.",
    )["motif"] == "video"
    assert _template(
        description="Run client delivery for a productized consulting service.",
    )["motif"] == "service"


def test_blueprint_cover_template_supports_chinese_description_signals():
    assert _template(description="自动完成视频拍摄、剪辑和发布前检查")["motif"] == "video"
    assert _template(description="管理商品、库存和电商销售")["motif"] == "commerce"


def test_blueprint_cover_template_is_stable_but_varies_by_identity():
    first = _template(description="A calm reusable workspace.")
    repeated = _template(description="A calm reusable workspace.")
    other = _template(
        description="A calm reusable workspace.",
        identity="blueprint-2",
    )

    assert first == repeated
    assert first["motif"] == "workspace"
    assert first["seed"] != other["seed"]
    assert first["variant"] in {0, 1, 2}
    assert first["palette"] in {"blue", "peach", "sage", "stone"}
