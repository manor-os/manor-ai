"""Unit tests for the installer's recipe.workflows + post_install_checks.

Uses postgres via the conftest ``db_session`` fixture (see
test_blueprint_exporter_embedded.py for why we moved off in-memory
sqlite — shared MetaData mutation broke downstream tests in the same
pytest process).

Exercises:
  * blueprint workflow DSL (kind/depends_on) → canonical WorkflowDefinition
    graph (type/next + explicit trigger) — dependency graph inversion
  * variables list → dict translation
  * fresh installs isolate local Flow ids by Workspace install scope
  * post_install_checks: session_alive / agent_callable / cron_scheduled /
    workflow_present each emit blocking todos on failure and stay silent
    on success
  * unknown check kind emits non-blocking note
"""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.installer import (
    InstallError,
    InstallMode,
    _install_workflow_binding,
    install_blueprint,
)
from packages.core.constants.blueprints import installed_blueprint_job_id
from packages.core.blueprints.exporter import export_workspace
from packages.core.blueprints.setup_preflight import BlueprintSetupPreflightFactory
from packages.core.models.base import generate_ulid
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.integration_session import IntegrationSession
from packages.core.models.marketplace_resource_link import MarketplaceResourceLink
from packages.core.models.permission import Visibility
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import (
    WorkflowBinding,
    WorkflowDefinition,
    WorkflowTemplateInstallation,
)
from packages.core.models.workspace import Agent, AgentSubscription, Workspace
from packages.core.services.workflow_service import (
    start_workflow_from_binding,
    validate_workflow_steps,
)
from packages.core.services.workspace_readiness import (
    evaluate_workspace_blocking_setup,
)


@pytest.fixture
def entity_id() -> str:
    return generate_ulid()


def _base_payload(**overrides) -> dict:
    payload = {
        "manifest": {"blueprint_version": "1.1", "title": "T", "kind": "k"},
        "contract": {
            "variables": [],
            "channels": [],
            "sessions": [],
            "requires": {"manor_min_version": None, "tools": [], "mcp_servers": [], "skills": [], "agents": []},
        },
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {
            "operating_model": {},
            "strategist": None,
            "prompts": [],
            "subscriptions": [],
            "scheduled_jobs": [],
            "workflows": [],
            "goals": [],
            "task_categories": [],
            "custom_fields": [],
            "sla_policies": [],
            "escalation_rules": [],
        },
        "policy": {
            "governance": {},
            "post_install_checks": [],
            "expected_baseline": None,
        },
    }
    for k, v in overrides.items():
        path = k.split(".")
        cur = payload
        for s in path[:-1]:
            cur = cur.setdefault(s, {})
        cur[path[-1]] = v
    return payload


async def test_install_preflight_resolves_creator_declared_account_requirements(
    db_session: AsyncSession,
    entity_id: str,
    monkeypatch: pytest.MonkeyPatch,
):
    from packages.core.blueprints import setup_preflight
    from packages.core.services.integration_resolution import (
        IntegrationProviderReadiness,
    )

    async def fake_integration_readiness(*_args, provider_keys, **_kwargs):
        return {
            provider: IntegrationProviderReadiness(
                provider=provider,
                ready=provider == "notion",
                reason=("Connected" if provider == "notion" else "Reconnect required"),
                scope="user",
            )
            for provider in provider_keys
        }

    monkeypatch.setattr(
        setup_preflight,
        "integration_provider_readiness",
        fake_integration_readiness,
    )
    db_session.add(IntegrationSession(
        entity_id=entity_id,
        provider="linkedin",
        label="publisher",
        status="active",
    ))
    await db_session.flush()
    payload = _base_payload(**{
        "contract.requires.mcp_servers": [
            {"slug": "notion", "required": True},
            {"slug": "slack", "required": True},
        ],
        "contract.sessions": [{
            "provider": "linkedin",
            "label": "publisher",
            "required": True,
        }, {
            "provider": "instagram",
            "label": "brand",
            "required": False,
        }],
    })

    result = await BlueprintSetupPreflightFactory.from_payload(
        db_session,
        payload=payload,
        entity_id=entity_id,
        user_id=generate_ulid(),
    )

    assert result.ready is False
    assert [item.provider for item in result.blocking_requirements] == ["slack"]
    states = {
        (item.kind.value, item.provider): item.ready
        for item in result.requirements
    }
    assert states == {
        ("integration", "notion"): True,
        ("integration", "slack"): False,
        ("browser_session", "linkedin"): True,
        ("browser_session", "instagram"): False,
    }


@pytest.mark.parametrize("server_key", ["custom-service", "custom-MCP-service"])
async def test_install_preflight_preserves_exact_catalog_keys(
    monkeypatch: pytest.MonkeyPatch,
    server_key: str,
):
    from packages.core.blueprints import setup_preflight
    from packages.core.services.integration_resolution import IntegrationProviderReadiness
    from packages.core.services.provider_keys import canonical_provider_key

    provider = canonical_provider_key(server_key)

    async def fake_integration_readiness(*_args, provider_keys, **_kwargs):
        assert provider_keys == [server_key]
        return {
            provider: IntegrationProviderReadiness(
                provider=provider,
                ready=True,
                reason="Integration requires no account connection.",
                scope="internal",
            ),
        }

    monkeypatch.setattr(
        setup_preflight,
        "integration_provider_readiness",
        fake_integration_readiness,
    )
    result = await BlueprintSetupPreflightFactory.from_payload(
        None,
        payload=_base_payload(**{
            "contract.requires.mcp_servers": [{"slug": server_key, "required": True}],
        }),
        entity_id=generate_ulid(),
        user_id=generate_ulid(),
    )

    assert result.ready is True
    assert result.requirements[0].provider == provider


@pytest.mark.parametrize("channel_type", ["slack", "telegram", "discord", "twilio_voice", "whatsapp"])
@pytest.mark.parametrize("binding_status", ["active", "disabled"])
async def test_channel_preflight_excludes_accounts_bound_to_another_workspace(
    db_session: AsyncSession,
    entity_id: str,
    channel_type: str,
    binding_status: str,
):
    user_id, workspace_id = generate_ulid(), generate_ulid()
    account = ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type=channel_type,
        provider=channel_type, name="Channel account", status="active", config={}, credentials={},
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(Channel(
        entity_id=entity_id, workspace_id=workspace_id, type=channel_type, name="Existing route",
        status=binding_status, config={"channel_config_id": account.id},
    ))
    await db_session.flush()
    payload = _base_payload(**{"contract.channels": [{"channel_type": channel_type, "required": True}]})
    for selected in (None, {f"channel:0:{channel_type}": account.id}):
        preflight = await BlueprintSetupPreflightFactory.from_payload(
            db_session, payload=payload, entity_id=entity_id, user_id=user_id,
            selected_channel_config_ids=selected,
        )
        [requirement] = preflight.requirements
        assert preflight.ready is (binding_status != "active")
        assert [option.id for option in requirement.resource_options] == (
            [] if binding_status == "active" else [account.id]
        )
    if binding_status == "active":
        existing_workspace = await BlueprintSetupPreflightFactory.from_payload(
            db_session, payload=payload, entity_id=entity_id, user_id=user_id,
            workspace_id=workspace_id,
        )
        assert existing_workspace.ready
        assert existing_workspace.requirements[0].resource_id == account.id


