import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.core.ai.tools.extended_tools import _deliver_image_to_sandbox  # noqa: E402
from tests.test_sandbox_session_service import fake_cache  # noqa: E402,F401 - fixture

ENTITY = "ent1"
USER = "user1"
AGENT = "agent-p"


class _FakeClient:
    def __init__(self, sandbox_id, *, fail=None):
        self.sandbox_id = sandbox_id
        self.fail = fail
        self.calls: list = []
        self.closed = False

    async def write_file_base64(self, sandbox_id, path, content_base64, mkdir=True, **view):
        if self.fail is not None:
            raise self.fail
        self.calls.append((sandbox_id, path, content_base64))
        self.views = getattr(self, "views", []) + [view]

    async def close(self):
        self.closed = True


@pytest.fixture
def routed_clients(monkeypatch):
    """Every delivery must reach the sandbox through its own runner route."""

    from packages.core.ai.tools import sandbox_tools

    clients: list[_FakeClient] = []
    state = {"fail": None}

    async def client_for_sandbox(sandbox_id):
        client = _FakeClient(sandbox_id, fail=state["fail"])
        clients.append(client)
        return client

    def forbid_service_url_client(*_args, **_kwargs):
        raise AssertionError("an existing sandbox must not be reached through SANDBOX_SERVICE_URL")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", client_for_sandbox)
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", forbid_service_url_client)
    return clients, state


async def _bind(binding_key, sandbox_id, *, agent_id=AGENT, skill_id="skill_pptx", user_id=USER, skill_key=None):
    from packages.core.ai.runtime.sandbox import (
        runtime_init_sandbox_binding,
        runtime_sandbox_scope,
    )

    scope = runtime_sandbox_scope(entity_id=ENTITY, user_id=user_id, binding_key=binding_key)
    await runtime_init_sandbox_binding(scope, sandbox_id, skill_id, agent_id=agent_id, skill_key=skill_key)


async def _deliver(binding_key="c1", *, sandbox_path="projects/p/images/page_01.png", image_bytes=b"png"):
    return await _deliver_image_to_sandbox(
        entity_id=ENTITY,
        user_id=USER,
        agent_id=AGENT,
        sandbox_binding_key=binding_key,
        sandbox_path=sandbox_path,
        image_bytes=image_bytes,
    )


@pytest.mark.asyncio
async def test_delivers_bytes_through_the_sandboxs_own_runner(fake_cache, routed_clients):
    clients, _ = routed_clients
    await _bind("c1", "sb1")

    raw = b"\x89PNGbinary\x00\xff payload"
    outcome = await _deliver(image_bytes=raw)

    assert outcome == {"path": "projects/p/images/page_01.png", "delivered": True, "sandbox_id": "sb1"}
    client = clients[-1]
    assert client.sandbox_id == "sb1"
    assert client.closed  # client always closed
    sid, path, b64 = client.calls[0]
    assert sid == "sb1"
    assert path == "projects/p/images/page_01.png"
    assert base64.b64decode(b64) == raw  # bytes survive the round-trip


@pytest.mark.asyncio
async def test_targets_the_agents_skill_sandbox_before_its_generic_one(fake_cache, routed_clients):
    clients, _ = routed_clients
    await _bind("c1", "sb_generic", skill_id="")
    assert (await _deliver())["sandbox_id"] == "sb_generic"

    await _bind("c1", "sb_skill")
    assert (await _deliver())["sandbox_id"] == "sb_skill"
    assert [client.sandbox_id for client in clients] == ["sb_generic", "sb_skill"]


@pytest.mark.asyncio
async def test_never_writes_into_another_conversations_sandbox(fake_cache, routed_clients):
    """Another conversation of this user, or another user's sandbox in this
    conversation, is never a delivery target."""

    clients, _ = routed_clients
    await _bind("c2", "sb_other_conversation")
    await _bind("c1", "sb_other_user", user_id="user2")

    outcome = await _deliver()

    assert outcome["delivered"] is False
    assert "No sandbox is bound to this agent" in outcome["error"]
    assert "do not regenerate" in outcome["error"]
    assert clients == []


@pytest.mark.asyncio
async def test_delivers_into_the_shared_sandbox_the_agent_uses(fake_cache, routed_clients):
    """A sandbox a delegate opened is the conversation's: once this agent uses
    it (any acquire binds its view), the image lands there."""

    clients, _ = routed_clients
    await _bind("c1", "sb_shared", agent_id="agent-b")
    assert (await _deliver())["delivered"] is False  # agent-p has not used it yet

    await _bind("c1", "sb_shared", skill_id="")  # agent-p joins (generic)
    assert (await _deliver())["sandbox_id"] == "sb_shared"
    assert [client.sandbox_id for client in clients] == ["sb_shared"]


