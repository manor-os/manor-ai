"""Read-only connection guidance shared by created and installed Workspaces.

Requirements come from current bindings and durable declarations, never from a
previous successful check. This projection does not change execution admission
or create channel/account bindings.
"""
from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.setup_preflight import BlueprintSetupPreflightFactory
from packages.core.constants.blueprints import BlueprintInstallRequirementKind, BlueprintInstallTodoKind
from packages.core.models.document import Integration
from packages.core.models.mcp import AgentMCPBinding, MCPServer
from packages.core.models.skill import AgentSkillBinding, Skill
from packages.core.models.user import OAuthAccount
from packages.core.models.workspace import Agent, AgentSubscription, AgentToolBinding, ToolDefinition, Workspace
from packages.core.services.provider_keys import canonical_provider_key
from packages.core.services.workspace_integration_scope import disabled_workspace_providers
from packages.core.services.integration_resolution import (
    integration_provider_readiness,
    is_user_facing_integration_provider,
)
from packages.core.services.integration_account_service import (
    IntegrationAccountKind,
    load_runtime_integration_registry,
    select_runtime_integration_account,
)
from packages.core.services.workspace_readiness import (
    channel_type_has_account_model,
    evaluate_workspace_blocking_setup,
    list_configured_workspace_channels,
    unbacked_channel_platform,
    workspace_declared_channels,
)


async def _failed_account_health(db: AsyncSession, *, workspace: Workspace, user_id: str, providers: list[str]) -> set[str]:
    """Project existing health evidence for the exact runtime-selected accounts.

    No provider requests, secret reads, or new execution gate are introduced.
    A saved failed probe remains guidance until the normal health checker
    records a successful check or the user reconnects/selects another account.
    """
    if not providers:
        return set()
    registry = await load_runtime_integration_registry(
        db, entity_id=workspace.entity_id, user_id=user_id, provider_keys=providers,
    )
    selected = []
    for provider in providers:
        account, _ = select_runtime_integration_account(list(registry.accounts_for(provider)))
        if account:
            selected.append(account)
    failed: set[str] = set()
    for kind, model, metadata in (
        (IntegrationAccountKind.OAUTH_ACCOUNT, OAuthAccount, OAuthAccount.profile),
        (IntegrationAccountKind.INTEGRATION, Integration, Integration.config),
    ):
        account_ids = {account.id: account.provider for account in selected if account.kind is kind}
        if not account_ids:
            continue
        rows = (await db.execute(select(model.id, metadata["last_health_check"]).where(model.id.in_(account_ids)))).all()
        for account_id, health in rows:
            if isinstance(health, dict) and health.get("ok") is False:
                failed.add(account_ids[account_id])
    return failed


_BROWSER_EXTENSION_PROVIDER = "chrome"