async def test_workspace_channel_preflight_requires_matching_provider(
    db_session: AsyncSession,
    entity_id: str,
):
    user_id, workspace_id = generate_ulid(), generate_ulid()
    wrong_provider = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=user_id,
        channel_type="telegram",
        provider="telegram_personal",
        name="Personal bot",
        status="active",
        config={},
        credentials={},
    )
    right_provider = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=user_id,
        channel_type="telegram",
        provider="telegram_bot",
        name="Workspace bot",
        status="active",
        config={},
        credentials={},
    )
    db_session.add_all([wrong_provider, right_provider])
    await db_session.flush()
    db_session.add(Channel(
        entity_id=entity_id,
        workspace_id=workspace_id,
        type="telegram",
        name="Wrong provider route",
        status="active",
        config={"channel_config_id": wrong_provider.id},
    ))
    await db_session.flush()
    payload = _base_payload(**{
        "contract.channels": [{
            "channel_type": "telegram",
            "provider": "telegram_bot",
            "required": True,
        }],
    })

    preflight = await BlueprintSetupPreflightFactory.from_payload(
        db_session,
        payload=payload,
        entity_id=entity_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )

    [requirement] = preflight.requirements
    assert preflight.ready is True
    assert requirement.resource_id == right_provider.id
    assert {option.id for option in requirement.resource_options} == {
        right_provider.id,
    }


async def test_existing_shared_channel_stays_available_only_to_its_bound_workspaces(
    db_session: AsyncSession,
    entity_id: str,
):
    user_id = generate_ulid()
    account = ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type="telegram",
        provider="telegram_bot", name="Explicitly shared bot", status="active",
        config={}, credentials={},
    )
    db_session.add(account)
    await db_session.flush()
    workspace_ids = [generate_ulid(), generate_ulid()]
    db_session.add_all([
        Channel(
            entity_id=entity_id, workspace_id=workspace_id, user_id=user_id,
            type="telegram", status="active", config={"channel_config_id": account.id},
        )
        for workspace_id in workspace_ids
    ])
    await db_session.flush()
    payload = _base_payload(**{
        "contract.channels": [{"channel_type": "telegram", "required": True}],
    })
    for workspace_id in workspace_ids:
        preflight = await BlueprintSetupPreflightFactory.from_payload(
            db_session, payload=payload, entity_id=entity_id,
            user_id=user_id, workspace_id=workspace_id,
        )
        assert preflight.ready
        assert preflight.requirements[0].resource_id == account.id
    for workspace_id in (None, generate_ulid()):
        preflight = await BlueprintSetupPreflightFactory.from_payload(
            db_session, payload=payload, entity_id=entity_id,
            user_id=user_id, workspace_id=workspace_id,
            selected_channel_config_ids={"channel:0:telegram": account.id},
        )
        assert not preflight.ready
        assert preflight.requirements[0].resource_options == ()


async def test_install_preflight_selects_and_binds_required_channel_account(
    db_session: AsyncSession,
    entity_id: str,
):
    user_id = generate_ulid()
    first = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=user_id,
        workspace_id=None,
        channel_type="telegram",
        provider="telegram_bot",
        name="Primary bot",
        config={},
        credentials={},
        status="active",
    )
    second = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=user_id,
        workspace_id=None,
        channel_type="telegram",
        provider="telegram_bot",
        name="Backup bot",
        config={},
        credentials={},
        status="active",
    )
    other_user = ChannelConfig(
        entity_id=entity_id,
        owner_user_id=generate_ulid(),
        workspace_id=None,
        channel_type="telegram",
        provider="telegram_bot",
        name="Another user's bot",
        config={},
        credentials={},
        status="active",
    )
    db_session.add_all([first, second, other_user])
    await db_session.flush()
    payload = _base_payload(**{
        "contract.channels": [{
            "channel_type": "telegram",
            "purpose": "Publish alerts",
            "required": True,
            "linked_service_key": "publisher",
        }],
        "embedded.agents": [{
            "slug": "publisher", "name": "Publisher",
            "system_prompt": "Publish channel alerts.",
        }],
        "recipe.subscriptions": [{
            "service_key": "publisher", "agent_slug": "publisher",
        }],
    })

    unresolved = await BlueprintSetupPreflightFactory.from_payload(
        db_session,
        payload=payload,
        entity_id=entity_id,
        user_id=user_id,
    )

    [channel_requirement] = [
        item for item in unresolved.requirements if item.kind.value == "channel"
    ]
    assert unresolved.ready is False
    assert channel_requirement.requirement_key == "channel:0:telegram"
    assert {option.id for option in channel_requirement.resource_options} == {
        first.id,
        second.id,
    }

    inaccessible = await BlueprintSetupPreflightFactory.from_payload(
        db_session,
        payload=payload,
        entity_id=entity_id,
        user_id=user_id,
        selected_channel_config_ids={"channel:0:telegram": other_user.id},
    )
    assert inaccessible.ready is False

    selected = await BlueprintSetupPreflightFactory.from_payload(
        db_session,
        payload=payload,
        entity_id=entity_id,
        user_id=user_id,
        selected_channel_config_ids={"channel:0:telegram": second.id},
    )
    assert selected.ready is True

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        user_id=user_id,
        channel_config_ids={"channel:0:telegram": second.id},
    )

    binding = (await db_session.execute(
        select(Channel).where(Channel.workspace_id == result.workspace_id)
    )).scalar_one()
    assert binding.config["channel_config_id"] == second.id
    assert not any(todo.kind == "channel" for todo in result.todos)

    from apps.api.routers.blueprints import _require_install_preflight

    variable_payload = _base_payload(**{
        "contract.variables": [{
            "key": "channel_type",
            "required": True,
        }],
        "contract.channels": [{
            "channel_type": "{{channel_type}}",
            "required": True,
        }],
    })
    variable_preflight = await _require_install_preflight(
        db_session,
        payload=variable_payload,
        entity_id=entity_id,
        user_id=user_id,
        selected_channel_config_ids={"channel:0:telegram": first.id},
        variable_values={"channel_type": "telegram"},
    )
    assert variable_preflight.ready is True


@pytest.mark.parametrize("channel_type", ["slack", "telegram", "discord", "twilio_voice", "whatsapp"])
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_installed_channel_preserves_owner_and_runtime_binding(
    db_session: AsyncSession,
    entity_id: str,
    channel_type: str,
    mode: InstallMode,
):
    from packages.core.blueprints.installer import _bind_blueprint_channel_configs
    from packages.core.services.channel_bindings import (
        load_channel_binding_by_id_for_config,
        resolve_unique_channel_binding_scope,
    )

    user_id = generate_ulid()
    account = ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type=channel_type,
        provider=channel_type, name="Channel account", status="active", config={}, credentials={},
    )
    db_session.add(account)
    await db_session.flush()
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        user_id=user_id,
        mode=mode,
        channel_config_ids={f"channel:0:{channel_type}": account.id},
        payload=_base_payload(**{
            "embedded.agents": [{
                "slug": "channel-agent", "name": "Channel Agent",
                "system_prompt": "Handle channel messages.",
            }],
            "contract.channels": [{
                "channel_type": channel_type, "required": True,
                "linked_service_key": "channel-service",
            }],
            "recipe.subscriptions": [{
                "service_key": "channel-service", "agent_slug": "channel-agent",
            }],
        }),
    )
    binding = (await db_session.execute(
        select(Channel).where(Channel.workspace_id == result.workspace_id)
    )).scalar_one()
    assert binding.user_id == account.owner_user_id == user_id
    assert await load_channel_binding_by_id_for_config(db_session, account, binding.id) is binding
    scope = await resolve_unique_channel_binding_scope(db_session, account)
    assert scope is not None
    assert scope.workspace_id == result.workspace_id
    assert scope.agent_id == binding.agent_id
    assert scope.agent_subscription_id == result.subscription_ids[0]
    assert not any(todo.kind == "channel" for todo in result.todos)

    # Updating a legacy binding must also restore its owner without adding
    # a second route for the same account.
    binding.user_id = None
    await db_session.flush()
    workspace = await db_session.get(Workspace, result.workspace_id)
    await _bind_blueprint_channel_configs(
        db_session, workspace=workspace, user_id=user_id,
        channel_requirements=[{
            "channel_type": channel_type, "required": True,
            "linked_service_key": "channel-service",
        }],
        selected_channel_config_ids={f"channel:0:{channel_type}": account.id},
    )
    assert binding.user_id == user_id
    assert await load_channel_binding_by_id_for_config(db_session, account, binding.id) is binding
    assert (await db_session.execute(
        select(Channel.id).where(Channel.workspace_id == result.workspace_id)
    )).scalars().all() == [binding.id]