@pytest.mark.asyncio
async def test_no_binding_scope_reports_instead_of_writing(fake_cache, routed_clients):
    clients, _ = routed_clients

    outcome = await _deliver(binding_key=None)

    assert outcome["delivered"] is False
    assert outcome["error"]
    assert clients == []


@pytest.mark.asyncio
async def test_missing_args_are_reported_not_written(fake_cache, routed_clients):
    clients, _ = routed_clients
    await _bind("c1", "sb1")

    assert (await _deliver(sandbox_path=""))["delivered"] is False
    assert (await _deliver(image_bytes=b""))["delivered"] is False
    assert clients == []


@pytest.mark.asyncio
async def test_delivery_failure_is_reported_not_raised(fake_cache, routed_clients):
    # A delivery error must never break image generation, and must never be
    # silent either: the model has to know the file is not in the sandbox.
    clients, state = routed_clients
    state["fail"] = RuntimeError("runner unreachable")
    await _bind("c1", "sb1")

    outcome = await _deliver()

    assert outcome["delivered"] is False
    assert outcome["sandbox_id"] == "sb1"
    assert "runner unreachable" in outcome["error"]
    assert clients[-1].closed


@pytest.mark.asyncio
async def test_binding_key_scopes_the_lookup_when_conversation_is_absent(fake_cache, routed_clients):
    """A task-worker run has no conversation; its sandbox is bound under the task key."""

    clients, _ = routed_clients
    await _bind("U01ABC", "sb_task")

    assert (await _deliver(binding_key="U01ABC"))["sandbox_id"] == "sb_task"
    assert (await _deliver(binding_key="c-other"))["delivered"] is False
    assert [client.sandbox_id for client in clients] == ["sb_task"]


@pytest.mark.asyncio
async def test_save_keeps_the_real_conversation_and_reports_the_delivery(monkeypatch):
    """Only the sandbox lookup takes the key; persistence keeps the real conversation.

    The binding key is a sandbox scope, not an identity: a document row or a
    workspace binding stamped with it would record a conversation that does
    not exist. The delivery outcome reaches the tool result.
    """

    from packages.core.ai.tools import extended_tools

    delivered: dict = {}
    document_origin: dict = {}

    async def fake_deliver(*, entity_id, user_id, agent_id, sandbox_binding_key, sandbox_path, image_bytes):
        delivered.update(
            {
                "entity_id": entity_id,
                "agent_id": agent_id,
                "sandbox_binding_key": sandbox_binding_key,
                "sandbox_path": sandbox_path,
            }
        )
        return {"path": sandbox_path, "delivered": False, "error": "runner unreachable"}

    async def fake_bill(**_kwargs):
        return None

    def fake_write(_entity_id, rel_path, _data, **_kwargs):
        document_origin["rel_path"] = rel_path
        return "/tmp/" + rel_path

    monkeypatch.setattr(extended_tools, "_deliver_image_to_sandbox", fake_deliver)
    monkeypatch.setattr(extended_tools, "_bill_media", fake_bill)
    monkeypatch.setattr(
        "packages.core.ai.runtime.file_actions.runtime_write_entity_file_atomic",
        fake_write,
    )

    image_url = await extended_tools._save_generated_image_bytes(
        entity_id="ent1",
        user_id="user1",
        prompt="a chart",
        model="gpt-image-1",
        size="1024x1024",
        image_bytes=b"\x89PNG",
        mime="image/png",
        is_byok=False,
        agent_id="agent-p",
        conversation_id=None,
        sandbox_binding_key="U01ABC",
        save_to_knowledge=False,
        sandbox_path="images/page_01.png",
    )

    assert image_url.startswith("/api/v1/fs/ent1/")
    # The sandbox lookup gets the key and the caller's agent.
    assert delivered == {
        "entity_id": "ent1",
        "agent_id": "agent-p",
        "sandbox_binding_key": "U01ABC",
        "sandbox_path": "images/page_01.png",
    }
    payload = await extended_tools._generated_image_result_payload(
        image_url=image_url,
        prompt="a chart",
        size="1024x1024",
        model="gpt-image-1",
        entity_id="ent1",
        workspace_id=None,
        workspace_asset_key="",
        saved_to_knowledge=False,
        include_fs_path=False,
    )
    assert payload["sandbox_delivery"] == {
        "path": "images/page_01.png",
        "delivered": False,
        "error": "runner unreachable",
    }


@pytest.mark.asyncio
async def test_writes_through_the_view_of_the_skill_the_agent_drives(fake_cache, routed_clients):
    """Skills coexist in the user's sandbox: the image goes into THIS agent's
    Skill, not whichever Skill another agent loaded last."""

    clients, _ = routed_clients
    await _bind("c1", "sb_shared", skill_key="pptx")
    await _bind("c1", "sb_shared", agent_id="agent-b", skill_id="skill_docx", skill_key="docx")

    assert (await _deliver())["sandbox_id"] == "sb_shared"
    assert clients[-1].views == [{"skill": "pptx"}]
