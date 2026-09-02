"""Read-only Blueprint setup checks that run before Workspace mutation.

The Blueprint contract declares *what* the creator requires.  This module
resolves those declarations against the installing user's local connections;
it never copies credentials into a Blueprint or creates a Workspace.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.blueprints.payload import PayloadError, migrate_payload, validate_payload
from packages.core.constants.blueprints import BlueprintInstallRequirementKind
from packages.core.models.channel import ChannelConfig
from packages.core.models.document import Channel
from packages.core.models.integration_session import IntegrationSession
from packages.core.services.integration_resolution import integration_provider_readiness
from packages.core.services.provider_keys import canonical_provider_key


class BlueprintSetupPreflightError(ValueError):
    """The portable payload cannot be evaluated as an install contract."""


@dataclass(frozen=True)
class BlueprintSetupResourceOption:
    id: str
    label: str


@dataclass(frozen=True)
class BlueprintSetupRequirementState:
    kind: BlueprintInstallRequirementKind
    provider: str
    label: str
    required: bool
    ready: bool
    reason: str
    install_blocking: bool = True
    purpose: str | None = None
    setup_kind: str | None = None
    scope: str | None = None
    config_fields_to_set: tuple[str, ...] = ()
    requirement_key: str | None = None
    resource_id: str | None = None
    resource_options: tuple[BlueprintSetupResourceOption, ...] = ()


@dataclass(frozen=True)
class BlueprintSetupPreflight:
    requirements: tuple[BlueprintSetupRequirementState, ...] = field(
        default_factory=tuple,
    )

    @property
    def blocking_requirements(self) -> tuple[BlueprintSetupRequirementState, ...]:
        return tuple(
            requirement
            for requirement in self.requirements
            if (
                requirement.required
                and requirement.install_blocking
                and not requirement.ready
            )
        )

    @property
    def ready(self) -> bool:
        return not self.blocking_requirements

    def blocking_requirements_for_mode(
        self,
        mode: object,
    ) -> tuple[BlueprintSetupRequirementState, ...]:
        mode_value = str(getattr(mode, "value", mode) or "").strip().lower()
        if mode_value == "simulate":
            return ()
        return self.blocking_requirements

    def ready_for_mode(self, mode: object) -> bool:
        return not self.blocking_requirements_for_mode(mode)


class BlueprintSetupPreflightFactory:
    """Build the same account-level readiness view for install and upgrade."""

    @classmethod
    async def resolve_channel_selections(
        cls,
        db: AsyncSession,
        *,
        channels: list[dict[str, Any]],
        entity_id: str,
        user_id: str | None,
        selected: dict[str, str],
        workspace_id: str | None = None,
    ) -> dict[str, str]:
        """Lock and revalidate selections before creating any Workspace rows."""
        if selected:
            # Lock shared Workspace accounts too. Access is revalidated below
            # after any concurrent ownership change or install has completed.
            await db.execute(select(ChannelConfig.id).where(
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.id.in_(set(selected.values())),
            ).order_by(ChannelConfig.id).with_for_update())
        preflight = await cls.from_contract(
            db,
            contract={"channels": channels},
            entity_id=entity_id,
            user_id=user_id,
            selected_channel_config_ids=selected,
            workspace_id=workspace_id,
        )
        resolved = {
            item.requirement_key: item.resource_id
            for item in preflight.requirements
            if item.requirement_key and item.resource_id
        }
        if any(resolved.get(key) != value for key, value in selected.items()):
            raise BlueprintSetupPreflightError("Selected channel account is unavailable; refresh setup.")
        if not preflight.ready:
            raise BlueprintSetupPreflightError(" ".join(
                item.reason for item in preflight.blocking_requirements
            ))
        if len(set(resolved.values())) != len(resolved):
            raise BlueprintSetupPreflightError("Choose a separate account for each channel requirement.")
        # Lock auto-selected accounts too, then recheck after a concurrent
        # owner update or install has released its lock.
        if resolved != selected:
            return await cls.resolve_channel_selections(
                db, channels=channels, entity_id=entity_id, user_id=user_id,
                selected=resolved, workspace_id=workspace_id,
            )
        return resolved

    @classmethod
    async def from_payload(
        cls,
        db: AsyncSession,
        *,
        payload: dict[str, Any],
        entity_id: str,
        user_id: str | None,
        selected_channel_config_ids: dict[str, str] | None = None,
        workspace_id: str | None = None,
    ) -> BlueprintSetupPreflight:
        try:
            validate_payload(payload)
            canonical = migrate_payload(payload)
        except PayloadError as exc:
            raise BlueprintSetupPreflightError(
                f"invalid Blueprint payload: {exc}"
            ) from exc

        return await cls.from_contract(
            db,
            contract=canonical.get("contract") or {},
            entity_id=entity_id,
            user_id=user_id,
            selected_channel_config_ids=selected_channel_config_ids,
            workspace_id=workspace_id,
        )

    @classmethod
    async def from_contract(
        cls,
        db: AsyncSession,
        *,
        contract: dict[str, Any],
        entity_id: str,
        user_id: str | None,
        selected_channel_config_ids: dict[str, str] | None = None,
        workspace_id: str | None = None,
    ) -> BlueprintSetupPreflight:
        """Evaluate an already validated, credential-free contract subset."""
        integration_specs = [
            item
            for item in ((contract.get("requires") or {}).get("mcp_servers") or [])
            if isinstance(item, dict)
            and canonical_provider_key(item.get("slug"))
        ]
        specs_by_provider: dict[str, list[dict[str, Any]]] = {}
        for spec in integration_specs:
            provider = canonical_provider_key(spec.get("slug"))
            if provider:
                specs_by_provider.setdefault(provider, []).append(spec)
        # Readiness resolves both exact catalog keys and canonical provider
        # aliases. Preserve hyphens/case here so account-free servers can match.
        provider_keys = [
            str(spec.get("slug") or "").strip()
            for spec in integration_specs
        ]
        integration_states = await integration_provider_readiness(
            db,
            entity_id=entity_id,
            user_id=user_id,
            provider_keys=provider_keys,
        ) if provider_keys else {}

        requirements: list[BlueprintSetupRequirementState] = []
        for provider, provider_specs in specs_by_provider.items():
            spec = provider_specs[0]
            state = integration_states[provider]
            runtime_required = any(
                bool(provider_spec.get("required", True))
                for provider_spec in provider_specs
            )
            config_fields = tuple(dict.fromkeys(
                str(field_name).strip()
                for provider_spec in provider_specs
                for field_name in provider_spec.get("config_fields_to_set") or []
                if str(field_name).strip()
            ))
            requirements.append(BlueprintSetupRequirementState(
                kind=BlueprintInstallRequirementKind.INTEGRATION,
                provider=provider,
                label=str(
                    spec.get("display_name")
                    or spec.get("name")
                    or provider.replace("_", " ").title()
                ),
                required=runtime_required,
                ready=state.ready,
                reason=state.reason,
                install_blocking=any(
                    bool(provider_spec.get("required", True))
                    and bool(provider_spec.get("install_blocking", True))
                    for provider_spec in provider_specs
                ),
                purpose=next((
                    purpose
                    for provider_spec in provider_specs
                    if (purpose := _optional_text(provider_spec.get("purpose")))
                ), None),
                setup_kind=state.setup_kind,
                scope=state.scope,
                config_fields_to_set=config_fields,
            ))

        selected_channels = selected_channel_config_ids or {}
        from packages.core.services.workspace_readiness import (
            BUILT_IN_CHANNEL_TYPES,
        )

        for index, spec in enumerate(contract.get("channels") or []):
            if not isinstance(spec, dict):
                continue
            channel_type = str(spec.get("channel_type") or "").strip()
            if not channel_type:
                continue
            provider = str(spec.get("provider") or channel_type).strip()
            requirement_key = blueprint_channel_requirement_key(
                index,
                spec,
            )
            if channel_type in BUILT_IN_CHANNEL_TYPES:
                requirements.append(BlueprintSetupRequirementState(
                    kind=BlueprintInstallRequirementKind.CHANNEL,
                    provider=provider,
                    label=str(
                        spec.get("label")
                        or channel_type.replace("_", " ").title()
                    ),
                    required=bool(spec.get("required", True)),
                    ready=True,
                    reason="Built-in Workspace channel is available.",
                    purpose=_optional_text(spec.get("purpose")),
                    setup_kind="workspace_channel",
                    scope="workspace",
                    requirement_key=requirement_key,
                ))
                continue

            role = str(spec.get("role") or "").strip()
            purpose = str(spec.get("purpose") or "").strip()
            linked_service_key = str(
                spec.get("linked_service_key") or ""
            ).strip()
            statement = select(ChannelConfig).where(
                ChannelConfig.entity_id == entity_id,
                ChannelConfig.status == "active",
                ChannelConfig.channel_type == channel_type,
            )
            if spec.get("provider"):
                statement = statement.where(ChannelConfig.provider == provider)
            attached_ids: set[str] = set()
            matching_ids: set[str] = set()
            if workspace_id:
                # Existing authorized bindings grant account access regardless
                # of the new Blueprint's routing metadata. Matching metadata
                # is only a selection preference, not an authorization rule.
                binding_owner_filters = [
                    Channel.user_id.is_(None),
                    Channel.user_id == ChannelConfig.owner_user_id,
                ]
                if user_id:
                    binding_owner_filters.append(ChannelConfig.owner_user_id == user_id)
                bound_rows = (await db.execute(select(Channel).join(
                    ChannelConfig,
                    ChannelConfig.id == Channel.config["channel_config_id"].astext,
                ).where(
                    Channel.entity_id == entity_id,
                    Channel.workspace_id == workspace_id,
                    Channel.type == channel_type,
                    Channel.status == "active",
                    or_(*binding_owner_filters),
                ).execution_options(populate_existing=True))).scalars().all()
                for binding in bound_rows:
                    config = binding.config or {}
                    account_id = config["channel_config_id"]
                    attached_ids.add(account_id)
                    if all(
                        not expected or config.get(key) == expected
                        for key, expected in (
                            ("role", role), ("purpose", purpose),
                            ("linked_service_key", linked_service_key),
                        )
                    ):
                        matching_ids.add(account_id)
                account_filters = [ChannelConfig.id.in_(attached_ids)]
                if user_id:
                    account_filters.append(and_(
                        ChannelConfig.owner_user_id == user_id,
                        ChannelConfig.workspace_id.is_(None),
                    ))
                statement = statement.where(
                    or_(*account_filters),
                    or_(
                        ChannelConfig.workspace_id.is_(None),
                        ChannelConfig.workspace_id == workspace_id,
                    ),
                )
            elif user_id:
                statement = statement.where(
                    ChannelConfig.owner_user_id == user_id,
                    ChannelConfig.workspace_id.is_(None),
                )
            channel_rows = list((await db.execute(
                statement.order_by(ChannelConfig.created_at, ChannelConfig.id)
                .execution_options(populate_existing=True)
            )).scalars().all()) if workspace_id or user_id else []
            if channel_rows:
                binding_query = select(Channel.config["channel_config_id"].astext).where(
                    Channel.entity_id == entity_id,
                    Channel.type == channel_type,
                    Channel.status == "active",
                    Channel.config["channel_config_id"].astext.in_(
                        [row.id for row in channel_rows]
                    ),
                )
                if workspace_id:
                    binding_query = binding_query.where(
                        Channel.workspace_id.is_distinct_from(workspace_id)
                    )
                bound_elsewhere = set((await db.execute(binding_query)).scalars().all())
                if channel_type != "slack":
                    # Preserve explicitly shared existing routes on upgrade,
                    # but never add another default route implicitly.
                    bound_elsewhere.difference_update(attached_ids)
                channel_rows = [row for row in channel_rows if row.id not in bound_elsewhere]
            options = tuple(
                BlueprintSetupResourceOption(
                    id=row.id,
                    label=str(row.name or row.provider or row.channel_type),
                )
                for row in channel_rows
            )
            selected_id = str(selected_channels.get(requirement_key) or "").strip()
            if not selected_id and workspace_id:
                selected_id = next(
                    (row.id for row in channel_rows if row.id in matching_ids),
                    "",
                )
                if not selected_id:
                    attached = [row for row in channel_rows if row.id in attached_ids]
                    if len(attached) == 1:
                        selected_id = attached[0].id
            if not selected_id and channel_rows and len(channel_rows) == 1:
                selected_id = channel_rows[0].id
            selected_row = next(
                (row for row in channel_rows if row.id == selected_id),
                None,
            )
            if selected_row is not None:
                reason = (
                    "Channel account is attached to this Workspace."
                    if workspace_id else "Channel account is ready to bind during installation."
                )
            elif workspace_id:
                reason = f"Attach an active {channel_type} channel account to this Workspace."
            elif channel_rows:
                reason = f"Choose a {channel_type} channel account before installing."
            else:
                reason = f"Connect a {channel_type} channel account before installing."
            requirements.append(BlueprintSetupRequirementState(
                kind=BlueprintInstallRequirementKind.CHANNEL,
                provider=provider,
                label=str(
                    spec.get("label")
                    or channel_type.replace("_", " ").title()
                ),
                required=bool(spec.get("required", True)),
                ready=selected_row is not None,
                reason=reason,
                purpose=_optional_text(spec.get("purpose")),
                setup_kind="workspace_channel",
                scope="workspace" if workspace_id else "user",
                requirement_key=requirement_key,
                resource_id=selected_row.id if selected_row is not None else None,
                resource_options=options,
            ))

        for spec in contract.get("sessions") or []:
            if not isinstance(spec, dict):
                continue
            provider = str(spec.get("provider") or "").strip()
            if not provider:
                continue
            label = str(spec.get("label") or "").strip()
            statement = select(IntegrationSession.id).where(
                IntegrationSession.entity_id == entity_id,
                IntegrationSession.status == "active",
                IntegrationSession.provider == provider,
            )
            if label:
                statement = statement.where(IntegrationSession.label == label)
            ready = (
                await db.execute(statement.limit(1))
            ).scalar_one_or_none() is not None
            display_label = provider.replace("_", " ").title()
            if label:
                display_label = f"{display_label} · {label}"
            requirements.append(BlueprintSetupRequirementState(
                kind=BlueprintInstallRequirementKind.BROWSER_SESSION,
                provider=provider,
                label=display_label,
                required=bool(spec.get("required", True)),
                ready=ready,
                reason=(
                    "Browser session is active."
                    if ready
                    else f"Capture an active {display_label} browser session before installing."
                ),
                purpose=_optional_text(spec.get("purpose")),
                setup_kind="browser_session",
                scope="entity",
            ))

        return BlueprintSetupPreflight(requirements=tuple(requirements))


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def blueprint_channel_requirement_key(
    index: int,
    spec: dict[str, Any],
) -> str:
    """Stable request key for selecting a user's channel account."""

    channel_type = str(spec.get("channel_type") or "channel").strip()
    return f"channel:{index}:{channel_type}"
