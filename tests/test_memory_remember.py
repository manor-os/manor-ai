"""remember: who may write which memory layer, how entries are edited, and undo."""
import os

import pytest

from packages.core.config import get_settings
from packages.core.memory.layers import MemoryTurnScope, org_memory_path, personal_memory_path, workspace_memory_path
from packages.core.memory.remember import MemoryWriteRefused, apply_memory_edit, contains_secret

E, OWNER, MEMBER, WS = "ent_remember", "user_rem_owner", "user_rem_member", "ws_remember"


def test_entries_are_edited_by_section_and_line():
    text = apply_memory_edit("", layer="workspace", action="add", content="VC has no cup threshold", section="Corrections")
    text = apply_memory_edit(text, layer="workspace", action="add", content="Draft before sending")
    text = apply_memory_edit(text, layer="workspace", action="add", content="Logo drifted in v7", section="corrections")
    corrections = text.split("## Corrections")[1]
    assert "VC has no cup threshold" in corrections and "Logo drifted in v7" in corrections
    assert apply_memory_edit(text, layer="workspace", action="add", content="Logo drifted   in v7") == text

    text = apply_memory_edit(text, layer="workspace", action="update", entry="cup threshold",
                             content="VC and I-3.0 have capacities, not thresholds")
    assert "no cup threshold" not in text and "capacities, not thresholds" in text
    text = apply_memory_edit(text, layer="workspace", action="remove", entry="Logo drifted")
    assert "Logo drifted" not in text
    with pytest.raises(MemoryWriteRefused):
        apply_memory_edit(text, layer="workspace", action="remove", entry="nothing like this")


SETUP_BLOCK = (
    "\n<!-- runtime-learning:01SETUP -->\n## Runtime Learning: About Ops\n"
    "- Source: workspace_setup.\n- Workspace memory: CANARY-SETUP charter\n<!-- /runtime-learning -->\n"
)
LEARNED_BLOCK = (
    "\n<!-- runtime-learning:01LEARNED -->\n## Runtime Learning: Cup sizes\n"
    "- Source: chat.\n- Workspace memory: CANARY-LEARNED\n<!-- /runtime-learning -->\n"
)


def test_workspace_memory_reads_without_what_setup_seeded():
    from packages.core.memory.canonical import WORKSPACE_MEMORY_FILES, workspace_memory_text

    template = WORKSPACE_MEMORY_FILES["MEMORY.md"].default_body
    assert workspace_memory_text(template) == ""
    assert workspace_memory_text(template + SETUP_BLOCK) == ""
    # As staging files look after a first write: real entries below the seeded part.
    text = workspace_memory_text(template + SETUP_BLOCK + LEARNED_BLOCK + "\n## Facts\n- VC has no cup threshold\n")
    assert text.startswith("# Workspace Memory") and "CANARY-LEARNED" in text and "- VC has no cup threshold" in text
    assert "CANARY-SETUP" not in text and "## Stable Facts" not in text and "Add durable facts" not in text


def test_sections_never_match_the_file_title():
    text = apply_memory_edit("", layer="preferences", action="add", content="Reply in Chinese", section="Preferences")
    text = apply_memory_edit(text, layer="preferences", action="add", content="Send drafts first", section="Guidance")
    text = apply_memory_edit(text, layer="preferences", action="add", content="No emoji", section="Preferences")
    assert text.split("## Guidance")[0].count("- ") == 2


@pytest.mark.parametrize("text, secret", [
    ("API key sk-proj-abcdefgh12345678", True),
    ("验证码 482913", True),
    ("my password is hunter22", True),
    # Construct the fake token at runtime so the OSS source scan stays strict.
    ("token ghp_" + "abcdefghijklmnopqrstuvwxyz0123", True),
    ("I post on pinterest daily", False),
    ("Prefers password managers over sticky notes", False),
    ("Reply in Chinese", False),
])
def test_secrets_are_recognised_by_shape(text, secret):
    assert contains_secret(text) is secret


@pytest.fixture
def remember_fs(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "DEPLOYMENT_MODE", "self_hosted")
    from packages.core.services.entity_fs import provision_entity_filesystem

    provision_entity_filesystem(E, "Remember Inc")
    return tmp_path


