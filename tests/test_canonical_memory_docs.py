import pytest

from packages.core.config import get_settings
from packages.core.ai.runtime import AIRuntimeRequest, ChatSurface, RuntimeResolver
from packages.core.ai.runtime.prompt_adapter import ChatContext
from packages.core.ai.runtime.prompt_context import runtime_agent_memories_prompt
from packages.core.ai.runtime.prompt_sections import agent_identity, workspace_operating_memory
from packages.core.memory.canonical import (
    WORKSPACE_MEMORY_FILES,
    append_workspace_memory_block,
    ensure_workspace_memory_docs,
    load_workspace_operating_memory,
    write_workspace_memory_file,
)
from packages.core.memory.repo import ensure_workspace_memory_dirs
from packages.core.services.agent_files import (
    AGENT_FILE_NAMES,
    agent_files_have_custom_content,
    build_agent_prompt_from_files,
    load_agent_files,
    write_agent_file,
)
from packages.core.services.entity_fs import provision_agent_workspace, provision_entity_filesystem


def test_canonical_workspace_memory_docs_are_created_and_bounded(tmp_path):
    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    try:
        settings.MANOR_FS_ROOT = str(tmp_path)
        settings.MANOR_FS_ENABLED = True

        provision_entity_filesystem("ent_memory_docs", "Memory Docs Inc")
        ensure_workspace_memory_dirs("ent_memory_docs", "ws_memory_docs")
        paths = ensure_workspace_memory_docs(
            "ent_memory_docs",
            "ws_memory_docs",
            workspace_name="Leasing Ops",
            workspace_kind="property",
        )

        assert set(WORKSPACE_MEMORY_FILES).issubset(paths)
        assert "Leasing Ops" in open(paths["WORKSPACE.md"], encoding="utf-8").read()

        write_workspace_memory_file(
            "ent_memory_docs",
            "ws_memory_docs",
            "RULES.md",
            "# Rules\n\n" + ("Always cite the source. " * 500),
        )
        block = load_workspace_operating_memory(
            "ent_memory_docs",
            "ws_memory_docs",
            max_chars=2_400,
        )

        assert "### WORKSPACE.md" in block
        assert "### RULES.md" in block
        assert ("Always cite the source. " * 500).rstrip() in block

        write_workspace_memory_file(
            "ent_memory_docs", "ws_memory_docs", "STATE.md", "# State\n\n- CANARY-STALE-STATE",
        )
        prompt_ctx = ChatContext(
            entity_id="ent_memory_docs",
            workspace_id="ws_memory_docs",
            workspace=type("WorkspaceObj", (), {"name": "Leasing Ops", "kind": "property"})(),
        )
        prompt_block = workspace_operating_memory(prompt_ctx)
        assert prompt_block
        assert "### RULES.md" in prompt_block
        assert "### WORKSPACE.md" not in prompt_block
        assert "### MEMORY.md" not in prompt_block
        assert "CANARY-STALE-STATE" not in prompt_block

        write_workspace_memory_file(
            "ent_memory_docs", "ws_memory_docs", "MEMORY.md",
            "# Workspace Memory\n\n- The brochure has no cup-volume threshold.",
        )
        assert "no cup-volume threshold" not in (workspace_operating_memory(prompt_ctx) or "")
        from packages.core.memory.layers import MemoryLayerFile, render_layer_file, workspace_memory_path
        layer = render_layer_file("ent_memory_docs", MemoryLayerFile(
            "workspace", workspace_memory_path("ent_memory_docs", "ws_memory_docs"), "ws_memory_docs",
        ))
        assert "no cup-volume threshold" in layer["content"]

        result = append_workspace_memory_block(
            "ent_memory_docs",
            "ws_memory_docs",
            "LEARNINGS.md",
            "<!-- runtime-learning:candidate-1 -->\n"
            "## Runtime Learning: Test\n"
            "- Learning: Similar tasks work better when batched.\n"
            "<!-- /runtime-learning -->",
            marker="runtime-learning:candidate-1",
        )
        assert result["kind"] == "workspace_memory_file"
        result_again = append_workspace_memory_block(
            "ent_memory_docs",
            "ws_memory_docs",
            "LEARNINGS.md",
            "duplicate",
            marker="runtime-learning:candidate-1",
        )
        assert result_again["already_present"] is True
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


def test_workspace_operating_memory_compacts_runtime_learning_with_provenance(tmp_path):
    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    try:
        settings.MANOR_FS_ROOT = str(tmp_path)
        settings.MANOR_FS_ENABLED = True

        provision_entity_filesystem("ent_runtime_prompt", "Runtime Prompt Inc")
        ensure_workspace_memory_dirs("ent_runtime_prompt", "ws_runtime_prompt")
        ensure_workspace_memory_docs(
            "ent_runtime_prompt",
            "ws_runtime_prompt",
            workspace_name="Social Ops",
            workspace_kind="workspace",
        )

        managed_blocks = "\n\n".join(
            "\n".join(
                [
                    f"<!-- runtime-learning:candidate-{i} -->",
                    f"## Runtime Learning: Rule {i}",
                    "- Summary: Generic candidate summary should not hide the durable rule.",
                    f"- Rule/guidance: Durable approval rule {i} should stay connected to its evidence.",
                    "<!-- /runtime-learning -->",
                ]
            )
            for i in range(18)
        )
        latest_rule = "Always cite the newest approved source before drafting."
        write_workspace_memory_file(
            "ent_runtime_prompt",
            "ws_runtime_prompt",
            "RULES.md",
            "# Workspace Rules\n\n"
            + ("User-authored policy stays visible. " * 80)
            + "\n\n"
            + managed_blocks
            + "\n\n"
            + "\n".join(
                [
                    "<!-- runtime-learning:candidate-new -->",
                    "## Runtime Learning: Newest Rule",
                    "- Summary: The user gave guidance that may need to persist.",
                    f"- Rule/guidance: {latest_rule}",
                    "<!-- /runtime-learning -->",
                ]
            ),
        )

        block = load_workspace_operating_memory(
            "ent_runtime_prompt",
            "ws_runtime_prompt",
            filenames=["RULES.md"],
            max_chars=1_500,
        )

        assert len(block) > 1_500
        assert "runtime-learning:candidate-0" in block
        assert "runtime-learning:candidate-new" in block
        assert latest_rule in block
        assert "User-authored policy stays visible. " * 80 in block
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


