"""Blueprint synchronization preserves shared Knowledge and exact live routes."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from test_blueprint_installer_workflows import _base_payload
from test_blueprint_upgrade_and_revert import _workspace
from packages.core.blueprints.installer import install_blueprint, InstallMode
from packages.core.blueprints.upgrade import BlueprintUpgradePlanChangedError, apply, revert
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig, MessageLog
from packages.core.models.document import Channel, Document, DocumentGroup, DocumentGroupMember
from packages.core.models.workspace import Agent, Workspace
from packages.core.services.workspace_readiness import evaluate_workspace_blocking_setup
from packages.core.services.document_service import add_document_to_group

pytestmark = pytest.mark.asyncio


async def _created_knowledge(db_session, *, multiple_packs=False):
    entity_id, user_id = generate_ulid(), generate_ulid()
    payload = _base_payload(**{
        "embedded.knowledge_packs": [{
            "slug": "guide", "title": "Guide", "mode": "inline_text",
            "starter_documents": [{
                "key": "shared-guide", "path": "guide.md", "body_md": "Shared instructions",
            }],
        }],
    })
    if multiple_packs:
        payload["embedded"]["knowledge_packs"].append({
            **copy.deepcopy(payload["embedded"]["knowledge_packs"][0]),
            "slug": "second-guide", "title": "Second guide",
        })
    workspace = await _workspace(db_session, entity_id, installed_from=payload)
    groups = [DocumentGroup(
        entity_id=entity_id, workspace_id=workspace.id, name=pack["title"],
        settings={"mode": "inline_text", "installed_from_blueprint_slug": pack["slug"]},
    ) for pack in payload["embedded"]["knowledge_packs"]]
    db_session.add_all(groups)
    group = groups[0]
    await db_session.flush()
    result = await apply(db_session, workspace=workspace, payload=payload, by_user_id=user_id)
    assert result["can_revert"], result
    document = (await db_session.execute(select(Document).join(
        DocumentGroupMember, DocumentGroupMember.document_id == Document.id,
    ).where(DocumentGroupMember.group_id == group.id))).scalar_one()
    document_id = document.id
    return workspace, document_id, group, user_id


@pytest.mark.parametrize("share_scope", ["workspace", "entity", "same_workspace"])
async def test_revert_preserves_new_document_shared_after_upgrade(db_session, share_scope):
    workspace, document_id, group, user_id = await _created_knowledge(db_session)
    entity_id = workspace.entity_id
    document = await db_session.get(Document, document_id)
    other_workspace = Workspace(entity_id=entity_id, name="Another active Workspace")
    db_session.add(other_workspace)
    await db_session.flush()
    other_group = DocumentGroup(
        entity_id=entity_id,
        workspace_id=(
            other_workspace.id if share_scope == "workspace"
            else workspace.id if share_scope == "same_workspace" else None
        ),
        name="Shared Knowledge",
    )
    db_session.add(other_group)
    await db_session.flush()
    # Sharing changes memberships, not the document's guarded scalar fields.
    assert await add_document_to_group(
        db_session, document.id, other_group.id, entity_id=entity_id,
    )
    await db_session.flush()
    assert document.owner_id == user_id
    await revert(
        db_session, workspace=workspace, by_user_id=user_id,
        actor=SimpleNamespace(id=user_id, entity_id=entity_id, role="member"),
    )
    await db_session.flush()
    remaining = (await db_session.execute(select(Document.id).where(
        Document.id == document_id,
    ))).scalar_one_or_none()
    assert remaining == document_id, "Reverting A deleted Knowledge now used by Workspace B"
    assert (await db_session.execute(select(DocumentGroupMember.group_id).where(
        DocumentGroupMember.document_id == document_id,
    ))).scalars().all() == [other_group.id]


@pytest.mark.parametrize("multiple_packs", [False, True])
async def test_revert_removes_only_unshared_upgrade_document(db_session, multiple_packs):
    workspace, document_id, _group, user_id = await _created_knowledge(
        db_session, multiple_packs=multiple_packs,
    )
    if multiple_packs:
        assert len((await db_session.execute(select(DocumentGroupMember.group_id).where(
            DocumentGroupMember.document_id == document_id,
        ))).scalars().all()) == 2
    await revert(db_session, workspace=workspace, by_user_id=user_id)
    assert await db_session.get(Document, document_id) is None
    assert not (await db_session.execute(select(DocumentGroupMember.group_id).where(
        DocumentGroupMember.document_id == document_id,
    ))).scalars().all()


async def test_legacy_knowledge_receipt_without_membership_identity_fails_closed(db_session):
    workspace, document_id, _group, user_id = await _created_knowledge(db_session)
    settings = copy.deepcopy(workspace.settings)
    for entry in settings["_blueprint"]["restore_point"]["items"]:
        entry.pop("remove_group_membership_on_revert", None)
        entry.pop("created_group_ids", None)
    workspace.settings = settings
    await db_session.flush()
    with pytest.raises(BlueprintUpgradePlanChangedError):
        await revert(db_session, workspace=workspace, by_user_id=user_id)
    assert await db_session.get(Document, document_id) is not None


@pytest.mark.parametrize("share_first", [False, True])
async def test_share_and_revert_serialize_without_orphaning_memberships(db_session, share_first):
    workspace, document_id, _group, user_id = await _created_knowledge(db_session)
    other_group = DocumentGroup(entity_id=workspace.entity_id, name="Concurrent sharing")
    db_session.add(other_group)
    await db_session.commit()
    engine = db_session.bind
    attempted_lock = asyncio.Event()

    def observe_lock(_conn, _cursor, statement, _parameters, _context, _many):
        if "FROM documents" in statement and "FOR UPDATE" in statement:
            attempted_lock.set()

    async with AsyncSession(engine, expire_on_commit=False) as contender:
        async def share(session):
            added = await add_document_to_group(
                session, document_id, other_group.id, entity_id=workspace.entity_id,
            )
            await session.commit()
            return added

        async def undo(session):
            await revert(session, workspace=workspace, by_user_id=user_id)
            await session.commit()

        if share_first:
            assert await add_document_to_group(
                db_session, document_id, other_group.id, entity_id=workspace.entity_id,
            )
        else:
            await revert(db_session, workspace=workspace, by_user_id=user_id)
        event.listen(engine.sync_engine, "before_cursor_execute", observe_lock)
        pending = asyncio.create_task(undo(contender) if share_first else share(contender))
        try:
            await asyncio.wait_for(attempted_lock.wait(), timeout=10)
            assert not pending.done()
            await db_session.commit()
            result = await asyncio.wait_for(pending, timeout=10)
            if not share_first:
                assert result is False
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", observe_lock)
            await db_session.rollback()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert bool((await db_session.execute(select(Document.id).where(
        Document.id == document_id,
    ))).scalar_one_or_none()) is share_first
    memberships = (await db_session.execute(select(DocumentGroupMember.group_id).where(
        DocumentGroupMember.document_id == document_id,
    ))).scalars().all()
    assert memberships == ([other_group.id] if share_first else [])


async def _installed_channels(db_session, *, identical=False):
    entity_id, user_id = generate_ulid(), generate_ulid()
    services = ["support", "sales"]
    payload = _base_payload(**{
        "contract.channels": [{
            "channel_type": "telegram", "provider": "telegram_bot", "required": True,
            "role": "support" if identical else service,
            "linked_service_key": "support" if identical else service,
            "purpose": "support" if identical else service,
        } for service in services],
        "embedded.agents": [{
            "slug": service, "name": service.title(), "system_prompt": f"Handle {service}.",
        } for service in services],
        "recipe.subscriptions": [{
            "service_key": service, "agent_slug": service,
        } for service in services],
    })
    accounts = [ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type="telegram",
        provider="telegram_bot", name=service, status="active",
    ) for service in services]
    db_session.add_all(accounts)
    await db_session.flush()
    selected = {f"channel:{i}:telegram": account.id for i, account in enumerate(accounts)}
    installed = await install_blueprint(
        db_session, entity_id=entity_id, payload=payload, user_id=user_id,
        blueprint_slug="review-channels", mode=InstallMode.LIVE,
        channel_config_ids=selected,
    )
    workspace = await db_session.get(Workspace, installed.workspace_id)
    return workspace, accounts, payload, selected, user_id


@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("disabled", ["account", "binding"])
async def test_each_required_channel_must_remain_live(db_session, identical, disabled):
    workspace, accounts, *_ = await _installed_channels(db_session, identical=identical)
    before = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert before is not None and before.status == "ready", before
    target = accounts[1]
    if disabled == "binding":
        target = (await db_session.execute(select(Channel).where(
            Channel.workspace_id == workspace.id,
            Channel.config["channel_config_id"].astext == accounts[1].id,
        ))).scalar_one()
    target.status = "inactive"
    await db_session.flush()
    after = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert after is not None and after.blocks_work, after
    assert len(after.details["incomplete_checks"]) == 1
    target.status = "active"
    await db_session.flush()
    restored = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert restored is not None and not restored.blocks_work


async def test_legacy_duplicate_channel_declarations_cannot_share_one_live_account(db_session):
    workspace, accounts, *_ = await _installed_channels(db_session, identical=True)
    settings = copy.deepcopy(workspace.settings)
    for todo in settings["_blueprint"]["live_setup_requirements"]:
        if todo["kind"] == "channel":
            for key in ("blueprint_requirement_key", "channel_config_id", "channel_binding_id"):
                todo["payload"].pop(key, None)
    workspace.settings = settings
    accounts[1].status = "inactive"
    await db_session.flush()
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is not None and readiness.blocks_work


async def test_revert_restores_upgraded_channel_routing(db_session):
    workspace, accounts, payload, selected, user_id = await _installed_channels(db_session)
    accounts[0].credentials = {"test_credential": "keep"}
    history = MessageLog(
        entity_id=workspace.entity_id, channel_config_id=accounts[0].id,
        channel_type="telegram", direction="inbound", content="Existing history",
    )
    db_session.add(history)
    await db_session.flush()
    binding = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
        Channel.config["channel_config_id"].astext == accounts[0].id,
    ))).scalar_one()
    before = copy.deepcopy(binding.config)
    before_subscription_id = binding.agent_subscription_id
    upgraded = copy.deepcopy(payload)
    upgraded["embedded"]["agents"][0]["system_prompt"] = "Updated support instructions."
    upgraded["contract"]["channels"][0]["purpose"] = "new purpose"
    upgraded["contract"]["channels"][0]["linked_service_key"] = "sales"
    result = await apply(
        db_session, workspace=workspace, payload=upgraded,
        channel_config_ids=selected, by_user_id=user_id,
    )
    assert result["fully_synchronized"] and result["can_revert"], result
    await db_session.refresh(binding)
    assert binding.config["linked_service_key"] == "sales"
    assert binding.agent_subscription_id != before_subscription_id
    await revert(db_session, workspace=workspace, by_user_id=user_id)
    await db_session.refresh(binding)
    assert (binding.agent_subscription_id, binding.config) == (before_subscription_id, before), (
        "Revert restored the Blueprint but retained its new live channel routing"
    )
    assert accounts[0].credentials == {"test_credential": "keep"}
    assert await db_session.get(MessageLog, history.id) is history


@pytest.mark.parametrize("changed", ["binding", "setup"])
async def test_channel_conflict_is_detected_before_restoring_any_agent(db_session, changed):
    workspace, accounts, payload, selected, user_id = await _installed_channels(db_session)
    upgraded = copy.deepcopy(payload)
    upgraded["embedded"]["agents"][0]["system_prompt"] = "Upgraded support"
    for requirement in upgraded["contract"]["channels"]:
        requirement["purpose"] = "Updated purpose"
    await apply(
        db_session, workspace=workspace, payload=upgraded,
        channel_config_ids=selected, by_user_id=user_id,
    )
    bindings = (await db_session.execute(select(Channel).where(
        Channel.workspace_id == workspace.id,
    ).order_by(Channel.id))).scalars().all()
    if changed == "binding":
        bindings[1].config = {**bindings[1].config, "language": "fr"}
    else:
        settings = copy.deepcopy(workspace.settings)
        settings["_blueprint"]["live_setup_requirements"].append({
            "kind": "channel", "payload": {"channel_type": "slack"}, "blocking": True,
        })
        workspace.settings = settings
    await db_session.flush()
    before = copy.deepcopy(workspace.settings)
    before_bindings = [copy.deepcopy(binding.config) for binding in bindings]
    with pytest.raises(BlueprintUpgradePlanChangedError):
        await revert(db_session, workspace=workspace, by_user_id=user_id)
    assert workspace.settings == before
    assert [binding.config for binding in bindings] == before_bindings
    assert (await db_session.execute(select(Agent.system_prompt).where(
        Agent.workspace_id == workspace.id, Agent.slug == "support",
    ))).scalar_one() == "Upgraded support"
