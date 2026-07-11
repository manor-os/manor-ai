import pytest

from packages.core.models.runtime_learning import AgentLearningCandidate
from packages.core.services.runtime_learning import (
    _apply_candidate_payload,
    _compact_agent_file_content,
    build_chat_learning_candidate_drafts,
    runtime_status_from_stop_reason,
)


def test_runtime_status_from_stop_reason():
    assert runtime_status_from_stop_reason("completed") == "succeeded"
    assert runtime_status_from_stop_reason("credit_exhausted") == "blocked"
    assert runtime_status_from_stop_reason("error") == "failed"
    assert runtime_status_from_stop_reason("error", has_content=True) == "partial"


def test_memory_candidate_from_user_guidance():
    drafts = build_chat_learning_candidate_drafts(
        user_message="以后发 post 必须先给我审核批准。",
        assistant_content="我会先准备草稿。",
        tool_calls_made=[],
        status="succeeded",
        stop_reason="completed",
    )
    memory = [d for d in drafts if d.candidate_type == "memory"]
    assert len(memory) == 1
    assert memory[0].scope == "user"
    assert memory[0].payload["memory_type"] == "instruction"
    assert "审核" in memory[0].payload["content"]


def test_agent_profile_candidate_from_role_guidance():
    drafts = build_chat_learning_candidate_drafts(
        user_message="以后你是这个 workspace 的 lease consultant，负责先总结客户需求再推荐房源。",
        assistant_content="我会按这个身份处理租房咨询。",
        tool_calls_made=[],
        status="succeeded",
        stop_reason="completed",
    )
    profile = [d for d in drafts if d.candidate_type == "agent_profile_patch"]
    assert len(profile) == 1
    assert profile[0].payload["apply_target"] == "AGENT.md"
    assert profile[0].payload["auto_apply_eligible"] is True


def test_agent_file_compaction_only_summarizes_managed_blocks():
    managed_blocks = "\n\n".join(
        "\n".join(
            [
                f"<!-- runtime-learning:{i} -->",
                f"## Runtime Learning: Update {i}",
                f"- Agent profile update: Long managed update {i}",
                "<!-- /runtime-learning -->",
            ]
        )
        for i in range(20)
    )
    existing = "# Agent Profile\n\nUser-authored content stays intact.\n\n" + managed_blocks
    existing += "\n" + ("extra text " * 1400)

    compacted, meta = _compact_agent_file_content(existing, filename="AGENT.md")

    assert meta["compacted_blocks"] == 20
    assert "User-authored content stays intact." in compacted
    assert "<!-- runtime-learning-summary -->" in compacted
    assert "<!-- runtime-learning:0 -->" not in compacted
    assert "Full original context remains available in runtime evidence" in compacted


def test_tool_pattern_and_skill_candidates_from_repeated_success():
    drafts = build_chat_learning_candidate_drafts(
        user_message="Prepare a leasing follow-up email and schedule a tour.",
        assistant_content="Drafted the email and scheduled the event.",
        tool_calls_made=["workspace_search", "gmail_draft", "calendar_create_event"],
        status="succeeded",
        stop_reason="completed",
        repeated_tool_pattern_count=3,
    )
    types = {d.candidate_type for d in drafts}
    assert "tool_experience" in types
    assert "skill" in types
    skill = next(d for d in drafts if d.candidate_type == "skill")
    assert skill.risk_level == "medium"
    assert "gmail_draft" in skill.payload["suggested_tools"]


def test_profile_patch_candidate_requires_repeated_failure():
    once = build_chat_learning_candidate_drafts(
        user_message="Generate the report.",
        assistant_content="",
        tool_calls_made=["generate_file"],
        status="failed",
        stop_reason="consecutive_tool_errors",
        repeated_failure_count=1,
    )
    assert not [d for d in once if d.candidate_type == "profile_patch"]

    repeated = build_chat_learning_candidate_drafts(
        user_message="Generate the report.",
        assistant_content="",
        tool_calls_made=["generate_file"],
        status="failed",
        stop_reason="consecutive_tool_errors",
        repeated_failure_count=2,
    )
    assert [d for d in repeated if d.candidate_type == "profile_patch"]


@pytest.mark.asyncio
async def test_workspace_agent_profile_patch_targets_workspace_override(tmp_path):
    from packages.core.config import get_settings
    from packages.core.memory.canonical import read_workspace_agent_memory_file
    from packages.core.services.agent_files import effective_agent_id, read_agent_file
    from packages.core.services.entity_fs import provision_entity_filesystem

    settings = get_settings()
    old_root = settings.MANOR_FS_ROOT
    old_enabled = settings.MANOR_FS_ENABLED
    try:
        settings.MANOR_FS_ROOT = str(tmp_path)
        settings.MANOR_FS_ENABLED = True
        provision_entity_filesystem("ent_runtime_learning", "Runtime Learning Inc")

        candidate = AgentLearningCandidate(
            id="cand_workspace_profile",
            entity_id="ent_runtime_learning",
            workspace_id="ws_runtime_learning",
            agent_id=None,
            user_id="user_runtime_learning",
            candidate_type="agent_profile_patch",
            scope="agent",
            title="Update workspace agent role",
            summary="Workspace-local leasing role update.",
            payload={
                "profile_update": "以后你是这个 workspace 的 lease consultant。",
                "auto_apply_eligible": True,
                "target_scope": "workspace_agent",
            },
            evidence_ids=["ev_runtime_learning"],
            dedupe_key="agent_profile:test",
            risk_level="low",
            status="proposed",
            confidence=0.8,
            created_by="runtime",
        )

        result = await _apply_candidate_payload(
            None,
            candidate,
            entity_id="ent_runtime_learning",
            user_id="user_runtime_learning",
        )

        assert result["kind"] == "workspace_agent_file"
        assert result["filename"] == "AGENT.md"
        agent_key = effective_agent_id(None)
        override = read_workspace_agent_memory_file(
            "ent_runtime_learning",
            "ws_runtime_learning",
            agent_key,
            "AGENT.md",
        )
        assert override and "lease consultant" in override

        global_agent = read_agent_file("ent_runtime_learning", agent_key, "AGENT.md") or ""
        assert "lease consultant" not in global_agent

        from packages.core.ai.runtime.prompt_adapter import (
            ChatContext,
            build_default_prompt_builder,
        )

        prompt = await build_default_prompt_builder().build(
            ChatContext(
                entity_id="ent_runtime_learning",
                workspace_id="ws_runtime_learning",
                agent_id=None,
                workspace=type("WorkspaceObj", (), {"name": "Runtime Learning WS", "kind": "leasing"})(),
                mode="full",
            )
        )
        assert "Workspace-Agent Override Memory" in prompt
        assert "lease consultant" in prompt
    finally:
        settings.MANOR_FS_ROOT = old_root
        settings.MANOR_FS_ENABLED = old_enabled
