import pytest

from packages.core.contracts.shapes import (
    coerce_to_shape,
    get_shape,
    shape_names,
)


def test_artifact_shape_normalizes_path_alias_to_fs_path():
    shape = get_shape("ArtifactResult")
    out = shape.normalize({"files": [{"name": "RULES.md", "path": "Workspaces/demo/RULES.md"}]})
    assert out["files"][0]["fs_path"] == "Workspaces/demo/RULES.md"
    assert out["files"][0]["name"] == "RULES.md"


def test_artifact_shape_json_schema_requires_fs_path():
    shape = get_shape("ArtifactResult")
    schema = shape.json_schema()
    item = schema["properties"]["files"]["items"]
    assert "fs_path" in item["required"]
    assert "name" in item["required"]


@pytest.mark.parametrize(
    "name",
    [
        "TextResult",
        "DocumentResult",
        "ListResult",
        "PublishResult",
        "CountResult",
        "EmptyResult",
    ],
)
def test_shape_registered_and_has_schema(name):
    shape = get_shape(name)
    assert shape.json_schema()["type"] == "object"


def test_text_result_folds_content_alias():
    out = get_shape("TextResult").normalize({"content": "hello"})
    assert out["text"] == "hello"


def test_list_result_unwraps_data_alias():
    out = get_shape("ListResult").normalize({"data": [1, 2, 3]})
    assert out["items"] == [1, 2, 3]


def test_publish_result_folds_post_url_alias():
    out = get_shape("PublishResult").normalize(
        {"post_url": "https://x.com/i/web/status/1", "created_at": "2026-06-13T00:00:00Z", "status": "published"}
    )
    assert out["url"] == "https://x.com/i/web/status/1"
    assert out["published_at"] == "2026-06-13T00:00:00Z"


def test_count_result_parses_int():
    out = get_shape("CountResult").normalize({"draft_count": 3})
    assert out["count"] == 3


def test_coerce_to_shape_normalizes_and_returns_dict():
    raw = {"files": [{"name": "a.md", "path": "Workspaces/x/a.md"}]}
    out = coerce_to_shape("ArtifactResult", raw)
    assert out["files"][0]["fs_path"] == "Workspaces/x/a.md"


def test_coerce_to_shape_extracts_text_from_string():
    out = coerce_to_shape("TextResult", "just prose")
    assert out["text"] == "just prose"


def test_shape_names_lists_core_shapes():
    assert {
        "ArtifactResult",
        "TextResult",
        "DocumentResult",
        "ListResult",
        "PublishResult",
        "CountResult",
        "EmptyResult",
    }.issubset(set(shape_names()))


def test_draftpack_shape_registered():
    assert "DraftPack" in shape_names()
    schema = get_shape("DraftPack").json_schema()
    assert "drafts" in schema["properties"]
    assert schema["properties"]["drafts"]["type"] == "array"


def test_draftpack_normalizes_posts_alias():
    out = coerce_to_shape("DraftPack", {"posts": [{"draft": "hi", "label": "P1"}]})
    assert out["drafts"][0]["text"] == "hi"
    assert out["drafts"][0]["label"] == "P1"
