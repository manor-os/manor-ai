"""Principal vocabulary shared by runtime resolution and authorization."""

from __future__ import annotations

from enum import Enum
from typing import Any


class RuntimePrincipalKind(str, Enum):
    OWNER = "owner"
    WORKSPACE_MEMBER = "workspace_member"
    AGENT = "agent"
    EXTERNAL_CONTACT = "external_contact"
    ANONYMOUS_PUBLIC = "anonymous_public"
    SYSTEM_WORKER = "system_worker"
    DELEGATED = "delegated"

    __str__ = str.__str__
    __format__ = str.__format__

    @classmethod
    def parse(cls, value: Any) -> "RuntimePrincipalKind | None":
        raw = getattr(value, "value", value)
        normalized = str(raw or "").strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            return None