@pytest.mark.parametrize("channel_type", ["slack", "telegram", "discord", "twilio_voice", "whatsapp"])
@pytest.mark.parametrize("mode", [InstallMode.SIMULATE, InstallMode.LIVE])
async def test_install_rechecks_channel_binding_before_creating_workspace(
    db_session: AsyncSession,
    entity_id: str,
    channel_type: str,
    mode: InstallMode,
):
    from packages.core.services.channel_bindings import load_channel_binding_for_config

    user_id = generate_ulid()
    account = ChannelConfig(
        entity_id=entity_id, owner_user_id=user_id, channel_type=channel_type,
        provider=channel_type, name="Existing account", status="active", config={}, credentials={},
    )
    db_session.add(account)
    await db_session.flush()
    payload = _base_payload(**{
        "contract.channels": [{"channel_type": channel_type, "required": True}],
    })
    preflight = await BlueprintSetupPreflightFactory.from_payload(
        db_session, payload=payload, entity_id=entity_id, user_id=user_id,
    )
    assert preflight.ready
    assert preflight.requirements[0].resource_id == account.id
    existing_binding = Channel(
        entity_id=entity_id, workspace_id=generate_ulid(), user_id=user_id,
        type=channel_type, name="Existing route", status="active",
        config={"channel_config_id": account.id},
    )
    db_session.add(existing_binding)
    await db_session.flush()

    with pytest.raises(InstallError, match="channel account is unavailable"):
        await install_blueprint(
            db_session, entity_id=entity_id, user_id=user_id, payload=payload,
            mode=mode, channel_config_ids={f"channel:0:{channel_type}": account.id},
        )

    assert (await db_session.execute(
        select(Workspace.id).where(Workspace.entity_id == entity_id)
    )).scalars().all() == []
    assert await load_channel_binding_for_config(db_session, account) is existing_binding
    assert (await db_session.execute(
        select(Channel.id).where(Channel.entity_id == entity_id)
    )).scalars().all() == [existing_binding.id]


async def test_install_variables_are_validated_and_materialized(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "contract.variables": [
                {
                    "key": "business_name",
                    "label": "Business name",
                    "required": True,
                    "materialize": True,
                },
                {"key": "daily_limit", "default": 3},
                {"key": "notifications_enabled", "default": False},
                {"key": "channels", "default": ["email", "chat"]},
                {"key": "limits", "default": {"warning": 2}},
                {"key": "channel_type", "default": "telegram"},
            ],
            "contract.channels": [{
                "channel_type": "{{channel_type}}",
                "required": True,
            }],
            "recipe.operating_model": {
                "context": "Operate {{business_name}}.",
                "settings": {
                    "daily_limit": "{{daily_limit}}",
                    "notifications_enabled": "{{notifications_enabled}}",
                    "channels": "{{channels}}",
                    "limits": "{{limits}}",
                },
            },
        }
    )

    from apps.api.routers.blueprints import _payload_setup_preview

    defaults = {
        item.key: item.default
        for item in _payload_setup_preview(payload).optional_variables
    }
    assert defaults == {
        "daily_limit": 3,
        "notifications_enabled": False,
        "channels": ["email", "chat"],
        "limits": {"warning": 2},
        "channel_type": "telegram",
    }

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        variable_values={"business_name": "Acme", "channel_type": "slack"},
    )
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    assert workspace.operating_context == "Operate Acme."
    assert workspace.settings["daily_limit"] == 3
    assert workspace.settings["notifications_enabled"] is False
    assert workspace.settings["channels"] == ["email", "chat"]
    assert workspace.settings["limits"] == {"warning": 2}
    assert workspace.settings["blueprint_personalization"] == {
        "business_name": "Acme",
    }
    assert workspace.settings["_blueprint"]["channel_requirements"] == [{
        "channel_type": "slack",
        "required": True,
    }]
    [channel_todo] = [todo for todo in result.todos if todo.kind == "channel"]
    assert channel_todo.payload["channel_type"] == "slack"
    assert "Acme" not in str(workspace.settings["_blueprint"])
    reexported = await export_workspace(db_session, result.workspace_id, title="Copy")
    assert reexported["contract"]["variables"] == []
    assert "blueprint_personalization" not in (
        reexported["recipe"]["operating_model"].get("settings") or {}
    )
    reexported_settings = reexported["recipe"]["operating_model"]["settings"]
    assert reexported_settings["daily_limit"] == 3
    assert reexported_settings["notifications_enabled"] is False
    assert reexported_settings["channels"] == ["email", "chat"]
    assert reexported_settings["limits"] == {"warning": 2}


async def test_missing_required_variable_and_newer_runtime_fail_before_mutation(
    db_session: AsyncSession,
    entity_id: str,
):
    missing = _base_payload(**{
        "contract.variables": [{"key": "business_name", "required": True}],
    })
    with pytest.raises(InstallError, match="required Blueprint install variable"):
        await install_blueprint(db_session, entity_id=entity_id, payload=missing)
    with pytest.raises(InstallError, match="required Blueprint install variable"):
        await install_blueprint(
            db_session,
            entity_id=entity_id,
            payload=missing,
            variable_values={"business_name": "   "},
        )

    typed = _base_payload(**{
        "contract.variables": [{"key": "enabled", "default": False}],
        "recipe.operating_model": {
            "settings": {"enabled": "{{enabled}}"},
        },
    })
    with pytest.raises(
        InstallError,
        match="Blueprint install variable 'enabled' must be boolean; got string",
    ):
        await install_blueprint(
            db_session,
            entity_id=entity_id,
            payload=typed,
            variable_values={"enabled": "false"},
        )

    too_new = _base_payload(**{
        "contract.requires.manor_min_version": "99.0",
    })
    with pytest.raises(InstallError, match="requires Manor 99.0"):
        await install_blueprint(db_session, entity_id=entity_id, payload=too_new)

    assert not list((await db_session.execute(
        select(Workspace).where(Workspace.entity_id == entity_id)
    )).scalars())


