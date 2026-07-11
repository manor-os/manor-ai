from packages.core.services.document_metadata import (
    merge_document_metadata,
    metadata_artifact,
    metadata_origin,
)


def test_merge_document_metadata_uses_structured_sections_without_duplicate_columns():
    meta = merge_document_metadata(
        origin={
            "workspace_id": "ws_1",
            "task_id": "task_1",
            "agent_id": "agent_1",
            "conversation_id": "conv_1",
            "user_id": "user_1",
            "tool_name": "generate_video",
        },
        artifact={"role": "final", "storage_scope": "artifact"},
        generation={"model": "seedance", "prompt": "make a video", "params": {"duration": 10}},
    )

    assert meta["schema_version"] == 2
    assert meta["origin"]["workspace_id"] == "ws_1"
    assert meta["artifact"]["role"] == "final"
    assert meta["generation"]["params"]["duration"] == 10
    assert "workspace_id" not in meta
    assert "task_id" not in meta
    assert "artifact_role" not in meta
    assert "storage_scope" not in meta


def test_merge_document_metadata_canonicalizes_legacy_origin_ids():
    meta = merge_document_metadata(
        {
            "workspace_id": "ws_old",
            "task_id": "task_old",
            "origin": {"agent_id": "agent_existing"},
        },
        origin={"workspace_id": "ws_new"},
        extra={"task_id": "task_extra", "note": "keep"},
    )

    assert meta["origin"]["workspace_id"] == "ws_new"
    assert meta["origin"]["task_id"] == "task_old"
    assert meta["origin"]["agent_id"] == "agent_existing"
    assert meta["note"] == "keep"
    assert "workspace_id" not in meta
    assert "task_id" not in meta


def test_metadata_accessors_only_read_canonical_origin_ids():
    legacy = {
        "workspace_id": "ws_legacy",
        "task_id": "task_legacy",
        "artifact_role": "final",
        "storage_scope": "artifact",
    }

    assert metadata_origin(legacy) == {}
    assert metadata_artifact(legacy)["role"] == "final"
    assert metadata_artifact(legacy)["storage_scope"] == "artifact"
