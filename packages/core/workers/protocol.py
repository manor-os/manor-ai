"""Authoritative version contract for external Manor workers."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Mapping


WORKER_PROTOCOL_HEADER = "Manor-Protocol-Version"


class WorkerProtocolVersion(IntEnum):
    """Supported external-worker wire protocol versions."""

    V2 = 2


CURRENT_WORKER_PROTOCOL_VERSION = WorkerProtocolVersion.V2


class UnsupportedWorkerProtocol(ValueError):
    """An external worker does not declare the current wire protocol."""


def require_current_worker_protocol(capabilities: Mapping[str, Any]) -> None:
    """Reject registrations that cannot speak the current worker protocol."""

    declared = capabilities.get("protocol_version")
    if declared != int(CURRENT_WORKER_PROTOCOL_VERSION):
        raise UnsupportedWorkerProtocol(
            "external workers must declare protocol_version "
            f"{int(CURRENT_WORKER_PROTOCOL_VERSION)}"
        )


def worker_protocol_header_value() -> str:
    return str(int(CURRENT_WORKER_PROTOCOL_VERSION))