def test_agent_fixed_memory_docs_are_supported_without_loading_placeholders(tmp_path):
    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    try:
        settings.MANOR_FS_ROOT = str(tmp_path)
        settings.MANOR_FS_ENABLED = True

        provision_entity_filesystem("ent_agent_docs", "Agent Docs Inc")
        provision_agent_workspace("ent_agent_docs", "leasing-agent")

        assert "SKILLS.md" in AGENT_FILE_NAMES
        assert "LEARNINGS.md" in AGENT_FILE_NAMES
        assert "MEMORY.md" in AGENT_FILE_NAMES

        placeholder_files = load_agent_files("ent_agent_docs", "leasing-agent")
        assert not agent_files_have_custom_content(placeholder_files, "leasing-agent")

        write_agent_file(
            "ent_agent_docs",
            "leasing-agent",
            "SKILLS.md",
            "# Skills\n\n- Prepare lease follow-up messages with source citations.",
        )

        updated_files = load_agent_files("ent_agent_docs", "leasing-agent")
        assert agent_files_have_custom_content(updated_files, "leasing-agent")
        prompt = build_agent_prompt_from_files("ent_agent_docs", "leasing-agent")
        assert "Agent Skills Memory" in prompt
        assert "Describe this agent's expertise" not in prompt
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


@pytest.fixture
def memory_fs(tmp_path):
    settings = get_settings()
    old_root, old_enabled = settings.MANOR_FS_ROOT, settings.MANOR_FS_ENABLED
    settings.MANOR_FS_ROOT = str(tmp_path)
    settings.MANOR_FS_ENABLED = True
    try:
        yield tmp_path
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled


def _identity_prompt(surface, **request):
    envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(surface=surface, entity_id="ent_layers", user_id="user_owner", **request)
    )
    ctx = ChatContext(
        entity_id="ent_layers",
        user_id="user_owner",
        agent_id=request.get("agent_id"),
        workspace_id=request.get("workspace_id"),
        runtime_envelope=envelope,
    )
    return agent_identity(ctx) or ""


def test_personal_and_master_memory_files_stay_out_of_workspace_chat(memory_fs):
    provision_entity_filesystem("ent_layers", "Layers Inc")
    provision_agent_workspace("ent_layers", "agent_sales")
    write_agent_file("ent_layers", None, "MEMORY.md", "# Memory\n\n- CANARY-MASTER-ENTITY")
    write_agent_file(
        "ent_layers", None, "MEMORY.md", "# Memory\n\n- CANARY-MASTER-PERSONAL", user_id="user_owner",
    )
    write_agent_file("ent_layers", "agent_sales", "MEMORY.md", "# Memory\n\n- CANARY-AGENT-ENTITY")
    write_agent_file(
        "ent_layers", "agent_sales", "MEMORY.md", "# Memory\n\n- CANARY-AGENT-PERSONAL",
        user_id="user_owner",
    )

    owner = _identity_prompt(ChatSurface.GLOBAL_OWNER_CHAT)
    workspace = _identity_prompt(ChatSurface.WORKSPACE_CHAT, workspace_id="ws_layers")
    service = _identity_prompt(
        ChatSurface.WORKSPACE_CHAT, workspace_id="ws_layers", agent_id="agent_sales",
    )

    assert "CANARY-MASTER-PERSONAL" in owner
    assert "CANARY-MASTER" not in workspace
    assert "CANARY-AGENT-ENTITY" in service
    assert "CANARY-AGENT-PERSONAL" not in service


@pytest.mark.asyncio
async def test_workspace_agent_prompt_only_loads_shared_database_memory(monkeypatch):
    async def read_memories(_db, _entity_id, *, user_id, **_kwargs):
        return "personal canary" if user_id else "shared canary"

    monkeypatch.setattr("packages.core.services.memory_service.get_context_memories", read_memories)
    for surface, workspace_id, expected in (
        (ChatSurface.WORKSPACE_CHAT, "ws_layers", "shared canary"),
        (ChatSurface.AGENT_DM, None, "personal canary"),
    ):
        envelope = RuntimeResolver().resolve_trace_envelope(AIRuntimeRequest(
            surface=surface, entity_id="ent_layers", user_id="user_owner",
            agent_id="agent_sales", workspace_id=workspace_id,
        ))
        assert await runtime_agent_memories_prompt(
            object(), envelope=envelope, entity_id="ent_layers",
            agent_id="agent_sales", user_id="user_owner",
        ) == f"## Agent Memories\n{expected}"