class WorkspaceConnectionStatusFactory:
    @classmethod
    async def create(cls, db: AsyncSession, *, workspace: Workspace, user_id: str) -> dict:
        settings = workspace.settings or {}
        blueprint = settings.get("_blueprint") or {}
        live_requirements = blueprint.get("live_setup_requirements") or []
        disabled_providers = disabled_workspace_providers(workspace.operating_model)
        providers: dict[str, dict] = {}
        sessions: list[dict] = []
        service_keys: dict[str, set[str]] = {}
        # Providers someone declared on purpose (Blueprint contract, setup
        # gate, explicit flag). Everything else is inferred from what agents
        # happen to be bound to, and only those may fall back to the browser.
        declared_provider_keys: set[str] = set()

        def declare(provider: object, *, required: bool = True, explicit: bool = True) -> None:
            key = canonical_provider_key(provider)
            if (
                not key
                or key in disabled_providers
                or not is_user_facing_integration_provider(key)
            ):
                return
            if explicit:
                declared_provider_keys.add(key)
            if key in providers:
                providers[key]["required"] |= required
            else:
                providers[key] = {"slug": str(provider).strip(), "required": required}

        # Explicit optional requirements take precedence over an inferred
        # dependency. An independently declared required use still wins.
        for todo in live_requirements:
            payload = todo.get("payload") or {}
            kind = todo.get("kind")
            required = bool(todo.get("blocking", True))
            if kind == BlueprintInstallTodoKind.MISSING_INTEGRATION.value:
                declare(payload.get("provider"), required=required)
            elif kind in {BlueprintInstallTodoKind.MCP_SERVER.value, BlueprintInstallTodoKind.MCP_CONFIGURATION.value}:
                declare(payload.get("server_slug"), required=required)
            elif kind == BlueprintInstallTodoKind.BROWSER_SESSION.value:
                # Captured browser sessions are not a connection people can add.
                # A platform with an integration is offered as that integration
                # (connected in place); the rest publish through Chrome.
                from packages.core.ai.tools.integration_route_tools import integration_provider_for_platform

                integration = integration_provider_for_platform(str(payload.get("provider") or ""))
                if integration:
                    declare(integration, required=False)

        for check in (settings.get("blocking_setup") or {}).get("checks") or []:
            if check.get("kind") == "integration_provider":
                declare(check.get("provider"), required=bool(check.get("blocking", True)))

        for flag in settings.get("flagged_integrations") or []:
            if not isinstance(flag, dict):
                continue
            provider = flag.get("provider")
            key = canonical_provider_key(provider)
            if key not in providers and flag.get("source") not in {"channel_setup", "blueprint_channel", "blueprint_session"}:
                declare(provider, required=bool(flag.get("required", True)))
            if key:
                service_keys.setdefault(key, set()).update(flag.get("linked_service_keys") or [])

        channels = workspace_declared_channels(workspace)

        subs = list((await db.execute(
            select(AgentSubscription).join(Agent, Agent.id == AgentSubscription.agent_id).where(
                AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.entity_id == workspace.entity_id,
                AgentSubscription.status == "active",
                Agent.deleted_at.is_(None),
                Agent.status == "active",
                or_(Agent.entity_id == workspace.entity_id, Agent.entity_id.is_(None)),
            )
        )).scalars().all())
        services_by_agent: dict[str, set[str]] = {}
        for sub in subs:
            services_by_agent.setdefault(sub.agent_id, set()).add(sub.service_key)

        def infer(provider: str, agent_id: str) -> None:
            key = canonical_provider_key(provider)
            if (
                not key
                or key in disabled_providers
                or not is_user_facing_integration_provider(key)
            ):
                return
            if key not in providers:
                declare(provider, explicit=False)
            service_keys.setdefault(key, set()).update(services_by_agent[agent_id])

        def infer_tools(names: list[str], agent_id: str) -> None:
            for name in names:
                parts = str(name).split("__", 2)
                if len(parts) == 3 and parts[0] == "mcp":
                    infer(parts[1], agent_id)

        if services_by_agent:
            for agent_id, server_key in (await db.execute(
                select(AgentMCPBinding.agent_id, MCPServer.server_key)
                .join(MCPServer, MCPServer.id == AgentMCPBinding.mcp_server_id)
                .where(AgentMCPBinding.agent_id.in_(services_by_agent), AgentMCPBinding.status == "active")
            )).all():
                infer(server_key, agent_id)
            for agent_id, tool_name in (await db.execute(
                select(AgentToolBinding.agent_id, ToolDefinition.name)
                .join(ToolDefinition, ToolDefinition.id == AgentToolBinding.tool_id)
                .where(AgentToolBinding.agent_id.in_(services_by_agent), ToolDefinition.status == "active")
            )).all():
                infer_tools([tool_name], agent_id)
            for agent_id, tools in (await db.execute(
                select(AgentSkillBinding.agent_id, Skill.tools)
                .join(Skill, Skill.id == AgentSkillBinding.skill_id)
                .where(
                    AgentSkillBinding.agent_id.in_(services_by_agent),
                    AgentSkillBinding.status == "active", Skill.status == "active",
                    or_(Skill.entity_id == workspace.entity_id, Skill.entity_id.is_(None)),
                )
            )).all():
                infer_tools(tools or [], agent_id)

        # Retain exact registry spellings (canonical keys are only identities).
        # Also detect a disabled/removed MCP instead of treating a still-saved
        # OAuth account as proof that the capability itself remains available.
        catalog = list((await db.execute(select(MCPServer.server_key, MCPServer.status))).all()) if providers else []
        active_keys = {canonical_provider_key(key) for key, status in catalog if status == "active"}
        spellings: dict[str, list[str]] = {}
        for raw, _ in catalog:
            spellings.setdefault(canonical_provider_key(raw), []).append(raw)
        provider_specs = [
            {**spec, "slug": registered_key}
            for key, spec in providers.items()
            for registered_key in spellings.get(key, [spec["slug"]])
        ]
        # Instagram works through the Meta integration: when that is already asked
        # for, a second "instagram (no built-in channel)" row asks for nothing new.
        from packages.core.ai.tools.integration_route_tools import integration_provider_for_platform

        covered_services = {service for keys in service_keys.values() for service in keys}
        channels = [
            channel for channel in channels
            if channel_type_has_account_model(channel.get("channel_type"))
            or (channel.get("linked_service_key") or None) not in covered_services
            and canonical_provider_key(
                integration_provider_for_platform(unbacked_channel_platform(channel))
            ) not in providers
        ]
        preflight = await BlueprintSetupPreflightFactory.from_contract(
            db, entity_id=workspace.entity_id, user_id=user_id,
            workspace_id=workspace.id,
            contract={
                "requires": {"mcp_servers": provider_specs},
                "channels": channels, "sessions": sessions,
            },
        )
        attached_channel_ids = {
            channel.get("channel_config_id")
            for channel in (await list_configured_workspace_channels(db, workspace) if channels else [])
        }
        failed_health = await _failed_account_health(
            db, workspace=workspace, user_id=user_id,
            providers=[state.provider for state in preflight.requirements if state.kind is BlueprintInstallRequirementKind.INTEGRATION and state.ready],
        )
        requirements = []
        for index, state in enumerate(preflight.requirements):
            item = {
                "kind": state.kind.value, "provider": state.provider,
                "label": state.label, "required": state.required,
                "ready": state.ready, "reason": state.reason,
                "setup_kind": state.setup_kind,
            }
            if state.kind is BlueprintInstallRequirementKind.INTEGRATION and state.provider in failed_health:
                item["ready"] = False
                item["reason"] = "The latest connection health check failed. Open Integrations and run Test now."
            if state.kind is BlueprintInstallRequirementKind.INTEGRATION and canonical_provider_key(state.provider) not in active_keys:
                item["ready"] = False
                item["reason"] = "This integration is disabled or missing from the current MCP catalog."
            if state.kind is BlueprintInstallRequirementKind.CHANNEL and state.resource_id and state.resource_id not in attached_channel_ids:
                item["ready"] = False
                item["reason"] = "Attach the connected channel account to this Workspace."
            item["key"] = state.requirement_key or f"{state.kind.value}:{state.provider}:{index}"
            item["service_keys"] = sorted(filter(None, service_keys.get(canonical_provider_key(state.provider), set())))
            requirements.append(item)

        # The Chrome extension is a general capability: it can operate any
        # platform in the browser. A platform integration that agents are
        # merely bound to (not declared required by a Blueprint, the setup
        # gate, or an explicit flag) therefore stops being a hard requirement
        # while the extension is connected — connect the dedicated integration
        # for best results, but either one is enough to work.
        browser_fallback_candidates = [
            item for item in requirements
            if item["kind"] == BlueprintInstallRequirementKind.INTEGRATION.value
            and item["required"] and not item["ready"]
            and canonical_provider_key(item["provider"]) not in declared_provider_keys
            and canonical_provider_key(item["provider"]) != _BROWSER_EXTENSION_PROVIDER
        ]
        if browser_fallback_candidates:
            browser_state = (await integration_provider_readiness(
                db, entity_id=workspace.entity_id, user_id=user_id,
                provider_keys=[_BROWSER_EXTENSION_PROVIDER],
            )).get(_BROWSER_EXTENSION_PROVIDER)
            if browser_state is not None and browser_state.ready:
                for item in browser_fallback_candidates:
                    item["required"] = False
                    item["covered_by"] = _BROWSER_EXTENSION_PROVIDER
                    item["reason"] = (
                        f"{item['reason']} The Chrome extension is connected and can operate "
                        f"{item['label']} in the browser meanwhile; connecting the dedicated "
                        "integration as well is best."
                    ).strip()

        # Surface exact MCP binding/configuration regressions from the existing
        # runtime gate as well, without inventing a second admission policy.
        gate = await evaluate_workspace_blocking_setup(db, workspace)
        blocking = [todo for todo in live_requirements if todo.get("blocking", True)]
        for check in (gate.details.get("incomplete_checks", []) if gate else []):
            if check.get("todo_kind") not in {
                BlueprintInstallTodoKind.MCP_SERVER.value,
                BlueprintInstallTodoKind.MCP_CONFIGURATION.value,
            }:
                continue
            index = int(check["key"].rsplit(":", 1)[-1])
            # The gate indexes the blocking subset of the durable contract.
            provider = canonical_provider_key((blocking[index].get("payload") or {}).get("server_slug"))
            if provider in disabled_providers:
                continue
            requirements.append({
                "key": check["key"], "kind": BlueprintInstallRequirementKind.INTEGRATION.value, "provider": provider,
                "label": provider, "required": True, "ready": False,
                "reason": check["reason"], "service_keys": sorted(filter(None, service_keys.get(provider, set()))),
                "setup_kind": "mcp_binding",
            })

        return {
            "workspace_id": workspace.id,
            "requirements": requirements,
            "required_issue_count": sum(item["required"] and not item["ready"] for item in requirements),
        }


