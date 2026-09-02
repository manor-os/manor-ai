"""Resolve only the credential accounts an acting user is allowed to use."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import case, func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.integrations import (
    INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT,
    INTEGRATION_ACCOUNT_CONTINUATION_MAX_CHARS,
    INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE_KEY,
    INTEGRATION_ACCOUNT_SELECTION_ARGUMENT,
)
from packages.core.config import get_settings
from packages.core.models.document import Integration
from packages.core.models.permission import (
    Capability,
    GrantStatus,
    ResourceGrant,
    ResourceType,
    SubjectType,
)
from packages.core.models.user import OAuthAccount, User, UserMembership
from packages.core.permissions import user_has_permission
from packages.core.services.integration_access import resolve_integration_access
from packages.core.services.integration_health import is_credential_rejection
from packages.core.services.oauth_account_credentials import (
    oauth_account_is_runtime_usable,
    oauth_account_is_runtime_usable_clause,
)
from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
)

logger = logging.getLogger(__name__)

_INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE = INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE_KEY


class IntegrationAccountScope(str, Enum):
    """Credential ownership boundary for one callable integration account."""

    USER = "user"
    ENTITY = "entity"

    __str__ = str.__str__
    __format__ = str.__format__


class IntegrationAccountKind(str, Enum):
    """Persistence source for one callable integration account."""

    OAUTH_ACCOUNT = "oauth_account"
    INTEGRATION = "integration"

    __str__ = str.__str__
    __format__ = str.__format__


class IntegrationAccountOwnership(str, Enum):
    """Relationship between the acting user and the account owner."""

    MINE = "mine"
    SHARED = "shared"

    __str__ = str.__str__
    __format__ = str.__format__


class IntegrationAccountSelectionMode(str, Enum):
    """How one runtime operation chooses connected accounts."""

    DEFAULT = "default"
    EXACT = "exact"
    ALL = "all"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def resolve(
        cls,
        *,
        selector: str | None = None,
        requested: str | None = None,
    ) -> IntegrationAccountSelectionMode:
        selector_value = str(selector or "").strip()
        requested_value = str(requested or "").strip().lower()
        if requested_value:
            try:
                mode = cls(requested_value)
            except ValueError as exc:
                raise ValueError("integration_account_selection must be 'default', 'exact', or 'all'.") from exc
        else:
            mode = cls.EXACT if selector_value else cls.DEFAULT

        if selector_value and mode is cls.ALL:
            raise ValueError("integration_account_id cannot be combined with all-account selection.")
        if mode is cls.EXACT and not selector_value:
            raise ValueError("integration_account_id is required for exact account selection.")
        if selector_value:
            return cls.EXACT
        return mode


class IntegrationAccountFanoutStatus(str, Enum):
    """Completeness of one bounded all-account tool execution."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"

    __str__ = str.__str__
    __format__ = str.__format__


@dataclass(frozen=True)
class IntegrationAccountFanoutContinuation:
    """Verified account snapshot and offset for one bounded fan-out."""

    account_ids: tuple[str, ...]
    next_offset: int

    @property
    def remaining_account_ids(self) -> tuple[str, ...]:
        return self.account_ids[self.next_offset :]