async def test_placeholder_agent_does_not_create_runnable_subscription(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"missing-agent-{entity_id}"
    payload = _base_payload(**{
        "recipe.operating_model": {"services": [{"service_key": "research"}]},
        "recipe.subscriptions": [{
            "service_key": "research",
            "agent_slug": slug,
            "config": {},
        }],
    })

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        create_missing_agents=True,
    )
    assert result.subscription_ids == []
    [todo] = [item for item in result.todos if item.kind == "missing_agent"]
    placeholder = await db_session.get(Agent, todo.payload["placeholder_agent_id"])
    assert placeholder is not None
    assert placeholder.status == "draft"
    assert placeholder.workspace_id == result.workspace_id

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    assert workspace.settings["_blueprint"]["blocking_todo_count"] == 1
    readiness = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert readiness is not None
    assert readiness.blocks_work


async def test_missing_agent_todo_requires_the_exact_callable_agent(
    db_session: AsyncSession,
    entity_id: str,
):
    from packages.core.workers.registry import ensure_internal_worker

    slug = f"required-agent-{entity_id}"
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(**{
            "recipe.operating_model": {
                "services": [{"service_key": "research"}],
            },
            "recipe.subscriptions": [{
                "service_key": "research",
                "agent_slug": slug,
                "config": {},
            }],
        }),
        create_missing_agents=True,
    )
    workspace = await db_session.get(Workspace, result.workspace_id)
    [todo] = [item for item in result.todos if item.kind == "missing_agent"]
    placeholder = await db_session.get(Agent, todo.payload["placeholder_agent_id"])
    assert workspace is not None and placeholder is not None

    wrong_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        name="Wrong agent",
        slug=f"wrong-{slug}",
        visibility=Visibility.WORKSPACE,
        status="active",
    )
    wrong_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        agent_id=wrong_agent.id,
        service_key="research",
        status="active",
    )
    db_session.add_all([wrong_agent, wrong_subscription])
    await db_session.flush()
    await ensure_internal_worker(db_session, entity_id)

    wrong_identity = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert wrong_identity is not None and wrong_identity.blocks_work

    placeholder.status = "active"
    exact_subscription = AgentSubscription(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        agent_id=placeholder.id,
        service_key="research",
        status="active",
    )
    db_session.add(exact_subscription)
    await db_session.flush()
    await ensure_internal_worker(db_session, entity_id)

    exact_identity = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert exact_identity is not None
    assert exact_identity.blocks_work is False


async def test_skeleton_knowledge_is_a_durable_blocker(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(**{
        "embedded.knowledge_packs": [{
            "slug": "customer-context",
            "title": "Customer context",
            "mode": "skeleton",
            "folder_structure": [{"path": "customers/README.md"}],
            "starter_documents": [],
        }],
    })
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    [todo] = [
        item for item in result.todos
        if item.kind == "knowledge_pack_content"
    ]
    assert todo.blocking is True
    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    assert workspace.settings["_blueprint"]["install_todos"][0]["kind"] == (
        "knowledge_pack_content"
    )


async def test_runtime_start_is_deferred_for_blocking_install(
    db_session: AsyncSession,
    entity_id: str,
    monkeypatch,
):
    from apps.api.routers.blueprints import _commit_and_start_installed_workspace
    from packages.core.services import (
        blueprint_startup_service,
        workspace_setup_service,
    )

    payload = _base_payload(**{
        "contract.channels": [{"channel_type": "telegram", "required": True}],
    })
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )
    calls: list[str] = []

    async def post_commit(*_args, **_kwargs):
        calls.append("post_commit")
        return {"strategist_dispatched": False}

    async def reconcile(*_args, **_kwargs):
        calls.append("reconcile")
        return None

    monkeypatch.setattr(
        workspace_setup_service,
        "dispatch_workspace_post_commit",
        post_commit,
    )
    monkeypatch.setattr(
        blueprint_startup_service,
        "reconcile_blueprint_startup",
        reconcile,
    )
    await _commit_and_start_installed_workspace(
        db_session,
        result=result,
        entity_id=entity_id,
    )
    assert calls == ["reconcile"]


async def test_declared_ready_startup_replaces_generic_post_commit_start(
    db_session: AsyncSession,
    entity_id: str,
    monkeypatch,
):
    from apps.api.routers.blueprints import _commit_and_start_installed_workspace
    from packages.core.services import (
        blueprint_startup_service,
        workspace_setup_service,
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(),
        mode=InstallMode.LIVE,
    )
    calls: list[str] = []

    async def post_commit(*_args, **_kwargs):
        calls.append("post_commit")
        return {"strategist_dispatched": True}

    async def reconcile(*_args, **_kwargs):
        calls.append("reconcile")
        return blueprint_startup_service.BlueprintStartupResult(
            state="ready_dispatched",
            dispatched_job_ids=("declared-first-review",),
        )

    monkeypatch.setattr(
        workspace_setup_service,
        "dispatch_workspace_post_commit",
        post_commit,
    )
    monkeypatch.setattr(
        blueprint_startup_service,
        "reconcile_blueprint_startup",
        reconcile,
    )

    await _commit_and_start_installed_workspace(
        db_session,
        result=result,
        entity_id=entity_id,
    )

    assert calls == ["reconcile"]


async def test_blueprint_without_declared_startup_keeps_generic_post_commit_start(
    db_session: AsyncSession,
    entity_id: str,
    monkeypatch,
):
    from apps.api.routers.blueprints import _commit_and_start_installed_workspace
    from packages.core.services import (
        blueprint_startup_service,
        workspace_setup_service,
    )

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(),
        mode=InstallMode.LIVE,
    )
    calls: list[str] = []

    async def post_commit(*_args, **_kwargs):
        calls.append("post_commit")
        return {"strategist_dispatched": True}

    async def reconcile(*_args, **_kwargs):
        calls.append("reconcile")
        return blueprint_startup_service.BlueprintStartupResult(
            state="not_configured",
        )

    monkeypatch.setattr(
        workspace_setup_service,
        "dispatch_workspace_post_commit",
        post_commit,
    )
    monkeypatch.setattr(
        blueprint_startup_service,
        "reconcile_blueprint_startup",
        reconcile,
    )

    await _commit_and_start_installed_workspace(
        db_session,
        result=result,
        entity_id=entity_id,
    )

    assert calls == ["reconcile", "post_commit"]


async def test_simulation_installs_with_missing_required_channel_when_client_sends_empty_selection(
    db_session: AsyncSession,
    entity_id: str,
):
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(**{
            "contract.channels": [{"channel_type": "telegram", "required": True}],
        }),
        mode=InstallMode.SIMULATE,
        channel_config_ids={},
    )

    assert result.workspace_id
    [todo] = [item for item in result.todos if item.kind == "channel"]
    assert todo.blocking is True


async def test_blocking_live_install_keeps_schedules_dormant_and_rejects_flows(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"blocked-flow-{entity_id}"
    payload = _base_payload(**{
        "contract.channels": [{"channel_type": "telegram", "required": True}],
        "recipe.operating_model": {"heartbeat_enabled": True},
        "recipe.workflows": [{
            "slug": slug,
            "trigger_type": "manual",
            "variables": [],
            "steps": [
                {
                    "id": "start",
                    "type": "trigger",
                    "name": "Start",
                    "config": {"trigger_type": "manual"},
                    "next": ["end"],
                },
                {
                    "id": "end",
                    "type": "end",
                    "name": "Done",
                    "config": {},
                    "next": [],
                },
            ],
        }],
    })
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
    )

    derived_jobs = list((await db_session.execute(
        select(ScheduledJob).where(
            ScheduledJob.workspace_id == result.workspace_id,
            ScheduledJob.job_id.startswith("sr:"),
        )
    )).scalars())
    assert derived_jobs, "runtime schedules must exist so they can resume after setup"

    binding = await db_session.get(WorkflowBinding, result.workflow_binding_ids[0])
    assert binding is not None
    with pytest.raises(ValueError, match="setup is incomplete"):
        await start_workflow_from_binding(db_session, binding)