def _read(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


async def test_writes_follow_layer_permissions_and_can_be_undone(client, remember_fs):
    import packages.core.database as database
    from sqlalchemy import select
    from packages.core.memory.remember import remember_memory
    from packages.core.models.runtime_learning import AgentLearningCandidate
    from packages.core.models.user import User
    from packages.core.models.workspace import Workspace, WorkspaceStaff

    async with database.async_session() as db:
        db.add_all([
            User(id=OWNER, entity_id=E, email="rem-owner@test.invalid", password_hash="test", role="owner"),
            User(id=MEMBER, entity_id=E, email="rem-member@test.invalid", password_hash="test", role="member"),
            Workspace(id=WS, entity_id=E, name="Remember Ops", status="active"),
            WorkspaceStaff(workspace_id=WS, user_id=MEMBER, role="editor", status="active"),
        ])
        await db.commit()

    own_owner = MemoryTurnScope(False, True, OWNER, None)
    own_member = MemoryTurnScope(False, True, MEMBER, None)
    member_in_ws = MemoryTurnScope(False, False, MEMBER, WS)
    scheduled_run = MemoryTurnScope(False, False, None, WS)

    async with database.async_session() as db:
        async def write(scope, target, content="", action="add", **kw):
            return await remember_memory(db, scope, entity_id=E, target=target, action=action, content=content,
                                         conversation_id="c_rem", **kw)

        assert (await write(own_owner, "org", "Brand name is spoken as one word"))["status"] == "saved"
        with pytest.raises(MemoryWriteRefused):
            await write(own_member, "org", "Members cannot set company memory")
        with pytest.raises(MemoryWriteRefused):
            await write(own_member, "workspace", "No Workspace in the outer chat")
        with pytest.raises(MemoryWriteRefused):
            await write(scheduled_run, "my_preferences", "A run has no speaker")
        with pytest.raises(MemoryWriteRefused):
            await write(MemoryTurnScope(True, False, None, None), "workspace", "External channels never write")
        with pytest.raises(MemoryWriteRefused):
            await write(own_member, "my_preferences", "my password is hunter22")

        # What setup seeded is replaced, not kept around the first entry; learned blocks stay.
        from packages.core.memory.canonical import WORKSPACE_MEMORY_FILES

        template = WORKSPACE_MEMORY_FILES["MEMORY.md"].default_body
        os.makedirs(os.path.dirname(workspace_memory_path(E, WS)), exist_ok=True)
        with open(workspace_memory_path(E, WS), "w", encoding="utf-8") as f:
            f.write(template + SETUP_BLOCK + LEARNED_BLOCK)
        await write(member_in_ws, "workspace", "VC has no cup threshold", section="Corrections")
        placeholder = next(line for line in template.splitlines() if line.startswith("- "))
        seeded = _read(workspace_memory_path(E, WS))
        assert placeholder not in seeded and "CANARY-SETUP" not in seeded and "CANARY-LEARNED" in seeded
        await write(scheduled_run, "workspace", "Quote the cost before generating shots")
        await write(member_in_ws, "my_preferences", "Reply in Chinese")
        assert "Reply in Chinese" in _read(personal_memory_path(E, MEMBER, "preferences"))
        assert "Brand name" in _read(org_memory_path(E))

        # The speaker's latest write in this conversation is rolled back.
        assert (await write(member_in_ws, "my_preferences", action="undo"))["status"] == "undone"
        assert "Reply in Chinese" not in _read(personal_memory_path(E, MEMBER, "preferences"))
        workspace_text = _read(workspace_memory_path(E, WS))
        assert "VC has no cup threshold" in workspace_text and "Quote the cost" in workspace_text

        rows = (await db.scalars(select(AgentLearningCandidate).where(
            AgentLearningCandidate.entity_id == E, AgentLearningCandidate.candidate_type == "memory",
        ))).all()
        assert sorted(r.status for r in rows) == ["applied", "applied", "applied", "rolled_back"]
        assert {r.user_id for r in rows} == {OWNER, MEMBER, None}



def test_extraction_may_only_choose_an_offered_layer():
    from packages.core.memory.manager import _validate

    item = {"scope": "fact", "title": "Plan", "body": "Launch in March", "tags": [],
            "source_message_id": "m1", "confidence": 0.6}
    personal = {"source_message_ids": ["m1"], "layers": ["profile", "preferences", "private"]}
    assert _validate("extract", {"entries": [{**item, "layer": "private"}]}, personal)["entries"][0]["layer"] == "private"
    assert _validate("extract", {"entries": [item]}, personal)["entries"][0]["layer"] == "private"
    with pytest.raises(ValueError, match="Invalid memory layer"):
        _validate("extract", {"entries": [{**item, "layer": "workspace"}]}, personal)