#: Providers this Workspace already asked its owner to connect. Asking twice
#: would only repeat a question the person answered (or chose to postpone).
CONNECTION_QUESTIONS_ASKED_KEY = "connection_questions_asked"


def _connect_body_zh(label: str, waiting: list[str]) -> str:
    services = "、".join(waiting)
    lead = f"{services} 正在等待 {label}。" if services else f"这个工作区里有工作在等待 {label}。"
    return lead + "连接后这些工作就会开始，你设的规则依然有效。"


#: Marks a card this module posted: the App's connections block already lists
#: the same requirement, so the decision queue leaves it out.
CONNECTION_REQUEST_KEY = "connection_request"


def _connect_question(*, provider: str, label: str, waiting: list[str]) -> dict:
    """The Reconnect card, worded for a first connection: sign in on the
    provider's own setup page, recommended first; never secrets in chat.

    Title and description carry codes so each viewer reads them in their own
    language; the English text is the fallback.
    """
    from packages.core.ai.pending_action import PendingAction
    from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
    from packages.core.services.decision_recommendation import recommendation

    action = PendingAction.needs_login(
        login_url=runtime_integration_setup_link(provider).get("setup_url") or "/integrations",
        title=f"Connect {label}",
        integration_hint=provider,
        provider_key=provider,
    ).to_dict()
    services = ", ".join(waiting)
    action.update({
        CONNECTION_REQUEST_KEY: True,
        "title_code": "integration.connect",
        "title_params": {"provider": label},
        "description": (
            f"{services} waits for {label}. Connect it and that work starts; your rules for it still apply."
            if services else
            f"Work in this Workspace waits for {label}. Connect it and that work starts; your rules for it still apply."
        ),
        "description_code": "connect_waiting" if services else "connect_waiting_none",
        "description_params": {"provider": label, "services": services},
    })
    action["option_details"] = {
        "sign_in": {"label": f"Connect {label}", "label_code": "integration.connect", "label_params": {"provider": label}},
        "skip": {"label": "Remind me later", "label_code": "integration.later"},
    }
    action["recommendation"] = recommendation(
        "sign_in", reason_code="connect_pending", reason_params={"provider": label},
    )
    return action