# ── Workflow translation ─────────────────────────────────────────────


async def test_workflow_dependency_inversion(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"morning-post-with-review-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "scheduled",
                    "trigger_ref": "morning-draft",
                    "variables": [
                        {"key": "post_topic", "default": "product_update"},
                    ],
                    "steps": [
                        {"id": "draft", "kind": "agent_call", "service_key": "social.x.poster", "input": "Draft post"},
                        {
                            "id": "review",
                            "kind": "hitl_approval",
                            "depends_on": ["draft"],
                            "channel": "telegram",
                            "timeout_minutes": 60,
                        },
                        {"id": "post", "kind": "tool_call", "depends_on": ["review"], "tool": "tool.x.post"},
                    ],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    wf = (
        await db_session.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.name == slug,
            )
        )
    ).scalar_one()

    assert wf.variables == {"post_topic": "product_update"}
    assert wf.trigger_type == "scheduled"
    assert wf.trigger_config == {"trigger_ref": "morning-draft"}
    validation = validate_workflow_steps(wf.steps)
    assert validation["valid"], validation
    assert validation["entry_step_id"] == "start"
    steps_by_id = {s["id"]: s for s in wf.steps}
    assert steps_by_id["start"]["type"] == "trigger"
    assert steps_by_id["start"]["next"] == ["draft"]
    assert steps_by_id["draft"]["next"] == ["review"]
    assert steps_by_id["review"]["next"] == ["post"]
    assert steps_by_id["post"]["next"] == []
    assert steps_by_id["draft"]["type"] == "agent"
    assert steps_by_id["review"]["type"] == "wait"
    assert steps_by_id["post"]["type"] == "tool"
    assert steps_by_id["draft"]["config"]["service_key"] == "social.x.poster"
    assert steps_by_id["review"]["config"]["wait_type"] == "approval"
    assert steps_by_id["review"]["config"]["timeout_minutes"] == 60


async def test_subworkflow_targets_and_trigger_state_round_trip(
    db_session: AsyncSession,
    entity_id: str,
):
    child_slug = f"portable-child-{entity_id.lower()}"
    parent_slug = f"portable-parent-{entity_id.lower()}"
    payload = _base_payload(**{
        "recipe.workflows": [
            {
                "slug": child_slug,
                "internal": True,
                "steps": [{"id": "child-work", "kind": "agent_call"}],
            },
            {
                "slug": parent_slug,
                "trigger_type": "event",
                "trigger_config": {
                    "event": "task.completed",
                    "filters": {"minimum_score": 0.8},
                },
                "enabled": False,
                "status": "inactive",
                "steps": [{
                    "id": "run-child",
                    "type": "subworkflow",
                    "config": {"workflow_id": child_slug},
                }],
            },
        ],
    })

    first = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    definitions = list((await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workspace_id == first.workspace_id,
        )
    )).scalars())
    by_name = {row.name: row for row in definitions}
    first_child = by_name[child_slug]
    first_parent = by_name[parent_slug]
    [first_subworkflow] = [
        step for step in first_parent.steps if step["type"] == "subworkflow"
    ]
    assert first_subworkflow["config"] == {
        "source_workflow_key": child_slug,
        "workflow_id": first_child.id,
    }
    [first_binding] = list((await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workspace_id == first.workspace_id,
            WorkflowBinding.workflow_id == first_parent.id,
        )
    )).scalars())
    assert first_binding.trigger_config == payload["recipe"]["workflows"][1][
        "trigger_config"
    ]
    assert first_binding.enabled is False
    assert first_binding.status == "inactive"

    exported = await export_workspace(db_session, first.workspace_id, title="Portable")
    exported_by_slug = {
        workflow["slug"]: workflow
        for workflow in exported["recipe"]["workflows"]
    }
    exported_parent = exported_by_slug[parent_slug]
    [portable_subworkflow] = [
        step for step in exported_parent["steps"]
        if step["type"] == "subworkflow"
    ]
    assert portable_subworkflow["config"] == {
        "source_workflow_key": child_slug,
    }
    assert first_child.id not in str(exported)
    assert exported_parent["trigger_config"] == first_binding.trigger_config
    assert exported_parent["enabled"] is False
    assert exported_parent["status"] == "inactive"

    second = await install_blueprint(db_session, entity_id=entity_id, payload=exported)
    await db_session.commit()
    copied = list((await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workspace_id == second.workspace_id,
        )
    )).scalars())
    copied_by_name = {row.name: row for row in copied}
    copied_child = copied_by_name[child_slug]
    copied_parent = copied_by_name[parent_slug]
    [copied_subworkflow] = [
        step for step in copied_parent.steps if step["type"] == "subworkflow"
    ]
    assert copied_subworkflow["config"]["workflow_id"] == copied_child.id
    assert copied_subworkflow["config"]["workflow_id"] != first_child.id


async def test_scheduled_workflow_resolves_portable_slug_to_installed_binding(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"scheduled-flow-{entity_id.lower()}"
    payload = _base_payload(**{
        "recipe.workflows": [{
            "slug": slug,
            "enabled": False,
            "status": "inactive",
            "steps": [{"id": "work", "kind": "agent_call"}],
        }],
        "recipe.scheduled_jobs": [{
            "job_id": "scheduled-flow-run",
            "execution_type": "workflow",
            "execution_target": {"workflow_slug": slug},
            "schedule_kind": "cron",
            "cron_expr": "0 9 * * *",
            "enabled": False,
        }],
    })

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    job = (await db_session.execute(
        select(ScheduledJob).where(
            ScheduledJob.workspace_id == result.workspace_id,
            ScheduledJob.execution_type == "workflow",
        )
    )).scalar_one()
    binding = await db_session.get(
        WorkflowBinding,
        job.execution_target["binding_id"],
    )

    assert job.enabled is False
    assert binding is not None
    assert binding.enabled is False
    assert binding.status == "inactive"
    assert job.execution_target == {
        "workflow_slug": slug,
        "workflow_id": binding.workflow_id,
        "binding_id": binding.id,
        "workspace_id": result.workspace_id,
    }


async def test_scheduled_internal_workflow_targets_definition_without_binding(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"scheduled-internal-flow-{entity_id.lower()}"
    payload = _base_payload(**{
        "recipe.workflows": [{
            "slug": slug,
            "internal": True,
            "steps": [{"id": "work", "kind": "agent_call"}],
        }],
        "recipe.scheduled_jobs": [{
            "job_id": "scheduled-internal-flow-run",
            "execution_type": "workflow",
            "execution_target": {"workflow_slug": slug},
            "schedule_kind": "cron",
            "cron_expr": "0 9 * * *",
            "enabled": True,
        }],
    })

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
    )
    job = (await db_session.execute(
        select(ScheduledJob).where(
            ScheduledJob.workspace_id == result.workspace_id,
            ScheduledJob.execution_type == "workflow",
        )
    )).scalar_one()
    workflow = (await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.workspace_id == result.workspace_id,
            WorkflowDefinition.name == slug,
        )
    )).scalar_one()

    assert job.execution_target == {
        "workflow_slug": slug,
        "workflow_id": workflow.id,
        "workspace_id": result.workspace_id,
    }
    assert not list((await db_session.execute(
        select(WorkflowBinding).where(
            WorkflowBinding.workflow_id == workflow.id,
        )
    )).scalars())


