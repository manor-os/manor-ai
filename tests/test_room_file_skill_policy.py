"""Group tool admission must never turn a file grant into arbitrary Skills."""

import pytest

from packages.core.services.room_file_skill_policy import (
    RoomFileToolPolicyError, room_generate_file_schema,
    validate_room_file_tool_arguments,
)
from packages.core.services.room_tool_approval_service import (
    RoomToolApprovalError, _validate_new_file_tool,
)


@pytest.mark.parametrize("slug", ["docx", "pdf", "pptx", "xlsx"])
def test_group_accepts_only_file_skill_slugs(slug):
    normalized, summary = validate_room_file_tool_arguments(
        "invoke_skill", {"skill_id": slug, "input": "Create a file"},
    )
    assert normalized == {"skill_id": slug, "input": "Create a file"}
    assert slug.upper() in summary


@pytest.mark.parametrize("skill", [
    "video-edit", "mcp_knowledge_local", "publish_to_slack",
    "01M39CVRTQ3CNWV0S0Y2RZ1ADJ", "PPTX", "",
])
def test_group_rejects_non_file_or_opaque_skill(skill):
    with pytest.raises(RoomFileToolPolicyError) as error:
        validate_room_file_tool_arguments(
            "invoke_skill", {"skill_id": skill, "input": "Run"},
        )
    assert error.value.code == "group_skill_not_file_skill"


def test_group_rejects_model_supplied_skill_scope():
    with pytest.raises(RoomFileToolPolicyError):
        validate_room_file_tool_arguments(
            "invoke_skill", {
                "skill_id": "pptx", "input": "Create slides",
                "params": {"workspace_id": "private"},
            },
        )


def test_group_generation_schema_distinguishes_text_from_code_bundle():
    description = room_generate_file_schema()["function"]["description"]
    assert "Markdown" in description
    assert "kind=document" in description
    assert "params.files code bundle" in description


@pytest.mark.parametrize("kind", ["document", "image", "audio", "video"])
def test_group_generation_accepts_file_kinds(kind):
    normalized, _ = validate_room_file_tool_arguments(
        "generate_file", {"kind": kind, "prompt": "Generate this"},
    )
    assert normalized["kind"] == kind


@pytest.mark.parametrize(("file_type", "expected_kind"), [
    ("pdf", "pdf"), ("docx", "word_document"),
    ("pptx", "presentation"), ("xlsx", "spreadsheet"),
])
def test_office_document_alias_cannot_bypass_file_skill(file_type, expected_kind):
    normalized, _ = validate_room_file_tool_arguments(
        "generate_file", {"kind": "document", "file_type": file_type, "content": "Model draft"},
    )
    assert normalized["kind"] == expected_kind


def test_office_filename_cannot_bypass_file_skill():
    normalized, _ = validate_room_file_tool_arguments(
        "generate_file", {"kind": "document", "name": "deck.pptx", "content": "Model draft"},
    )
    assert normalized["kind"] == "presentation"


@pytest.mark.parametrize("params", [
    {"source": {"path": "Knowledge/private.pdf"}},
    {"workspace_id": "private"},
    {"reference_urls": ["/api/v1/fs/private.png"]},
    {"sandbox_path": "/workspace/private"},
])
def test_group_generation_rejects_private_references(params):
    with pytest.raises(RoomFileToolPolicyError) as error:
        validate_room_file_tool_arguments(
            "generate_file", {"kind": "image", "prompt": "Draw", "params": params},
        )
    assert error.value.code == "group_generation_reference_not_allowed"


def test_approval_boundary_rejects_model_scope_before_database_access():
    with pytest.raises(RoomToolApprovalError) as error:
        _validate_new_file_tool("generate_file", {
            "kind": "document", "prompt": "Create",
            "params": {"source": {"path": "private.md"}},
        })
    assert error.value.code == "group_generation_reference_not_allowed"


def test_approval_boundary_does_not_treat_invoke_skill_as_file_mutation():
    from packages.core.services.room_tool_approval_service import ROOM_MUTATION_TOOLS

    assert "invoke_skill" not in ROOM_MUTATION_TOOLS


def test_group_runtime_file_grant_does_not_open_public_customer_files():
    from dataclasses import replace

    from packages.core.ai.runtime.policies import check_runtime_tool_policy
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.resolver import RuntimeResolver
    from packages.core.ai.runtime.surfaces import ChatSurface

    request = AIRuntimeRequest(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        entity_id="E" * 26,
        metadata={"room_id": "G" * 26, "room_agent_run_id": "R" * 26},
    )
    ordinary = RuntimeResolver().resolve_trace_envelope(
        request, tool_schemas=[], allowed_tool_names={"generate_file"},
    )
    assert not check_runtime_tool_policy(
        envelope=ordinary, tool_name="generate_file", arguments={"kind": "pdf"},
    ).allowed
    group = replace(
        ordinary, room_file_tool_names=("generate_file", "sandbox"),
        blocked_tool_names=tuple(
            name for name in ordinary.blocked_tool_names if name not in {"generate_file", "sandbox"}
        ),
    )
    assert check_runtime_tool_policy(
        envelope=group, tool_name="generate_file", arguments={"kind": "pdf"},
    ).allowed
    assert not check_runtime_tool_policy(
        envelope=group, tool_name="sandbox_exec", arguments={},
    ).allowed
    assert not check_runtime_tool_policy(
        envelope=group, tool_name="sandbox", arguments={"action": "exec"},
    ).allowed
    group.discovered_tool_grants.grant(["sandbox"])
    assert check_runtime_tool_policy(
        envelope=group, tool_name="sandbox", arguments={"action": "exec"},
    ).allowed
    assert "room_file_tool_names" not in group.to_message_meta()