async def ask_to_connect_pending_integrations(
    db: AsyncSession,
    *,
    workspace: Workspace,
    user_id: str | None,
    publish_event: bool = True,
) -> list[str]:
    """Ask in Workspace chat to connect each required integration not yet ready.

    Created, installed and edited (an applied operation draft) Workspaces keep
    a requested integration that is not connected as a pending requirement
    (bindings, channel, rules) instead of dropping it. This puts the matching
    question in front of the person, with connecting recommended. Each
    provider is asked once. The caller commits; the card is published after.
    Returns the providers asked about.
    """
    if not user_id:
        return []
    status = await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user_id)
    settings = dict(workspace.settings or {})
    asked = [str(item) for item in settings.get(CONNECTION_QUESTIONS_ASKED_KEY) or []]
    # A service's id is ``service_key`` or ``key`` depending on who wrote it
    # (architect, operation, Blueprint); people read its ``name``.
    service_names = {
        str(service.get("service_key") or service.get("key") or ""): str(service.get("name") or service.get("title") or "")
        for service in ((workspace.operating_model or {}).get("services") or [])
        if isinstance(service, dict)
    }
    pending = [
        item for item in status["requirements"]
        if item["kind"] == BlueprintInstallRequirementKind.INTEGRATION.value
        and item["required"] and not item["ready"]
        and canonical_provider_key(item["provider"]) not in asked
    ]
    if not pending:
        return []

    from packages.core.models.user import User
    from packages.core.workspace_chat.service import post_message, queue_workspace_chat_message_event_after_commit

    # The chat line under the card is stored once; write it in the language of
    # the person it asks (the card itself localizes per viewer).
    asker = await db.get(User, user_id)
    chinese = str(getattr(asker, "locale", "") or "").lower().startswith("zh")
    posted: list[str] = []
    for item in pending:
        provider = canonical_provider_key(item["provider"])
        if provider in posted:
            continue
        label = str(item.get("label") or provider)
        # Never show a raw service key; a service without a name is left out.
        waiting = [name for key in item.get("service_keys") or [] if (name := service_names.get(key))]
        card = _connect_question(provider=item["provider"], label=label, waiting=waiting)
        message = await post_message(
            db,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            body=_connect_body_zh(label, waiting) if chinese else card["description"],
            message_kind="hitl_request",
            author_kind="system",
            pending_action=card,
            publish_event=False,
        )
        if publish_event:
            # Callers ask inside their own transaction (an applied change, an install).
            queue_workspace_chat_message_event_after_commit(
                db, entity_id=workspace.entity_id, workspace_id=workspace.id, message=message,
            )
        posted.append(provider)
    settings[CONNECTION_QUESTIONS_ASKED_KEY] = [*asked, *posted]
    workspace.settings = settings
    return posted