async def test_entity_task_policy_sections_are_rejected_before_mutation(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.prompts": [{"key": "daily", "title": "Daily", "body": "Run the daily loop."}],
            "recipe.task_categories": [{"key": "production", "label": "Production", "sort_order": 20}],
            "recipe.sla_policies": [{"key": "review", "threshold_hours": 24, "description": "Review quickly."}],
            "recipe.escalation_rules": [{"key": "review_late", "sla_policy_key": "review", "action": "notify", "delay_seconds": 60}],
        }
    )
    with pytest.raises(InstallError, match="task_categories.*not portable"):
        await install_blueprint(db_session, entity_id=entity_id, payload=payload)

    assert not (await db_session.execute(select(Workspace).where(Workspace.entity_id == entity_id))).scalars().all()


@pytest.mark.parametrize(
    "output_schema",
    [False, {"$ref": "#"}, {"enum": []}],
)
async def test_invalid_workflow_contract_is_rejected_before_workspace_mutation(
    db_session: AsyncSession,
    entity_id: str,
    output_schema,
):
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": "invalid-contract",
                    "steps": [
                        {
                            "id": "produce",
                            "kind": "agent_call",
                            "output_schema": output_schema,
                        }
                    ],
                }
            ]
        }
    )

    with pytest.raises(InstallError, match="invalid config.output_schema"):
        await install_blueprint(db_session, entity_id=entity_id, payload=payload)

    workspaces = (
        await db_session.execute(
            select(Workspace).where(Workspace.entity_id == entity_id)
        )
    ).scalars().all()
    assert workspaces == []


async def test_prompts_are_workspace_guidance_and_roundtrip(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.prompts": [{"key": "daily", "title": "Daily", "body": "Run the daily loop."}],
        }
    )
    installed = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()

    workspace = (
        await db_session.execute(select(Workspace).where(Workspace.id == installed.workspace_id))
    ).scalar_one()
    assert workspace.operating_model["blueprint_prompts"] == payload["recipe"]["prompts"]
    exported = await export_workspace(db_session, workspace.id, title="T")
    assert exported["recipe"]["prompts"] == payload["recipe"]["prompts"]


async def test_reinstall_creates_an_isolated_workflow_per_workspace(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"wf-one-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "manual",
                    "variables": [],
                    "steps": [
                        {"id": "s1", "kind": "agent_call"},
                    ],
                }
            ],
        }
    )
    first_install = await install_blueprint(
        db_session, entity_id=entity_id, payload=payload,
    )
    await db_session.commit()
    first = (
        await db_session.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.entity_id == entity_id,
                WorkflowDefinition.name == slug,
            )
        )
    ).scalar_one()
    first.steps = [{"id": "s1", "type": "agent_call", "name": "stale", "config": {}, "next": []}]
    first.status = "draft"
    await db_session.commit()

    second_install = await install_blueprint(
        db_session, entity_id=entity_id, payload=payload,
    )
    await db_session.commit()
    rows = list(
        (
            await db_session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.entity_id == entity_id,
                    WorkflowDefinition.name == slug,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    by_workspace = {row.workspace_id: row for row in rows}
    assert by_workspace[first_install.workspace_id].id == first.id
    assert by_workspace[first_install.workspace_id].status == "draft"
    second = by_workspace[second_install.workspace_id]
    assert second.id != first.id
    assert second.status == "active"
    validation = validate_workflow_steps(second.steps)
    assert validation["valid"], validation
    assert validation["entry_step_id"] == "start"
    steps_by_id = {step["id"]: step for step in second.steps}
    assert steps_by_id["start"]["next"] == ["s1"]
    assert steps_by_id["s1"]["type"] == "agent"


async def test_same_component_key_from_two_blueprint_ids_does_not_collide(
    db_session: AsyncSession,
    entity_id: str,
):
    workflow = {
        "slug": "shared-readable-key",
        "trigger_type": "manual",
        "variables": [],
        "steps": [{"id": "work", "kind": "agent_call"}],
    }
    payload = _base_payload(**{"recipe.workflows": [workflow]})

    first_install = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        blueprint_id="blueprint:first",
    )
    second_install = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        blueprint_id="blueprint:second",
    )
    await db_session.commit()

    workflows = list((await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.name == workflow["slug"],
        )
    )).scalars().all())
    sources = list((await db_session.execute(
        select(WorkflowTemplateInstallation).where(
            WorkflowTemplateInstallation.entity_id == entity_id,
            WorkflowTemplateInstallation.component_key == workflow["slug"],
        )
    )).scalars().all())
    links = list((await db_session.execute(
        select(MarketplaceResourceLink).where(
            MarketplaceResourceLink.entity_id == entity_id,
            MarketplaceResourceLink.marketplace_resource_type
            == "workspace_blueprint",
            MarketplaceResourceLink.relationship == "installed_component",
            MarketplaceResourceLink.local_resource_type == "workflow",
            MarketplaceResourceLink.component_key == workflow["slug"],
        )
    )).scalars().all())

    assert len(workflows) == 2
    assert {workflow.workspace_id for workflow in workflows} == {
        first_install.workspace_id,
        second_install.workspace_id,
    }
    assert len({source.template_id for source in sources}) == 2
    assert all(
        source.template_id.startswith("workspace-blueprint-install:")
        for source in sources
    )
    assert len({source.workflow_id for source in sources}) == 2
    assert {link.marketplace_resource_id for link in links} == {
        "blueprint:first",
        "blueprint:second",
    }
    assert {link.local_resource_id for link in links} == {
        workflow_row.id for workflow_row in workflows
    }


async def test_raw_payloads_with_the_same_slug_do_not_share_flow_identity(
    db_session: AsyncSession,
    entity_id: str,
):
    workflow = {
        "slug": "same-raw-payload-slug",
        "description": "first payload",
        "trigger_type": "manual",
        "variables": [],
        "steps": [{"id": "work", "kind": "agent_call"}],
    }
    first_payload = _base_payload(**{
        "manifest.slug": "same-manifest-slug",
        "recipe.workflows": [workflow],
    })
    second_payload = copy.deepcopy(first_payload)
    second_payload["recipe"]["workflows"][0]["description"] = "second payload"

    first = await install_blueprint(
        db_session, entity_id=entity_id, payload=first_payload,
    )
    second = await install_blueprint(
        db_session, entity_id=entity_id, payload=second_payload,
    )
    await db_session.commit()

    definitions = list((await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.name == workflow["slug"],
        )
    )).scalars().all())
    by_workspace = {row.workspace_id: row for row in definitions}
    assert by_workspace[first.workspace_id].description == "first payload"
    assert by_workspace[second.workspace_id].description == "second payload"
    assert by_workspace[first.workspace_id].id != by_workspace[second.workspace_id].id


