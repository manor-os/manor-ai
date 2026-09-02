"""Typed identities for durable notification delivery progress."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from packages.core.constants.notification_types import NotificationChannel


class NotificationTargetKind(str, Enum):
    ENTITY = "entity"
    WORKSPACE = "workspace"
    CONTACT = "contact"
    ADDRESS = "address"


@dataclass(frozen=True, slots=True)
class NotificationDeliveryTarget:
    channel_type: str
    kind: NotificationTargetKind
    identity: str

    @property
    def key(self) -> str:
        return f"{self.channel_type}:{self.kind.value}:{self.identity}"


class NotificationDeliveryTargetFactory:
    """Build and parse the one persisted target-key representation."""

    @staticmethod
    def broadcast(
        entity_id: str,
        *,
        workspace_id: str | None = None,
    ) -> NotificationDeliveryTarget:
        if workspace_id:
            return NotificationDeliveryTarget(
                channel_type=NotificationChannel.BROADCAST.value,
                kind=NotificationTargetKind.WORKSPACE,
                identity=workspace_id,
            )
        return NotificationDeliveryTarget(
            channel_type=NotificationChannel.BROADCAST.value,
            kind=NotificationTargetKind.ENTITY,
            identity=entity_id,
        )

    @staticmethod
    def external(
        *,
        channel_type: str,
        contact_id: str | None = None,
        address: str | None = None,
    ) -> NotificationDeliveryTarget:
        if contact_id:
            return NotificationDeliveryTarget(
                channel_type=channel_type,
                kind=NotificationTargetKind.CONTACT,
                identity=contact_id,
            )
        normalized_address = str(address or "").strip().lower()
        if normalized_address:
            return NotificationDeliveryTarget(
                channel_type=channel_type,
                kind=NotificationTargetKind.ADDRESS,
                identity=normalized_address,
            )
        raise ValueError("notification delivery target requires a contact or address")

    @staticmethod
    def parse(key: str) -> NotificationDeliveryTarget:
        try:
            channel_type, kind_value, identity = key.split(":", 2)
            kind = NotificationTargetKind(kind_value)
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"invalid notification delivery target key {key!r}") from exc
        if not channel_type or not identity:
            raise ValueError(f"invalid notification delivery target key {key!r}")
        return NotificationDeliveryTarget(
            channel_type=channel_type,
            kind=kind,
            identity=identity,
        )