class IntegrationAccountFanoutResultFactory:
    """Own the typed aggregate contract for bounded all-account calls."""

    SCHEMA_MARKER = "x-manor-integration-account-fanout"

    @staticmethod
    def _canonical_hash(value: object) -> str:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _encode_token_part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_token_part(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @classmethod
    def _sign_token_part(cls, payload: str) -> str:
        digest = hmac.new(
            get_settings().JWT_SECRET_KEY.encode("utf-8"),
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return cls._encode_token_part(digest)

    @classmethod
    def continuation_token(
        cls,
        *,
        server: str,
        tool: str,
        entity_id: str,
        user_id: str,
        account_ids: Iterable[str],
        next_offset: int,
        arguments: Mapping[str, Any],
    ) -> str:
        """Sign one continuation against its actor, query, and account order."""

        ordered_account_ids = [str(account_id) for account_id in account_ids]
        payload = {
            "v": 2,
            "server": str(server),
            "tool": str(tool),
            "entity_id": str(entity_id),
            "user_id": str(user_id),
            "account_ids": ordered_account_ids,
            "account_snapshot": cls._canonical_hash(ordered_account_ids),
            "account_count": len(ordered_account_ids),
            "next_offset": int(next_offset),
            "arguments": cls._canonical_hash(dict(arguments)),
        }
        encoded = cls._encode_token_part(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if len(encoded) + 44 > INTEGRATION_ACCOUNT_CONTINUATION_MAX_CHARS:
            raise ValueError("The integration-account continuation snapshot is too large.")
        return f"{encoded}.{cls._sign_token_part(encoded)}"

    @classmethod
    def continuation_snapshot(
        cls,
        token: str,
        *,
        server: str,
        tool: str,
        entity_id: str,
        user_id: str,
        current_account_ids: Iterable[str],
        arguments: Mapping[str, Any],
    ) -> IntegrationAccountFanoutContinuation | None:
        """Verify a continuation and recover its original account order."""

        if len(str(token)) > INTEGRATION_ACCOUNT_CONTINUATION_MAX_CHARS:
            return None
        try:
            encoded, signature = str(token).split(".", 1)
        except ValueError:
            return None
        if not encoded or not hmac.compare_digest(
            signature,
            cls._sign_token_part(encoded),
        ):
            return None
        try:
            payload = json.loads(cls._decode_token_part(encoded).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            return None
        expected = {
            "server": str(server),
            "tool": str(tool),
            "entity_id": str(entity_id),
            "user_id": str(user_id),
            "arguments": cls._canonical_hash(dict(arguments)),
        }
        if not isinstance(payload, dict) or any(
            payload.get(key) != value for key, value in expected.items()
        ):
            return None
        version = payload.get("v")
        if version == 2:
            raw_account_ids = payload.get("account_ids")
            if not isinstance(raw_account_ids, list):
                return None
            ordered_account_ids = tuple(
                str(account_id).strip() for account_id in raw_account_ids
            )
            if (
                not ordered_account_ids
                or any(not account_id for account_id in ordered_account_ids)
                or len(set(ordered_account_ids)) != len(ordered_account_ids)
            ):
                return None
        elif version == 1:
            # Rolling compatibility for tokens issued before the original
            # account order was embedded in the signed payload.
            ordered_account_ids = tuple(
                str(account_id) for account_id in current_account_ids
            )
        else:
            return None
        if (
            payload.get("account_snapshot")
            != cls._canonical_hash(ordered_account_ids)
            or payload.get("account_count") != len(ordered_account_ids)
        ):
            return None
        try:
            next_offset = int(payload.get("next_offset"))
        except (TypeError, ValueError):
            return None
        if next_offset < 0 or next_offset >= len(ordered_account_ids):
            return None
        return IntegrationAccountFanoutContinuation(
            account_ids=ordered_account_ids,
            next_offset=next_offset,
        )

    @staticmethod
    def continuation(
        *,
        account_id: str,
        remaining_account_count: int,
        reason: str,
        token: str,
    ) -> dict[str, Any]:
        """Build the public continuation receipt for one bounded fan-out."""

        return {
            "mode": IntegrationAccountSelectionMode.ALL.value,
            "argument": INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT,
            "token": token,
            # Keep the named fields during rolling upgrades. New callers should
            # treat ``token`` as opaque and send it through ``argument``.
            "cursor_account_id": account_id,
            "next_integration_account_id": account_id,
            "remaining_account_count": remaining_account_count,
            "reason": reason,
            "instruction": (
                "Call the same tool with integration_account_selection='all' "
                f"and {INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT}=token."
            ),
        }

    @classmethod
    def output_schema(
        cls,
        account_output_schema: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(account_output_schema, dict):
            return None
        if account_output_schema.get(cls.SCHEMA_MARKER) is True:
            return deepcopy(account_output_schema)
        return {
            cls.SCHEMA_MARKER: True,
            "type": "object",
            "required": [
                "integration_account_selection",
                "status",
                "total_account_count",
                "account_count",
                "failed_count",
                "result_truncated_count",
                "omitted_account_count",
                "results",
            ],
            "properties": {
                "server": {"type": "string"},
                "tool": {"type": "string"},
                "integration_account_selection": {
                    "const": IntegrationAccountSelectionMode.ALL.value,
                },
                "status": {
                    "type": "string",
                    "enum": [status.value for status in IntegrationAccountFanoutStatus],
                },
                "registry_status": {"type": "string"},
                "registry_errors": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "total_account_count": {"type": "integer", "minimum": 0},
                "account_count": {"type": "integer", "minimum": 0},
                "failed_count": {"type": "integer", "minimum": 0},
                "result_truncated_count": {"type": "integer", "minimum": 0},
                "omitted_account_count": {"type": "integer", "minimum": 0},
                "truncated": {"type": "boolean"},
                "deadline_exhausted": {"type": "boolean"},
                "continuation": {
                    "anyOf": [{"type": "object"}, {"type": "null"}],
                },
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["integration_account_id", "ok", "result"],
                        "properties": {
                            "integration_account_id": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "display_name": {"type": "string"},
                            "scope": {"type": "string"},
                            "is_default": {"type": "boolean"},
                            "ok": {"type": "boolean"},
                            "result_truncated": {"type": "boolean"},
                            "result": {
                                "anyOf": [
                                    deepcopy(account_output_schema),
                                    {
                                        "type": "object",
                                        "required": ["error"],
                                        "properties": {
                                            "error": {"type": "string"},
                                        },
                                    },
                                    {
                                        "type": "object",
                                        "required": ["truncated", "preview"],
                                        "properties": {
                                            "truncated": {"const": True},
                                            "preview": {"type": "string"},
                                        },
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        }

    @classmethod
    def input_schema(
        cls,
        account_input_schema: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Add Manor's ALL selector to a vendor-owned object contract."""

        if not isinstance(account_input_schema, dict):
            return None
        schema = deepcopy(account_input_schema)
        schema["type"] = "object"
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
            schema["properties"] = properties
        properties[INTEGRATION_ACCOUNT_SELECTION_ARGUMENT] = {
            "type": "string",
            "const": IntegrationAccountSelectionMode.ALL.value,
        }
        required = [
            str(name)
            for name in schema.get("required", [])
            if str(name or "").strip()
        ]
        if INTEGRATION_ACCOUNT_SELECTION_ARGUMENT not in required:
            required.append(INTEGRATION_ACCOUNT_SELECTION_ARGUMENT)
        schema["required"] = required
        return schema

    @classmethod
    def output_is_complete(cls, payload: object) -> bool:
        """Require terminal status and one successful receipt per account."""

        if not isinstance(payload, dict):
            return False
        results = payload.get("results")
        if not isinstance(results, list):
            return False
        counts: list[int] = []
        for key in (
            "total_account_count",
            "account_count",
            "failed_count",
            "result_truncated_count",
            "omitted_account_count",
        ):
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
            counts.append(value)
        total_count, account_count, failed_count, truncated_count, omitted_count = counts

        account_ids: list[str] = []
        for result in results:
            if not isinstance(result, dict):
                return False
            account_id = str(result.get("integration_account_id") or "").strip()
            if (
                not account_id
                or result.get("ok") is not True
                or bool(result.get("result_truncated"))
            ):
                return False
            account_ids.append(account_id)
        return (
            payload.get("integration_account_selection")
            == IntegrationAccountSelectionMode.ALL.value
            and payload.get("status") == IntegrationAccountFanoutStatus.COMPLETE.value
            and total_count == account_count == len(results)
            and len(set(account_ids)) == len(account_ids)
            and failed_count == 0
            and truncated_count == 0
            and omitted_count == 0
            and payload.get("continuation") is None
            and not bool(payload.get("truncated"))
            and not bool(payload.get("deadline_exhausted"))
        )

    @staticmethod
    def continuation_account_id(payload: object) -> str | None:
        if not isinstance(payload, dict):
            return None
        continuation = payload.get("continuation")
        if not isinstance(continuation, dict):
            return None
        account_id = str(
            continuation.get("token")
            or continuation.get("cursor_account_id")
            or continuation.get("next_integration_account_id")
            or ""
        ).strip()
        return account_id or None

    @classmethod
    def merge_pages(
        cls,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge one cursor page and recompute status from accepted receipts."""

        ordered: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        for payload in (previous, current):
            for raw in payload.get("results") or []:
                if not isinstance(raw, dict):
                    continue
                item = deepcopy(raw)
                account_id = str(item.get("integration_account_id") or "").strip()
                if account_id:
                    ordered[account_id] = item
                else:
                    anonymous.append(item)
        results = [*ordered.values(), *anonymous]
        failed_count = sum(item.get("ok") is False for item in results)
        truncated_count = sum(bool(item.get("result_truncated")) for item in results)
        total_count = max(
            int(previous.get("total_account_count") or 0),
            int(current.get("total_account_count") or 0),
        )
        omitted_count = max(
            0,
            int(current.get("omitted_account_count") or 0),
            total_count - len(results),
        )
        if results and failed_count == len(results) and omitted_count == 0:
            status = IntegrationAccountFanoutStatus.FAILED
        elif failed_count or truncated_count or omitted_count:
            status = IntegrationAccountFanoutStatus.PARTIAL
        else:
            status = IntegrationAccountFanoutStatus.COMPLETE

        merged = deepcopy(current)
        merged.update(
            {
                "integration_account_selection": IntegrationAccountSelectionMode.ALL.value,
                "status": status.value,
                "total_account_count": max(total_count, len(results) + omitted_count),
                "account_count": len(results),
                "failed_count": failed_count,
                "result_truncated_count": truncated_count,
                "omitted_account_count": omitted_count,
                "truncated": bool(truncated_count or omitted_count),
                "results": results,
            }
        )
        previous_errors = previous.get("registry_errors") or []
        current_errors = current.get("registry_errors") or []
        merged["registry_errors"] = list(
            dict.fromkeys([str(error) for error in [*previous_errors, *current_errors] if str(error)])
        )
        return merged


class IntegrationRegistryLoadStatus(str, Enum):
    """Completeness of an account-registry snapshot."""

    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"

    __str__ = str.__str__
    __format__ = str.__format__


class IntegrationAccountAvailability(str, Enum):
    """Whether a catalog account can be selected for a runtime operation."""

    CALLABLE = "callable"
    RECONNECT_REQUIRED = "reconnect_required"
    PERMISSION_DENIED = "permission_denied"
    LOAD_FAILED = "load_failed"

    __str__ = str.__str__
    __format__ = str.__format__


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationAccount:
    id: str
    provider: str
    kind: IntegrationAccountKind
    scope: IntegrationAccountScope
    ownership: IntegrationAccountOwnership
    owner_user_id: str | None
    display_name: str
    is_default: bool

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IntegrationAccountKind):
            object.__setattr__(self, "kind", IntegrationAccountKind(self.kind))
        if not isinstance(self.scope, IntegrationAccountScope):
            object.__setattr__(self, "scope", IntegrationAccountScope(self.scope))
        if not isinstance(self.ownership, IntegrationAccountOwnership):
            object.__setattr__(
                self,
                "ownership",
                IntegrationAccountOwnership(self.ownership),
            )

    def public_option(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "ownership": self.ownership.value,
            "is_default": self.is_default,
        }


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationAccountPreference:
    kind: IntegrationAccountKind
    account_id: str


class RuntimeIntegrationAccountPreferenceFactory:
    """Parse the entity/provider account pointer stored in user preferences."""

    @staticmethod
    def from_user_preferences(
        preferences: object,
        *,
        entity_id: str,
        provider_keys: Iterable[str] | None = None,
    ) -> dict[str, RuntimeIntegrationAccountPreference]:
        if not isinstance(preferences, dict):
            return {}
        all_defaults = preferences.get(_INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE)
        if not isinstance(all_defaults, dict):
            return {}
        entity_defaults = all_defaults.get(entity_id)
        if not isinstance(entity_defaults, dict):
            return {}
        requested = (
            {canonical for key in provider_keys if (canonical := canonical_provider_key(key))}
            if provider_keys is not None
            else None
        )
        parsed: dict[str, RuntimeIntegrationAccountPreference] = {}
        for raw_provider, raw_preference in entity_defaults.items():
            provider = canonical_provider_key(raw_provider)
            if (
                not provider
                or (requested is not None and provider not in requested)
                or not isinstance(raw_preference, dict)
            ):
                continue
            account_id = str(raw_preference.get("account_id") or "").strip()
            try:
                kind = IntegrationAccountKind(raw_preference.get("kind"))
            except (TypeError, ValueError):
                continue
            if account_id:
                parsed[provider] = RuntimeIntegrationAccountPreference(
                    kind=kind,
                    account_id=account_id,
                )
        return parsed


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationBinding:
    """One provider and every account callable in the current runtime scope."""

    provider: str
    accounts: tuple[RuntimeIntegrationAccount, ...] = ()
    configured_entity_account_count: int = 0
    credentialed_entity_account_count: int = 0
    denied_permissions: tuple[str, ...] = ()
    reconnect_required_account_ids: tuple[str, ...] = ()
    load_errors: tuple[str, ...] = ()
    preference: RuntimeIntegrationAccountPreference | None = None

    @property
    def connected(self) -> bool:
        return bool(self.accounts)

    @property
    def requires_explicit_account(self) -> bool:
        return bool(self.accounts and self.load_errors)

    @property
    def default_account_id(self) -> str | None:
        if self.requires_explicit_account:
            return None
        return self.accounts[0].id if self.accounts else None

    def public_option(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "connected": self.connected,
            "account_count": len(self.accounts),
            "default_account_id": self.default_account_id,
            **({"requires_explicit_account": True} if self.requires_explicit_account else {}),
            "account_options": [account.public_option() for account in self.accounts],
        }


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationRegistry:
    """Runtime snapshot of integrations available to one actor.

    ``covered_providers=None`` means the snapshot loaded every persisted
    provider for this actor/entity. A non-null set marks a deliberately partial
    snapshot, allowing permission checks to fall back to the database when a
    caller asks about a provider that was not loaded. Public projections expose
    account metadata only; internal ORM row references are never serialized.
    """

    user_id: str
    entity_id: str
    integrations: tuple[RuntimeIntegrationBinding, ...]
    covered_providers: frozenset[str] | None = None
    load_errors: tuple[str, ...] = ()

    def covers(self, provider: str) -> bool:
        canonical = canonical_provider_key(provider)
        return self.covered_providers is None or canonical in self.covered_providers

    def integration(self, provider: str) -> RuntimeIntegrationBinding | None:
        canonical = canonical_provider_key(provider)
        return next(
            (item for item in self.integrations if item.provider == canonical),
            None,
        )

    def accounts_for(self, provider: str) -> tuple[RuntimeIntegrationAccount, ...]:
        integration = self.integration(provider)
        return integration.accounts if integration else ()

    def public_catalog(self, *, connected_only: bool = False) -> list[dict[str, object]]:
        return [
            integration.public_option()
            for integration in self.integrations
            if not connected_only or integration.connected
        ]


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationRegistryLoadResult:
    """Observable result for callers that must fail closed on load errors."""

    status: IntegrationRegistryLoadStatus
    registry: RuntimeIntegrationRegistry | None = None
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status is IntegrationRegistryLoadStatus.READY and self.registry is not None


@dataclass(frozen=True, slots=True)
class IntegrationAccountCatalogAccount:
    """Credential-free management projection with explicit callability."""

    id: str
    provider: str
    kind: IntegrationAccountKind
    scope: IntegrationAccountScope
    ownership: IntegrationAccountOwnership
    owner_user_id: str | None
    display_name: str
    is_default: bool
    availability: IntegrationAccountAvailability

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IntegrationAccountKind):
            object.__setattr__(self, "kind", IntegrationAccountKind(self.kind))
        if not isinstance(self.scope, IntegrationAccountScope):
            object.__setattr__(self, "scope", IntegrationAccountScope(self.scope))
        if not isinstance(self.ownership, IntegrationAccountOwnership):
            object.__setattr__(
                self,
                "ownership",
                IntegrationAccountOwnership(self.ownership),
            )
        if not isinstance(self.availability, IntegrationAccountAvailability):
            object.__setattr__(
                self,
                "availability",
                IntegrationAccountAvailability(self.availability),
            )

    @property
    def runtime_callable(self) -> bool:
        return self.availability is IntegrationAccountAvailability.CALLABLE


class IntegrationAccountCatalogAccountFactory:
    """Build catalog records without retrying a failed runtime projection."""

    @staticmethod
    def from_runtime(
        account: RuntimeIntegrationAccount,
    ) -> IntegrationAccountCatalogAccount:
        return IntegrationAccountCatalogAccount(
            id=account.id,
            provider=account.provider,
            kind=account.kind,
            scope=account.scope,
            ownership=account.ownership,
            owner_user_id=account.owner_user_id,
            display_name=account.display_name,
            is_default=account.is_default,
            availability=IntegrationAccountAvailability.CALLABLE,
        )

    @staticmethod
    def from_oauth_management(
        row: OAuthAccount,
        *,
        actor_user_id: str,
        provider: str,
        availability: IntegrationAccountAvailability,
    ) -> IntegrationAccountCatalogAccount:
        ownership = (
            IntegrationAccountOwnership.MINE if row.user_id == actor_user_id else IntegrationAccountOwnership.SHARED
        )
        return IntegrationAccountCatalogAccount(
            id=row.id,
            provider=canonical_provider_key(provider or row.provider),
            kind=IntegrationAccountKind.OAUTH_ACCOUNT,
            scope=IntegrationAccountScope.USER,
            ownership=ownership,
            owner_user_id=row.user_id,
            display_name=oauth_account_display_name(row),
            # An unavailable row must never become an implicit runtime choice.
            is_default=False,
            availability=availability,
        )

    @staticmethod
    def from_entity_management(
        row: Integration,
        *,
        actor_user_id: str,
        provider: str,
        availability: IntegrationAccountAvailability,
    ) -> IntegrationAccountCatalogAccount:
        ownership = (
            IntegrationAccountOwnership.MINE
            if row.owner_user_id == actor_user_id
            else IntegrationAccountOwnership.SHARED
        )
        return IntegrationAccountCatalogAccount(
            id=row.id,
            provider=canonical_provider_key(provider or row.provider),
            kind=IntegrationAccountKind.INTEGRATION,
            scope=IntegrationAccountScope.ENTITY,
            ownership=ownership,
            owner_user_id=row.owner_user_id,
            display_name=entity_account_display_name(row),
            # An unavailable row must never become an implicit runtime choice.
            is_default=False,
            availability=availability,
        )


@dataclass(frozen=True, slots=True)
class IntegrationAccountCatalogBinding:
    """Catalog projection for one provider, including load completeness."""

    provider: str
    accounts: tuple[IntegrationAccountCatalogAccount, ...]
    status: IntegrationRegistryLoadStatus
    errors: tuple[str, ...] = ()

    @property
    def requires_explicit_account(self) -> bool:
        return self.status is IntegrationRegistryLoadStatus.PARTIAL


@dataclass(frozen=True, slots=True)
class IntegrationAccountCatalogSnapshot:
    """Typed catalog snapshot that never overloads an empty account list."""

    integrations: tuple[IntegrationAccountCatalogBinding, ...]

    def integration(self, provider: str) -> IntegrationAccountCatalogBinding | None:
        canonical = canonical_provider_key(provider)
        return next(
            (item for item in self.integrations if item.provider == canonical),
            None,
        )

    def accounts_for(
        self,
        provider: str,
    ) -> tuple[IntegrationAccountCatalogAccount, ...]:
        integration = self.integration(provider)
        return integration.accounts if integration else ()


class IntegrationAccountCatalogSnapshotFactory:
    """Build a catalog snapshot from the runtime authorization boundary."""

    @staticmethod
    def create(
        registry: RuntimeIntegrationRegistry,
        *,
        accounts_by_provider: dict[str, list[IntegrationAccountCatalogAccount]],
        extra_errors_by_provider: dict[str, list[str]],
    ) -> IntegrationAccountCatalogSnapshot:
        registry_bindings = {binding.provider: binding for binding in registry.integrations}
        providers = set(registry_bindings) | set(accounts_by_provider)
        integrations: list[IntegrationAccountCatalogBinding] = []
        for provider in sorted(providers):
            binding = registry_bindings.get(provider)
            errors = list(binding.load_errors if binding else ())
            for error in extra_errors_by_provider.get(provider, []):
                if error not in errors:
                    errors.append(error)
            status = IntegrationRegistryLoadStatus.PARTIAL if errors else IntegrationRegistryLoadStatus.READY
            integrations.append(
                IntegrationAccountCatalogBinding(
                    provider=provider,
                    accounts=tuple(accounts_by_provider.get(provider, [])),
                    status=status,
                    errors=tuple(errors),
                )
            )
        return IntegrationAccountCatalogSnapshot(tuple(integrations))


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationAccountCallPlan:
    mode: IntegrationAccountSelectionMode
    accounts: tuple[RuntimeIntegrationAccount, ...]


class RuntimeIntegrationAccountCallPlanFactory:
    """Build a deterministic account fan-out plan from one registry binding."""

    @staticmethod
    def create(
        accounts: Iterable[RuntimeIntegrationAccount],
        *,
        selector: str | None = None,
        selection: str | None = None,
        allowed_account_ids: Iterable[str] | None = None,
    ) -> RuntimeIntegrationAccountCallPlan:
        ordered = tuple(accounts)
        if allowed_account_ids is not None:
            allowed = {str(account_id).strip() for account_id in allowed_account_ids if str(account_id or "").strip()}
            ordered = tuple(account for account in ordered if account.id in allowed)
        mode = IntegrationAccountSelectionMode.resolve(
            selector=selector,
            requested=selection,
        )
        if mode is IntegrationAccountSelectionMode.ALL:
            selected = ordered
        elif mode is IntegrationAccountSelectionMode.DEFAULT:
            selected = ordered[:1]
        else:
            selected = tuple(account for account in ordered if account.id == str(selector or "").strip())
        return RuntimeIntegrationAccountCallPlan(mode=mode, accounts=selected)


def _first_text(*values: object) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def oauth_account_display_name(row: OAuthAccount) -> str:
    profile = row.profile if isinstance(row.profile, dict) else {}
    return (
        _first_text(
            profile.get("email"),
            profile.get("display_name"),
            profile.get("name"),
            row.provider_user_id,
        )
        or f"{row.provider} account {row.id[-6:]}"
    )


def entity_account_display_name(row: Integration) -> str:
    """Return an account label derived only from non-secret configuration."""
    cfg = row.config if isinstance(row.config, dict) else {}
    profile = cfg.get("profile") if isinstance(cfg.get("profile"), dict) else {}
    return (
        _first_text(
            cfg.get("name"),
            profile.get("email"),
            profile.get("display_name"),
            profile.get("name"),
            cfg.get("display_name"),
            cfg.get("email"),
            cfg.get("from_address"),
            cfg.get("from_email"),
            cfg.get("username"),
            cfg.get("phone_number"),
        )
        or f"{row.provider} account {row.id[-6:]}"
    )


def entity_account_has_credentials(row: Integration) -> bool:
    return bool(row.credentials or row.credential_ref or entity_account_nango_connection_id(row))


def entity_account_nango_connection_id(row: Integration) -> str | None:
    cfg = row.config if isinstance(row.config, dict) else {}
    nango = cfg.get("nango")
    if not isinstance(nango, dict):
        return None
    connection_id = str(nango.get("connection_id") or "").strip()
    return connection_id or None


def entity_account_nango_provider_config_key(row: Integration) -> str:
    cfg = row.config if isinstance(row.config, dict) else {}
    nango = cfg.get("nango")
    if not isinstance(nango, dict):
        return canonical_provider_key(row.provider)
    return str(nango.get("provider_config_key") or row.provider or "").strip()


@dataclass(frozen=True, slots=True)
class NangoConnectionPointer:
    entity_id: str
    owner_user_id: str
    provider_config_key: str
    nonce: str


class NangoConnectionPointerFactory:
    """Parse the server-issued identity encoded in a Nango connection id."""

    @staticmethod
    def from_connection_id(connection_id: object) -> NangoConnectionPointer | None:
        parts = str(connection_id or "").strip().split("--", 3)
        if len(parts) != 4 or any(not part.strip() for part in parts):
            return None
        return NangoConnectionPointer(
            entity_id=parts[0].strip(),
            owner_user_id=parts[1].strip(),
            provider_config_key=parts[2].strip(),
            nonce=parts[3].strip(),
        )


def nango_connection_matches_runtime_scope(
    *,
    connection_id: str,
    entity_id: str,
    owner_user_id: str | None,
    provider: str,
    provider_config_key: str,
) -> bool:
    """Validate the tenant, owner, and provider encoded in a Nango pointer."""
    pointer = NangoConnectionPointerFactory.from_connection_id(connection_id)
    normalized_entity_id = str(entity_id or "").strip()
    normalized_owner_user_id = str(owner_user_id or "").strip()
    if (
        pointer is None
        or not normalized_entity_id
        or not normalized_owner_user_id
        or pointer.entity_id != normalized_entity_id
        or pointer.owner_user_id != normalized_owner_user_id
    ):
        return False
    canonical_provider = canonical_provider_key(provider)
    return (
        bool(canonical_provider)
        and canonical_provider_key(pointer.provider_config_key) == canonical_provider
        and canonical_provider_key(provider_config_key) == canonical_provider
    )


def entity_account_credentials_rejected(row: Integration) -> bool:
    cfg = row.config if isinstance(row.config, dict) else {}
    health = cfg.get("last_health_check")
    return bool(
        isinstance(health, dict) and health.get("ok") is False and is_credential_rejection(health.get("detail"))
    )


class RuntimeIntegrationAccountFactory:
    """Convert persistence rows into credential-free runtime account values."""

    @staticmethod
    def from_oauth(
        row: OAuthAccount,
        *,
        actor_user_id: str | None = None,
        provider: str | None = None,
    ) -> RuntimeIntegrationAccount:
        canonical = canonical_provider_key(provider or row.provider)
        profile = row.profile if isinstance(row.profile, dict) else {}
        ownership = (
            IntegrationAccountOwnership.MINE
            if actor_user_id is None or row.user_id == actor_user_id
            else IntegrationAccountOwnership.SHARED
        )
        return RuntimeIntegrationAccount(
            id=row.id,
            provider=canonical,
            kind=IntegrationAccountKind.OAUTH_ACCOUNT,
            scope=IntegrationAccountScope.USER,
            ownership=ownership,
            owner_user_id=row.user_id,
            display_name=oauth_account_display_name(row),
            is_default=(ownership is IntegrationAccountOwnership.MINE and bool(profile.get("is_default"))),
        )

    @staticmethod
    def from_entity(
        row: Integration,
        *,
        actor_user_id: str | None = None,
        provider: str | None = None,
    ) -> RuntimeIntegrationAccount:
        canonical = canonical_provider_key(provider or row.provider)
        cfg = row.config if isinstance(row.config, dict) else {}
        ownership = (
            IntegrationAccountOwnership.MINE
            if actor_user_id is None or row.owner_user_id == actor_user_id
            else IntegrationAccountOwnership.SHARED
        )
        return RuntimeIntegrationAccount(
            id=row.id,
            provider=canonical,
            kind=IntegrationAccountKind.INTEGRATION,
            scope=IntegrationAccountScope.ENTITY,
            ownership=ownership,
            owner_user_id=row.owner_user_id,
            display_name=entity_account_display_name(row),
            is_default=(ownership is IntegrationAccountOwnership.MINE and bool(cfg.get("is_default"))),
        )


def _integration_account_scope_lock_key(
    *,
    user_id: str | None,
    entity_id: str,
    provider: str,
) -> int:
    owner_scope = f"{entity_id}\0{user_id or '<unowned>'}"
    digest = hashlib.sha256(
        (f"integration-account-default\0{owner_scope}\0{canonical_provider_key(provider)}").encode()
    ).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


async def lock_runtime_integration_account_scope(
    db: AsyncSession,
    *,
    kind: IntegrationAccountKind | str,
    user_id: str | None,
    entity_id: str,
    provider: str,
) -> None:
    """Serialize cross-storage default writes for one actor/entity/provider."""
    IntegrationAccountKind(kind)
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql":
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    _integration_account_scope_lock_key(
                        user_id=user_id,
                        entity_id=entity_id,
                        provider=provider,
                    )
                )
            )
        )


async def _load_runtime_integration_account_preferences(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider_keys: Iterable[str] | None = None,
) -> dict[str, RuntimeIntegrationAccountPreference]:
    rows = list((await db.execute(select(User.preferences).where(User.id == user_id))).scalars().all())
    return RuntimeIntegrationAccountPreferenceFactory.from_user_preferences(
        rows[0] if rows else {},
        entity_id=entity_id,
        provider_keys=provider_keys,
    )


async def _write_runtime_integration_account_preference(
    db: AsyncSession,
    *,
    user_id: str | None,
    entity_id: str,
    provider: str,
    preference: RuntimeIntegrationAccountPreference | None,
) -> None:
    if not user_id:
        return
    provider = canonical_provider_key(provider)
    path = array(
        [
            _INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE,
            entity_id,
            provider,
        ]
    )
    get_bind = getattr(db, "get_bind", None)
    bind = get_bind() if callable(get_bind) else None
    if bind is not None and bind.dialect.name == "postgresql":
        empty = literal({}, type_=JSONB)
        current = func.coalesce(User.preferences, empty)
        if preference is None:
            updated_preferences = current.op("#-")(path)
        else:
            root_path = array([_INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE])
            entity_path = array(
                [
                    _INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE,
                    entity_id,
                ]
            )
            current_root = current.op("#>")(root_path)
            current_entity = current.op("#>")(entity_path)
            with_root = func.jsonb_set(
                current,
                root_path,
                case(
                    (func.jsonb_typeof(current_root) == "object", current_root),
                    else_=empty,
                ),
                True,
            )
            with_entity = func.jsonb_set(
                with_root,
                entity_path,
                case(
                    (
                        func.jsonb_typeof(current_entity) == "object",
                        current_entity,
                    ),
                    else_=empty,
                ),
                True,
            )
            updated_preferences = func.jsonb_set(
                with_entity,
                path,
                literal(
                    {
                        "kind": preference.kind.value,
                        "account_id": preference.account_id,
                    },
                    type_=JSONB,
                ),
                True,
            )
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(preferences=updated_preferences)
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        return

    user = await db.get(User, user_id)
    if user is None:
        return
    preferences = dict(user.preferences or {})
    raw_defaults = preferences.get(_INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE)
    all_defaults = dict(raw_defaults) if isinstance(raw_defaults, dict) else {}
    raw_entity_defaults = all_defaults.get(entity_id)
    entity_defaults = dict(raw_entity_defaults) if isinstance(raw_entity_defaults, dict) else {}
    if preference is None:
        entity_defaults.pop(provider, None)
    else:
        entity_defaults[provider] = {
            "kind": preference.kind.value,
            "account_id": preference.account_id,
        }
    all_defaults[entity_id] = entity_defaults
    preferences[_INTEGRATION_ACCOUNT_DEFAULTS_PREFERENCE] = all_defaults
    user.preferences = preferences
    await db.flush()


async def _write_runtime_integration_account_default(
    db: AsyncSession,
    *,
    model,
    json_column,
    owner_scope: tuple,
    account_id: str,
) -> None:
    patch = case(
        (
            model.id == account_id,
            literal({"is_default": True}, type_=JSONB),
        ),
        else_=literal({"is_default": False}, type_=JSONB),
    )
    current = func.coalesce(json_column, literal({}, type_=JSONB))
    statement = (
        update(model)
        .where(*owner_scope)
        .values({json_column: current.op("||")(patch)})
        .execution_options(synchronize_session=False)
    )
    await db.execute(statement)
    await db.flush()


async def set_default_runtime_integration_account(
    db: AsyncSession,
    *,
    kind: IntegrationAccountKind | str,
    user_id: str,
    entity_id: str,
    provider: str,
    account_id: str,
) -> bool:
    """Atomically prefer one callable account for the acting user.

    The actor/provider lock serializes the per-user preference. Legacy sibling
    flags are synchronized only when the actor owns the selected account;
    selecting a shared account never mutates its owner's row.
    """
    resolved_kind = IntegrationAccountKind(kind)
    canonical_provider = canonical_provider_key(provider)
    await lock_runtime_integration_account_scope(
        db,
        kind=resolved_kind,
        user_id=user_id,
        entity_id=entity_id,
        provider=provider,
    )
    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=[canonical_provider],
    )
    target = next(
        (
            account
            for account in registry.accounts_for(canonical_provider)
            if account.kind is resolved_kind and account.id == account_id
        ),
        None,
    )
    if target is None:
        return False

    if target.ownership is IntegrationAccountOwnership.MINE:
        aliases = provider_key_aliases(canonical_provider)
        if resolved_kind is IntegrationAccountKind.OAUTH_ACCOUNT:
            model = OAuthAccount
            json_column = OAuthAccount.profile
            owner_scope = (
                OAuthAccount.user_id == user_id,
                OAuthAccount.provider.in_(aliases),
            )
        else:
            model = Integration
            json_column = Integration.config
            owner_scope = (
                Integration.entity_id == entity_id,
                Integration.owner_user_id == user_id,
                Integration.provider.in_(aliases),
                Integration.status == "active",
            )
        await _write_runtime_integration_account_default(
            db,
            model=model,
            json_column=json_column,
            owner_scope=owner_scope,
            account_id=account_id,
        )

    await _write_runtime_integration_account_preference(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider=canonical_provider,
        preference=RuntimeIntegrationAccountPreference(
            kind=resolved_kind,
            account_id=account_id,
        ),
    )
    return True


async def _load_preferred_shared_runtime_account(
    db: AsyncSession,
    *,
    preference: RuntimeIntegrationAccountPreference | None,
    user_id: str | None,
    entity_id: str,
    provider: str,
) -> RuntimeIntegrationAccount | None:
    """Resolve an explicit shared default without promoting unrelated rows."""
    if preference is None or not user_id:
        return None
    aliases = provider_key_aliases(canonical_provider_key(provider))
    if preference.kind is IntegrationAccountKind.OAUTH_ACCOUNT:
        row = (
            await db.execute(
                select(OAuthAccount).where(
                    OAuthAccount.id == preference.account_id,
                    OAuthAccount.provider.in_(aliases),
                )
            )
        ).scalar_one_or_none()
        if row is None or row.user_id == user_id or not oauth_account_is_runtime_usable(row):
            return None
        access = await resolve_integration_access(
            db,
            kind="oauth_account",
            connection_id=row.id,
            entity_id=entity_id,
            user_id=user_id,
            action="use",
        )
        if not access.allowed:
            return None
        return RuntimeIntegrationAccountFactory.from_oauth(
            row,
            actor_user_id=user_id,
            provider=provider,
        )

    if preference.kind is not IntegrationAccountKind.INTEGRATION:
        return None
    row = (
        await db.execute(
            select(Integration).where(
                Integration.id == preference.account_id,
                Integration.entity_id == entity_id,
                Integration.provider.in_(aliases),
                Integration.status == "active",
            )
        )
    ).scalar_one_or_none()
    if (
        row is None
        or row.owner_user_id == user_id
        or not entity_account_has_credentials(row)
        or entity_account_credentials_rejected(row)
    ):
        return None
    access = await resolve_integration_access(
        db,
        kind="integration",
        connection_id=row.id,
        entity_id=entity_id,
        user_id=user_id,
        action="use",
    )
    if not access.allowed:
        return None
    if row.required_permission and not await user_has_permission(
        db,
        user_id,
        entity_id,
        str(row.required_permission),
    ):
        return None
    return RuntimeIntegrationAccountFactory.from_entity(
        row,
        actor_user_id=user_id,
        provider=provider,
    )


async def normalize_runtime_integration_account_defaults(
    db: AsyncSession,
    *,
    kind: IntegrationAccountKind | str,
    user_id: str | None,
    entity_id: str,
    provider: str,
) -> str | None:
    """Keep one provider-wide default across OAuth and entity accounts."""
    resolved_kind = IntegrationAccountKind(kind)
    aliases = provider_key_aliases(canonical_provider_key(provider))
    await lock_runtime_integration_account_scope(
        db,
        kind=resolved_kind,
        user_id=user_id,
        entity_id=entity_id,
        provider=provider,
    )
    if resolved_kind is IntegrationAccountKind.OAUTH_ACCOUNT:
        model = OAuthAccount
        json_column = OAuthAccount.profile
        owner_scope = (
            OAuthAccount.user_id == user_id,
            OAuthAccount.provider.in_(aliases),
        )
    else:
        model = Integration
        json_column = Integration.config
        owner_scope = (
            Integration.entity_id == entity_id,
            Integration.owner_user_id == user_id,
            Integration.provider.in_(aliases),
            Integration.status == "active",
        )

    rows = list(
        (await db.execute(select(model).where(*owner_scope).order_by(model.created_at.desc(), model.id.desc())))
        .scalars()
        .all()
    )
    if rows:
        chosen_for_kind = next(
            (
                row
                for row in rows
                if isinstance(getattr(row, json_column.key), dict)
                and bool(getattr(row, json_column.key).get("is_default"))
            ),
            rows[0],
        )
        await _write_runtime_integration_account_default(
            db,
            model=model,
            json_column=json_column,
            owner_scope=owner_scope,
            account_id=chosen_for_kind.id,
        )

    oauth_rows = list(
        (
            await db.execute(
                select(OAuthAccount)
                .where(
                    OAuthAccount.user_id == user_id,
                    OAuthAccount.provider.in_(aliases),
                )
                .order_by(OAuthAccount.created_at.desc(), OAuthAccount.id.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    integration_rows = list(
        (
            await db.execute(
                select(Integration)
                .where(
                    Integration.entity_id == entity_id,
                    Integration.owner_user_id == user_id,
                    Integration.provider.in_(aliases),
                    Integration.status == "active",
                )
                .order_by(Integration.created_at.desc(), Integration.id.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    accounts: list[RuntimeIntegrationAccount] = []
    for row in oauth_rows:
        try:
            if not oauth_account_is_runtime_usable(row):
                continue
            accounts.append(
                RuntimeIntegrationAccountFactory.from_oauth(
                    row,
                    actor_user_id=user_id or "",
                    provider=provider,
                )
            )
        except Exception:
            logger.exception(
                "Ignoring malformed OAuth account %s while normalizing %s",
                row.id,
                provider,
            )
    permission_cache: dict[str, bool] = {}
    for row in integration_rows:
        try:
            if not entity_account_has_credentials(row):
                continue
            if row.required_permission:
                permission = str(row.required_permission)
                if not user_id:
                    continue
                if permission not in permission_cache:
                    permission_cache[permission] = await user_has_permission(
                        db,
                        user_id,
                        entity_id,
                        permission,
                    )
                if not permission_cache[permission]:
                    continue
            accounts.append(
                RuntimeIntegrationAccountFactory.from_entity(
                    row,
                    actor_user_id=user_id or "",
                    provider=provider,
                )
            )
        except Exception:
            logger.exception(
                "Ignoring malformed entity account %s while normalizing %s",
                row.id,
                provider,
            )
    preferences = (
        await _load_runtime_integration_account_preferences(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider_keys=[provider],
        )
        if user_id
        else {}
    )
    preference = preferences.get(canonical_provider_key(provider))
    preferred_shared = await _load_preferred_shared_runtime_account(
        db,
        preference=preference,
        user_id=user_id,
        entity_id=entity_id,
        provider=provider,
    )
    if preferred_shared is not None:
        accounts.append(preferred_shared)
    ordered = _ordered_accounts(
        accounts,
        preference=preference,
    )
    selected = ordered[0] if ordered else None
    await _write_runtime_integration_account_preference(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider=provider,
        preference=(
            RuntimeIntegrationAccountPreference(
                kind=selected.kind,
                account_id=selected.id,
            )
            if selected is not None
            else None
        ),
    )
    return selected.id if selected is not None else None


def _ordered_accounts(
    accounts: Iterable[RuntimeIntegrationAccount],
    *,
    preference: RuntimeIntegrationAccountPreference | None = None,
    mark_effective_default: bool = True,
) -> tuple[RuntimeIntegrationAccount, ...]:
    base_order = sorted(
        accounts,
        key=lambda account: (
            0 if account.ownership is IntegrationAccountOwnership.MINE else 1,
            0 if account.scope is IntegrationAccountScope.USER else 1,
            0 if account.is_default else 1,
        ),
    )
    preferred = next(
        (
            account
            for account in base_order
            if (preference is not None and account.kind is preference.kind and account.id == preference.account_id)
        ),
        None,
    )
    selected = preferred or next(
        (
            account
            for account in base_order
            if (account.ownership is IntegrationAccountOwnership.MINE and account.is_default)
        ),
        base_order[0] if base_order else None,
    )
    ordered = (
        [selected, *(account for account in base_order if account is not selected)] if selected is not None else []
    )
    return tuple(
        replace(
            account,
            is_default=mark_effective_default and index == 0,
        )
        for index, account in enumerate(ordered)
    )


async def _load_runtime_integration_registry(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider_keys: Iterable[str] | None = None,
    live_nango_connection_ids: set[str] | frozenset[str] | None = None,
) -> RuntimeIntegrationRegistry:
    """Load all callable account bindings for one user/entity in bulk.

    This is the canonical runtime layer behind discovery and dispatch. It
    returns credential-free account metadata only; credentials are leased later
    for the exact selected account at call time.
    """
    requested = None
    if provider_keys is not None:
        requested = frozenset(canonical for key in provider_keys if (canonical := canonical_provider_key(key)))
        if not requested:
            return RuntimeIntegrationRegistry(
                user_id=user_id,
                entity_id=entity_id,
                integrations=(),
                covered_providers=requested,
            )

    storage_keys: set[str] | None = None
    if requested is not None:
        storage_keys = {alias for provider in requested for alias in provider_key_aliases(provider)}

    oauth_ids = await _shared_resource_ids(
        db,
        entity_id=entity_id,
        user_id=user_id,
        resource_type=ResourceType.OAUTH_ACCOUNT,
    )
    integration_ids = await _shared_resource_ids(
        db,
        entity_id=entity_id,
        user_id=user_id,
        resource_type=ResourceType.INTEGRATION,
    )

    oauth_query = select(OAuthAccount).where(
        (OAuthAccount.user_id == user_id) | OAuthAccount.id.in_(oauth_ids),
        oauth_account_is_runtime_usable_clause(),
    )
    entity_query = select(Integration).where(
        Integration.entity_id == entity_id,
        Integration.status == "active",
        (Integration.owner_user_id == user_id) | Integration.id.in_(integration_ids),
    )
    if storage_keys is not None:
        oauth_query = oauth_query.where(OAuthAccount.provider.in_(storage_keys))
        entity_query = entity_query.where(Integration.provider.in_(storage_keys))

    oauth_rows = list((await db.execute(oauth_query.order_by(OAuthAccount.created_at.desc()))).scalars().all())
    entity_rows = list((await db.execute(entity_query.order_by(Integration.created_at.desc()))).scalars().all())
    preferences = await _load_runtime_integration_account_preferences(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=requested,
    )
    shared_owner_ids = {row.user_id for row in oauth_rows if row.user_id != user_id} | {
        row.owner_user_id for row in entity_rows if row.owner_user_id and row.owner_user_id != user_id
    }
    active_shared_owner_ids = await _active_entity_member_ids(
        db,
        entity_id=entity_id,
        user_ids=shared_owner_ids,
    )

    oauth_by_provider: dict[str, list[OAuthAccount]] = defaultdict(list)
    entity_by_provider: dict[str, list[Integration]] = defaultdict(list)
    for row in oauth_rows:
        provider = canonical_provider_key(row.provider)
        if provider and (requested is None or provider in requested):
            oauth_by_provider[provider].append(row)
    for row in entity_rows:
        provider = canonical_provider_key(row.provider)
        if provider and (requested is None or provider in requested):
            entity_by_provider[provider].append(row)

    providers = set(requested or ())
    providers.update(oauth_by_provider)
    providers.update(entity_by_provider)
    permission_cache: dict[str, bool] = {}
    integrations: list[RuntimeIntegrationBinding] = []
    load_errors: list[str] = []

    for provider in sorted(providers):
        accounts: list[RuntimeIntegrationAccount] = []
        provider_load_errors: list[str] = []
        for row in oauth_by_provider.get(provider, []):
            try:
                if row.user_id != user_id and row.user_id not in active_shared_owner_ids:
                    continue
                accounts.append(
                    RuntimeIntegrationAccountFactory.from_oauth(
                        row,
                        actor_user_id=user_id,
                        provider=provider,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to load runtime OAuth account for provider %s",
                    provider,
                )
                error = f"{provider}:oauth_account_load_failed"
                if error not in provider_load_errors:
                    provider_load_errors.append(error)

        provider_entity_rows = entity_by_provider.get(provider, [])
        credentialed_count = 0
        denied_permissions: list[str] = []
        reconnect_required_account_ids: list[str] = []
        for row in provider_entity_rows:
            try:
                has_credentials = entity_account_has_credentials(row)
            except Exception:
                logger.exception(
                    "Failed to inspect runtime entity account for provider %s",
                    provider,
                )
                error = f"{provider}:entity_account_load_failed"
                if error not in provider_load_errors:
                    provider_load_errors.append(error)
                continue
            if not has_credentials:
                continue
            credentialed_count += 1
            nango_connection_id = entity_account_nango_connection_id(row)
            if entity_account_credentials_rejected(row):
                reconnect_required_account_ids.append(row.id)
                continue
            if nango_connection_id and (
                not nango_connection_matches_runtime_scope(
                    connection_id=nango_connection_id,
                    entity_id=entity_id,
                    owner_user_id=row.owner_user_id,
                    provider=provider,
                    provider_config_key=(entity_account_nango_provider_config_key(row)),
                )
                or (live_nango_connection_ids is not None and nango_connection_id not in live_nango_connection_ids)
            ):
                reconnect_required_account_ids.append(row.id)
                continue
            if row.owner_user_id != user_id:
                if row.owner_user_id not in active_shared_owner_ids:
                    continue
            if row.required_permission:
                permission = str(row.required_permission)
                try:
                    if permission not in permission_cache:
                        permission_cache[permission] = await user_has_permission(
                            db,
                            user_id,
                            entity_id,
                            permission,
                        )
                except Exception:
                    logger.exception(
                        "Failed to check runtime integration permission for provider %s",
                        provider,
                    )
                    error = f"{provider}:permission_check_failed"
                    if error not in provider_load_errors:
                        provider_load_errors.append(error)
                    continue
                if not permission_cache[permission]:
                    if permission not in denied_permissions:
                        denied_permissions.append(permission)
                    continue
            try:
                accounts.append(
                    RuntimeIntegrationAccountFactory.from_entity(
                        row,
                        actor_user_id=user_id,
                        provider=provider,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to load runtime entity account for provider %s",
                    provider,
                )
                error = f"{provider}:entity_account_load_failed"
                if error not in provider_load_errors:
                    provider_load_errors.append(error)

        load_errors.extend(provider_load_errors)

        integrations.append(
            RuntimeIntegrationBinding(
                provider=provider,
                accounts=_ordered_accounts(
                    accounts,
                    preference=preferences.get(provider),
                    mark_effective_default=not provider_load_errors,
                ),
                configured_entity_account_count=len(provider_entity_rows),
                credentialed_entity_account_count=credentialed_count,
                denied_permissions=tuple(denied_permissions),
                reconnect_required_account_ids=tuple(reconnect_required_account_ids),
                load_errors=tuple(provider_load_errors),
                preference=preferences.get(provider),
            )
        )

    return RuntimeIntegrationRegistry(
        user_id=user_id,
        entity_id=entity_id,
        integrations=tuple(integrations),
        covered_providers=requested,
        load_errors=tuple(load_errors),
    )


class RuntimeIntegrationRegistryFactory:
    """Canonical factory for one actor's integration-account snapshot."""

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        user_id: str,
        entity_id: str,
        provider_keys: Iterable[str] | None = None,
        live_nango_connection_ids: set[str] | frozenset[str] | None = None,
    ) -> RuntimeIntegrationRegistry:
        return await _load_runtime_integration_registry(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider_keys=provider_keys,
            live_nango_connection_ids=live_nango_connection_ids,
        )

    @staticmethod
    async def try_create(
        db: AsyncSession,
        *,
        user_id: str,
        entity_id: str,
        provider_keys: Iterable[str] | None = None,
        live_nango_connection_ids: set[str] | frozenset[str] | None = None,
    ) -> RuntimeIntegrationRegistryLoadResult:
        try:
            registry = await RuntimeIntegrationRegistryFactory.create(
                db,
                user_id=user_id,
                entity_id=entity_id,
                provider_keys=provider_keys,
                live_nango_connection_ids=live_nango_connection_ids,
            )
        except Exception:
            logger.exception(
                "Failed to load runtime integration account registry for entity %s",
                entity_id,
            )
            return RuntimeIntegrationRegistryLoadResult(
                status=IntegrationRegistryLoadStatus.FAILED,
                errors=("integration_account_registry_load_failed",),
            )
        status = IntegrationRegistryLoadStatus.PARTIAL if registry.load_errors else IntegrationRegistryLoadStatus.READY
        return RuntimeIntegrationRegistryLoadResult(
            status=status,
            registry=registry,
            errors=registry.load_errors,
        )


async def load_runtime_integration_registry(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider_keys: Iterable[str] | None = None,
    live_nango_connection_ids: set[str] | frozenset[str] | None = None,
) -> RuntimeIntegrationRegistry:
    """Load a credential-free registry without performing provider I/O.

    A caller that already fetched a live Nango snapshot outside its database
    session may inject it. Otherwise Nango liveness is verified when the exact
    account credential is leased for execution.
    """
    return await RuntimeIntegrationRegistryFactory.create(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=provider_keys,
        live_nango_connection_ids=live_nango_connection_ids,
    )


async def try_load_runtime_integration_registry(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider_keys: Iterable[str] | None = None,
    live_nango_connection_ids: set[str] | frozenset[str] | None = None,
) -> RuntimeIntegrationRegistryLoadResult:
    """Load a registry as an observable, fail-closed result."""
    return await RuntimeIntegrationRegistryFactory.try_create(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=provider_keys,
        live_nango_connection_ids=live_nango_connection_ids,
    )


async def list_runtime_integration_accounts(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider: str,
    include_unusable_oauth: bool = False,
) -> list[RuntimeIntegrationAccount]:
    """Return every account the acting user may use for ``provider``.

    Owner accounts are preferred to explicitly shared accounts. Within each
    ownership class, personal OAuth precedes entity credentials, with the
    explicit default preferred inside that scope. Secrets are never returned.
    """
    if include_unusable_oauth:
        provider = canonical_provider_key(provider)
        aliases = provider_key_aliases(provider)
        shared_ids = await _shared_resource_ids(
            db,
            entity_id=entity_id,
            user_id=user_id,
            resource_type=ResourceType.OAUTH_ACCOUNT,
        )
        rows = list(
            (
                await db.execute(
                    select(OAuthAccount)
                    .where(
                        OAuthAccount.provider.in_(aliases),
                        (OAuthAccount.user_id == user_id) | OAuthAccount.id.in_(shared_ids),
                    )
                    .order_by(OAuthAccount.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        oauth_accounts: list[RuntimeIntegrationAccount] = []
        for row in rows:
            access = await resolve_integration_access(
                db,
                kind="oauth_account",
                connection_id=row.id,
                entity_id=entity_id,
                user_id=user_id,
                action="use",
            )
            if access.allowed:
                oauth_accounts.append(
                    RuntimeIntegrationAccountFactory.from_oauth(
                        row,
                        actor_user_id=user_id,
                        provider=provider,
                    )
                )
        usable_registry = await load_runtime_integration_registry(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider_keys=[provider],
        )
        entity_accounts = [
            account
            for account in usable_registry.accounts_for(provider)
            if account.scope is IntegrationAccountScope.ENTITY
        ]
        preferences = await _load_runtime_integration_account_preferences(
            db,
            user_id=user_id,
            entity_id=entity_id,
            provider_keys=[provider],
        )
        return list(
            _ordered_accounts(
                [*oauth_accounts, *entity_accounts],
                preference=preferences.get(provider),
            )
        )

    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=[provider],
    )
    return list(registry.accounts_for(provider))


async def load_integration_catalog_accounts(
    db: AsyncSession,
    *,
    user_id: str,
    entity_id: str,
    provider_keys: Iterable[str] | None = None,
) -> IntegrationAccountCatalogSnapshot:
    """Bulk-load authorized account labels for the Integrations catalog.

    Callable accounts come from the runtime registry. Authorized OAuth and
    entity rows omitted by runtime projection remain visible for repair or
    removal, but this catalog-only projection never makes them callable and
    never leases credentials.
    """
    registry = await load_runtime_integration_registry(
        db,
        user_id=user_id,
        entity_id=entity_id,
        provider_keys=provider_keys,
    )
    accounts_by_provider: dict[str, list[IntegrationAccountCatalogAccount]] = {
        binding.provider: [
            IntegrationAccountCatalogAccountFactory.from_runtime(account) for account in binding.accounts
        ]
        for binding in registry.integrations
    }
    extra_errors_by_provider: dict[str, list[str]] = defaultdict(list)
    known_ids = {account.id for accounts in accounts_by_provider.values() for account in accounts}

    requested = None
    if provider_keys is not None:
        requested = frozenset(canonical for key in provider_keys if (canonical := canonical_provider_key(key)))
        if not requested:
            return IntegrationAccountCatalogSnapshot(())
    storage_keys = (
        {alias for provider in requested for alias in provider_key_aliases(provider)} if requested is not None else None
    )
    shared_ids_by_type = await _shared_resource_ids_by_type(
        db,
        entity_id=entity_id,
        user_id=user_id,
        resource_types=(ResourceType.OAUTH_ACCOUNT, ResourceType.INTEGRATION),
    )
    shared_oauth_ids = shared_ids_by_type[ResourceType.OAUTH_ACCOUNT]
    shared_integration_ids = shared_ids_by_type[ResourceType.INTEGRATION]
    oauth_query = select(OAuthAccount).where(
        (OAuthAccount.user_id == user_id) | OAuthAccount.id.in_(shared_oauth_ids),
    )
    entity_query = select(Integration).where(
        Integration.entity_id == entity_id,
        Integration.status == "active",
        (Integration.owner_user_id == user_id) | Integration.id.in_(shared_integration_ids),
    )
    if storage_keys is not None:
        oauth_query = oauth_query.where(OAuthAccount.provider.in_(storage_keys))
        entity_query = entity_query.where(Integration.provider.in_(storage_keys))
    catalog_oauth_rows = list((await db.execute(oauth_query.order_by(OAuthAccount.created_at.desc()))).scalars().all())
    catalog_entity_rows = list((await db.execute(entity_query.order_by(Integration.created_at.desc()))).scalars().all())
    active_shared_owner_ids = await _active_entity_member_ids(
        db,
        entity_id=entity_id,
        user_ids={row.user_id for row in catalog_oauth_rows if row.user_id != user_id}
        | {row.owner_user_id for row in catalog_entity_rows if row.owner_user_id and row.owner_user_id != user_id},
    )

    for row in catalog_oauth_rows:
        if row.id in known_ids:
            continue
        provider = canonical_provider_key(row.provider)
        if not provider or (requested is not None and provider not in requested):
            continue
        if row.user_id != user_id and row.user_id not in active_shared_owner_ids:
            continue
        try:
            availability = (
                IntegrationAccountAvailability.LOAD_FAILED
                if oauth_account_is_runtime_usable(row)
                else IntegrationAccountAvailability.RECONNECT_REQUIRED
            )
            account = IntegrationAccountCatalogAccountFactory.from_oauth_management(
                row,
                actor_user_id=user_id,
                provider=provider,
                availability=availability,
            )
        except Exception:
            logger.exception(
                "Failed to load management OAuth account for provider %s",
                provider,
            )
            extra_errors_by_provider[provider].append(f"{provider}:catalog_oauth_account_load_failed")
            continue
        if availability is IntegrationAccountAvailability.LOAD_FAILED:
            extra_errors_by_provider[provider].append(f"{provider}:oauth_account_load_failed:{row.id}")
        accounts_by_provider.setdefault(provider, []).append(account)

    permission_cache: dict[str, bool] = {}
    for row in catalog_entity_rows:
        if row.id in known_ids:
            continue
        provider = canonical_provider_key(row.provider)
        if not provider or (requested is not None and provider not in requested):
            continue
        if row.owner_user_id != user_id and row.owner_user_id not in active_shared_owner_ids:
            continue

        availability = IntegrationAccountAvailability.LOAD_FAILED
        load_error: str | None = None
        try:
            binding = registry.integration(provider)
            reconnect_required_ids = binding.reconnect_required_account_ids if binding else ()
            if row.id in reconnect_required_ids:
                availability = IntegrationAccountAvailability.RECONNECT_REQUIRED
            elif not entity_account_has_credentials(row):
                availability = IntegrationAccountAvailability.RECONNECT_REQUIRED
            elif row.required_permission:
                permission = str(row.required_permission)
                if permission not in permission_cache:
                    permission_cache[permission] = await user_has_permission(
                        db,
                        user_id,
                        entity_id,
                        permission,
                    )
                if not permission_cache[permission]:
                    availability = IntegrationAccountAvailability.PERMISSION_DENIED
                else:
                    load_error = f"{provider}:entity_account_load_failed:{row.id}"
            else:
                load_error = f"{provider}:entity_account_load_failed:{row.id}"
        except Exception:
            logger.exception(
                "Failed to classify management entity account for provider %s",
                provider,
            )
            load_error = f"{provider}:entity_account_load_failed:{row.id}"

        try:
            account = IntegrationAccountCatalogAccountFactory.from_entity_management(
                row,
                actor_user_id=user_id,
                provider=provider,
                availability=availability,
            )
        except Exception:
            logger.exception(
                "Failed to load management entity account for provider %s",
                provider,
            )
            extra_errors_by_provider[provider].append(f"{provider}:catalog_entity_account_load_failed")
            continue
        if load_error:
            extra_errors_by_provider[provider].append(load_error)
        accounts_by_provider.setdefault(provider, []).append(account)

    return IntegrationAccountCatalogSnapshotFactory.create(
        registry,
        accounts_by_provider=accounts_by_provider,
        extra_errors_by_provider=extra_errors_by_provider,
    )


async def _active_entity_member_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    user_ids: Iterable[str],
) -> set[str]:
    requested = {user_id for user_id in user_ids if user_id}
    if not requested:
        return set()
    active = set(
        (
            await db.execute(
                select(UserMembership.user_id).where(
                    UserMembership.user_id.in_(requested),
                    UserMembership.entity_id == entity_id,
                    UserMembership.status == "active",
                    UserMembership.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = requested - active
    if missing:
        active.update(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(missing),
                        User.entity_id == entity_id,
                        User.status == "active",
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    return active


async def _shared_resource_ids(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    resource_type: str,
) -> set[str]:
    return (
        await _shared_resource_ids_by_type(
            db,
            entity_id=entity_id,
            user_id=user_id,
            resource_types=(resource_type,),
        )
    )[resource_type]


async def _shared_resource_ids_by_type(
    db: AsyncSession,
    *,
    entity_id: str,
    user_id: str,
    resource_types: Iterable[str],
) -> dict[str, set[str]]:
    requested_types = tuple(dict.fromkeys(resource_types))
    shared_ids = {resource_type: set() for resource_type in requested_types}
    if not requested_types:
        return shared_ids
    now = datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(ResourceGrant).where(
                    ResourceGrant.entity_id == entity_id,
                    ResourceGrant.resource_type.in_(requested_types),
                    ResourceGrant.subject_type == SubjectType.USER,
                    ResourceGrant.subject_id == user_id,
                    ResourceGrant.status == GrantStatus.ACTIVE,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if Capability.USE in (row.capabilities or []) and (row.expires_at is None or row.expires_at > now):
            shared_ids.setdefault(row.resource_type, set()).add(row.resource_id)
    return shared_ids


def select_runtime_integration_account(
    accounts: list[RuntimeIntegrationAccount],
    selector: str | None = None,
) -> tuple[RuntimeIntegrationAccount | None, str | None]:
    """Select by exact connection ID, or use the ordered default."""
    plan = RuntimeIntegrationAccountCallPlanFactory.create(
        accounts,
        selector=selector,
    )
    if plan.accounts:
        return plan.accounts[0], None
    if plan.mode is IntegrationAccountSelectionMode.DEFAULT:
        return None, None
    return None, ("The selected integration account is not connected or is not available to this user.")