async def test_workflow_binding_config_installs_and_resynchronizes(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"chat-entrypoint-{entity_id}"
    workflow = {
        "slug": slug,
        "trigger_type": "manual",
        "variables": [
            {"key": "request", "default": ""},
            {"key": "removed_default", "default": "legacy value"},
        ],
        "run_inputs": [{
            "key": "request",
            "label": "Request",
            "type": "string",
            "required": True,
        }],
        "binding_config": {
            "chat_entrypoint": {
                "enabled": True,
                "title": "Run workflow",
            }
        },
        "steps": [{"id": "s1", "kind": "agent_call"}],
    }
    payload = _base_payload(**{"recipe.workflows": [workflow]})

    first = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    binding = (
        await db_session.execute(
            select(WorkflowBinding).where(WorkflowBinding.id == first.workflow_binding_ids[0])
        )
    ).scalar_one()
    assert binding.config["source"] == "blueprint"
    assert binding.config["chat_entrypoint"]["title"] == "Run workflow"
    definition = (await db_session.execute(
        select(WorkflowDefinition).where(WorkflowDefinition.id == binding.workflow_id)
    )).scalar_one()
    assert definition.steps[0]["config"]["run_inputs"] == workflow["run_inputs"]
    assert definition.steps[0]["config"]["outputs"] == [{
        "key": "request",
        "type": "text",
        "value": "{{start.request}}",
    }]

    binding.config = {
        **dict(binding.config or {}),
        "operator_setting": "preserve-me",
    }
    binding.variables = {
        **dict(binding.variables or {}),
        "operator_variable": "preserve-me",
    }
    await db_session.flush()
    workflow["binding_config"]["chat_entrypoint"]["title"] = "Updated workflow"
    workflow["variables"] = [{"key": "request", "default": "updated request"}]
    workflow["deprecated_variable_keys"] = ["removed_default"]
    workflow["trigger_type"] = "mcp"
    workflow["trigger_config"] = {
        "event": "workspace.updated",
        "filters": {"source": "operator"},
    }
    workflow["enabled"] = False
    workflow["status"] = "inactive"
    second_binding_id = await _install_workflow_binding(
        db_session,
        entity_id=entity_id,
        workspace_id=binding.workspace_id,
        workflow_id=binding.workflow_id,
        w=workflow,
        source_template_id=binding.config["source_template_id"],
    )
    await db_session.commit()
    await db_session.refresh(binding)

    assert second_binding_id == binding.id
    assert binding.trigger_type == "mcp"
    assert binding.trigger_config == workflow["trigger_config"]
    assert binding.enabled is False
    assert binding.status == "inactive"
    assert binding.config["chat_entrypoint"]["title"] == "Updated workflow"
    assert binding.config["operator_setting"] == "preserve-me"
    assert binding.variables == {
        "request": "updated request",
        "operator_variable": "preserve-me",
    }


async def test_blueprint_workflow_can_persist_explicit_typed_data_contracts(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"explicit-contracts-{entity_id}"
    payload = _base_payload(**{
        "recipe.workflows": [{
            "slug": slug,
            "trigger_type": "manual",
            "explicit_data_contracts": True,
            "run_inputs": [{"key": "source", "type": "string", "required": True}],
            "steps": [
                {
                    "id": "draft",
                    "kind": "agent_call",
                    "input": "Draft from {{source}}",
                    "output_var": "packet",
                    "output_format": "json",
                },
                {
                    "id": "gate",
                    "type": "condition",
                    "depends_on": ["draft"],
                    "config": {"expression": "packet.approved == true"},
                },
                {
                    "id": "done",
                    "type": "end",
                    "depends_on": ["gate"],
                    "config": {"inputs": {"input": "{{packet}}"}},
                },
            ],
        }],
    })

    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    definition = (await db_session.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.entity_id == entity_id,
            WorkflowDefinition.name == slug,
        )
    )).scalar_one()
    steps = {step["id"]: step for step in definition.steps}

    assert all(isinstance(step["config"]["inputs"], list) for step in steps.values())
    assert all(isinstance(step["config"]["outputs"], list) for step in steps.values())
    assert steps["draft"]["config"]["inputs"] == [
        {"key": "source", "value": "{{source}}", "type": "text"},
    ]
    assert steps["draft"]["config"]["outputs"] == [
        {"key": "packet", "value": "{{draft}}", "type": "json"},
    ]
    assert steps["gate"]["config"]["inputs"] == [
        {"key": "packet", "value": "{{packet}}", "type": "json"},
    ]
    assert steps["done"]["config"]["inputs"] == [
        {"key": "input", "value": "{{packet}}", "type": "json"},
    ]


async def test_workflow_diamond_dependencies(
    db_session: AsyncSession,
    entity_id: str,
):
    """A step depended on by multiple downstreams populates `next` with
    all of them."""
    slug = f"diamond-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "manual",
                    "variables": [],
                    "steps": [
                        {"id": "root", "kind": "agent_call"},
                        {"id": "branch_a", "kind": "agent_call", "depends_on": ["root"]},
                        {"id": "branch_b", "kind": "agent_call", "depends_on": ["root"]},
                        {"id": "join", "kind": "agent_call", "depends_on": ["branch_a", "branch_b"]},
                    ],
                }
            ],
        }
    )
    await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    wf = (await db_session.execute(select(WorkflowDefinition).where(WorkflowDefinition.name == slug))).scalar_one()
    validation = validate_workflow_steps(wf.steps)
    assert validation["valid"], validation
    steps_by_id = {s["id"]: s for s in wf.steps}
    assert steps_by_id["start"]["next"] == ["root"]
    assert sorted(steps_by_id["root"]["next"]) == ["branch_a", "branch_b"]
    assert steps_by_id["branch_a"]["next"] == ["join"]
    assert steps_by_id["branch_b"]["next"] == ["join"]
    assert steps_by_id["join"]["next"] == []


# ── post_install_checks ───────────────────────────────────────────────


async def test_check_session_alive_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    db_session.add(
        IntegrationSession(
            id=generate_ulid(),
            entity_id=entity_id,
            provider="x",
            label="main",
            status="active",
        )
    )
    await db_session.commit()
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "session_alive", "provider": "x", "session_label": "main"},
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    session = (await db_session.execute(
        select(IntegrationSession).where(IntegrationSession.entity_id == entity_id)
    )).scalar_one()
    session.status = "inactive"
    await db_session.flush()
    disabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert disabled is not None and disabled.blocks_work


async def test_check_session_alive_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "session_alive", "provider": "x", "session_label": "main"},
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert pic_todos[0].blocking is True
    assert "no active session" in pic_todos[0].detail