# How a step's own result says it could not reach something, when it does not name it.
_CONNECTION_WORDS = (
    "connect", "integration", "authoriz", "authoris", "access", "sign in", "log in", "login", "credential", "tool",
    "连接", "授权", "登录", "接入", "权限",
)


def workflow_step_result_evidence(result: object) -> str:
    """What a step's own result says went wrong."""
    if not isinstance(result, dict):
        return ""
    parts = []
    for field in ("output", "error", "prior_output"):
        value = result.get(field)
        if value in (None, ""):
            continue
        # The host's own "finished without a saved file" says nothing about why.
        if field == "error" and result.get("code") == "workflow_step_expectation_unmet":
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str))
    return "\n".join(parts)


async def workflow_step_connection_need(
    db: AsyncSession, *, run: object, step_id: str, user_id: str, evidence: str | None = None,
) -> dict | None:
    """The unconnected integration a failed Workflow step's Agent was waiting for.

    A step whose Agent's integration is not connected cannot reach it (its
    tools are not offered), so it ends without the action it owed. This names
    that integration from the same requirements the App's connections show,
    for the run to wait for it and continue from this step once connected.
    When the Agent has several unconnected integrations, the one the step's
    own result names (``evidence``, else the result the run stored for the
    step) is the one it waited for. A result that explains something else (a
    missing file, work outside the Agent's scope) is not waiting for a
    connection: connecting one would not change it.

    A step that stopped to ask a person said what it needs; that is the
    blocker, and it is a connection only when the ask names one.
    """
    workspace_id = getattr(run, "workspace_id", None)
    if not workspace_id or not step_id or not user_id:
        return None
    stored = getattr(run, "step_results", None)
    stored = stored.get(step_id) if isinstance(stored, dict) else None
    request = stored.get("needs_person") if isinstance(stored, dict) and evidence is None else None
    asked_person = isinstance(request, dict)
    if asked_person:
        evidence = " ".join(str(request.get(key) or "") for key in ("missing", "ask"))
    elif evidence is None:
        evidence = workflow_step_result_evidence(stored)
    from packages.core.services.workflow_run_trace import execution_snapshot_for_run

    # The frozen execution contract holds each step's config; the display
    # snapshot does not.
    snapshot = execution_snapshot_for_run(None, run) or {}
    node = next(
        (item for item in snapshot.get("steps") or [] if isinstance(item, dict) and str(item.get("id")) == step_id),
        None,
    )
    config = (node or {}).get("config") if isinstance((node or {}).get("config"), dict) else {}
    service_keys = {str(config.get("service_key") or "").strip()} - {""}
    agent_id = str(config.get("agent_id") or "").strip()
    if agent_id:
        service_keys.update((await db.execute(
            select(AgentSubscription.service_key).where(
                AgentSubscription.workspace_id == workspace_id,
                AgentSubscription.agent_id == agent_id,
                AgentSubscription.status == "active",
            )
        )).scalars().all())
    if not service_keys or any("{{" in key for key in service_keys):
        return None
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        return None
    status = await WorkspaceConnectionStatusFactory.create(db, workspace=workspace, user_id=user_id)
    needs = [
        {"provider": item["provider"], "label": str(item.get("label") or item["provider"])}
        for item in status["requirements"]
        if item["kind"] == BlueprintInstallRequirementKind.INTEGRATION.value
        and item["required"] and not item["ready"]
        and service_keys & set(item.get("service_keys") or [])
    ]
    text = evidence.lower()
    named = [need for need in needs if text and (need["label"].lower() in text or need["provider"].lower() in text)]
    if named:
        return named[0]
    # Staging 2026-11-08: a step that asked for real product photos was told to connect Facebook.
    if asked_person:
        return None
    # Staging 2026-11-05: "the file was not found" still waited for Shopify, with a connect card.
    if text.strip() and not any(word in text for word in _CONNECTION_WORDS):
        return None
    return needs[0] if needs else None