async def test_check_agent_callable_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"ax-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "A",
                    "system_prompt": "Test agent-callable post-install check.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "recipe.subscriptions": [
                {
                    "service_key": "x.svc",
                    "agent_slug": agent_slug,
                    "custom_prompt": None,
                    "config": {},
                }
            ],
            "policy.post_install_checks": [
                {"kind": "agent_callable", "service_key": "x.svc"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    subscription = await db_session.get(AgentSubscription, result.subscription_ids[0])
    assert subscription is not None
    subscription.status = "inactive"
    await db_session.flush()
    disabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert disabled is not None and disabled.blocks_work


async def test_legacy_entity_agent_subscription_ignores_scoped_slug_collision(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"legacy-{entity_id}"
    entity_agent = Agent(
        id=generate_ulid(),
        entity_id=entity_id,
        name="Entity Agent",
        slug=agent_slug,
        visibility=Visibility.ENTITY,
        status="active",
    )
    db_session.add_all([
        entity_agent,
        Agent(
            id=generate_ulid(),
            entity_id=entity_id,
            workspace_id=generate_ulid(),
            name="Scoped Agent",
            slug=agent_slug,
            visibility=Visibility.WORKSPACE,
            status="active",
        ),
    ])
    await db_session.commit()

    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=_base_payload(**{
            "recipe.subscriptions": [{
                "service_key": "x.svc",
                "agent_slug": agent_slug,
                "custom_prompt": None,
                "config": {},
            }],
            "policy.post_install_checks": [{
                "kind": "agent_callable",
                "service_key": "x.svc",
            }],
        }),
    )

    assert result.subscription_ids
    subscription = await db_session.get(AgentSubscription, result.subscription_ids[0])
    assert subscription is not None
    assert subscription.agent_id == entity_agent.id
    assert not [todo for todo in result.todos if todo.kind == "post_install_check"]


async def test_check_agent_callable_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "agent_callable", "service_key": "x.svc"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert "no active subscription" in pic_todos[0].detail


async def test_check_cron_scheduled_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    agent_slug = f"scheduled-{entity_id}"
    payload = _base_payload(
        **{
            "embedded.agents": [
                {
                    "slug": agent_slug,
                    "name": "Scheduled agent",
                    "system_prompt": "Run the scheduled draft.",
                    "config": {},
                    "tool_bindings": [],
                    "mcp_bindings": [],
                    "skill_bindings": [],
                    "starter_memory": [],
                }
            ],
            "recipe.subscriptions": [
                {
                    "service_key": "x.svc",
                    "agent_slug": agent_slug,
                    "custom_prompt": None,
                    "config": {},
                }
            ],
            "recipe.scheduled_jobs": [
                {
                    "job_id": "morning-draft",
                    "name": "Morning",
                    "schedule_kind": "cron",
                    "cron_expr": "0 8 * * *",
                    "execution_type": "agent_message",
                    "execution_target": {"service_key": "x.svc"},
                    "payload_message": "draft",
                }
            ],
            "policy.post_install_checks": [
                {"kind": "cron_scheduled", "job_id": "morning-draft"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    job = (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id)
    )).scalar_one()
    job.enabled = False
    await db_session.flush()
    disabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert disabled is not None and disabled.blocks_work


async def test_scheduled_agent_install_fails_when_service_is_not_deployed(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.scheduled_jobs": [
                {
                    "job_id": "orphan-agent-job",
                    "schedule_kind": "cron",
                    "cron_expr": "0 8 * * *",
                    "execution_type": "agent",
                    "execution_target": {"service_key": "missing.service"},
                    "payload_message": "Run the missing service.",
                }
            ],
        }
    )

    with pytest.raises(InstallError, match="no active Workspace subscription"):
        await install_blueprint(db_session, entity_id=entity_id, payload=payload)


async def test_check_cron_scheduled_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "cron_scheduled", "job_id": "morning-draft"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert pic_todos[0].blocking is True

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    db_session.add(ScheduledJob(
        id=generate_ulid(),
        job_id=installed_blueprint_job_id("morning-draft", workspace.id),
        entity_id=entity_id,
        workspace_id=workspace.id,
        enabled=False,
    ))
    await db_session.flush()
    disabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert disabled is not None and disabled.blocks_work

    job = (await db_session.execute(
        select(ScheduledJob).where(ScheduledJob.workspace_id == workspace.id)
    )).scalar_one()
    job.enabled = True
    await db_session.flush()
    enabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert enabled is not None
    assert enabled.blocks_work is False


async def test_check_workflow_present_pass(
    db_session: AsyncSession,
    entity_id: str,
):
    slug = f"wf-test-{entity_id}"
    payload = _base_payload(
        **{
            "recipe.workflows": [
                {
                    "slug": slug,
                    "trigger_type": "manual",
                    "variables": [],
                    "steps": [
                        {
                            "id": "start",
                            "type": "trigger",
                            "name": "Start",
                            "config": {"trigger_type": "manual"},
                            "next": ["end"],
                        },
                        {
                            "id": "end",
                            "type": "end",
                            "name": "Done",
                            "config": {},
                            "next": [],
                        },
                    ],
                }
            ],
            "policy.post_install_checks": [
                {"kind": "workflow_dryrun", "workflow_slug": slug},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert pic_todos == []

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    binding = (await db_session.execute(
        select(WorkflowBinding).where(WorkflowBinding.workspace_id == workspace.id)
    )).scalar_one()
    binding.enabled = False
    await db_session.flush()
    disabled = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert disabled is not None and disabled.blocks_work


async def test_check_workflow_present_fail(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "workflow_dryrun", "workflow_slug": f"wf-missing-{entity_id}"},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert f"wf-missing-{entity_id}" in pic_todos[0].detail

    workspace = await db_session.get(Workspace, result.workspace_id)
    assert workspace is not None
    slug = f"wf-missing-{entity_id}"
    workflow = WorkflowDefinition(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        name=slug,
        trigger_type="manual",
        steps=[],
        variables={},
        is_active=False,
        status="inactive",
    )
    binding = WorkflowBinding(
        id=generate_ulid(),
        entity_id=entity_id,
        workspace_id=workspace.id,
        workflow_id=workflow.id,
        trigger_type="manual",
        config={"workspace_blueprint_workflow_slug": slug},
        enabled=True,
        status="active",
    )
    db_session.add_all([workflow, binding])
    await db_session.flush()
    inactive = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert inactive is not None and inactive.blocks_work

    workflow.is_active = True
    workflow.status = "active"
    await db_session.flush()
    active = await evaluate_workspace_blocking_setup(db_session, workspace)
    assert active is not None
    assert active.blocks_work is False


async def test_check_unknown_kind_non_blocking_note(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "policy.post_install_checks": [
                {"kind": "ping_satellite", "freq_mhz": 2400},
            ],
        }
    )
    result = await install_blueprint(db_session, entity_id=entity_id, payload=payload)
    await db_session.commit()
    pic_todos = [t for t in result.todos if t.kind == "post_install_check"]
    assert len(pic_todos) == 1
    assert pic_todos[0].blocking is False
    assert "ping_satellite" in pic_todos[0].detail


async def test_check_blocking_workspace_setup_surfaces_install_todo(
    db_session: AsyncSession,
    entity_id: str,
):
    payload = _base_payload(
        **{
            "recipe.operating_model": {
                "settings": {
                    "blocking_setup": {
                        "allowed_setup_task_keys": ["prepare_workspace_identity"],
                        "checks": [
                            {
                                "key": "workspace_identity",
                                "kind": "workspace_identity_assets",
                                "asset_key": "stickman_character",
                                "asset_path": "brand/stickman-character.png",
                                "narrator_profile_key": "stickman_narrator_profile",
                                "manifest_path": "brand/stickman-workspace-assets.md",
                                "setup_task_key": "prepare_workspace_identity",
                            }
                        ],
                    }
                }
            },
            "policy.post_install_checks": [
                {"kind": "blocking_setup_ready"},
            ],
        }
    )
    result = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
    )
    await db_session.commit()

    setup_todos = [todo for todo in result.todos if todo.kind == "blocking_setup"]
    assert len(setup_todos) == 1
    assert setup_todos[0].blocking is True
    assert setup_todos[0].payload["result"] == "blocking_setup_incomplete"
    assert setup_todos[0].payload["incomplete_checks"][0]["key"] == "workspace_identity"
    assert setup_todos[0].payload["allowed_setup_task_keys"] == [
        "prepare_workspace_identity"
    ]
