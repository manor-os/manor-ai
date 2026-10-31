"""Worker layer — registry, auth, lifecycle.

Two flavours of worker:

  internal — rows of the retired in-process Plan Step worker. They no
             longer run; external endpoints still refuse them.

  external — registered via ``POST /api/v1/workers/register`` (M3.5).
             Has a bcrypt-hashed secret used to authenticate heartbeats.
             Examples: a developer's Claude Code, a custom HTTP daemon,
             a shell-script-based worker.

This package is the *registry* layer: who exists, what they can do,
how they authenticate. The ``packages.core.dispatcher`` package on top
decides who gets which lease.
"""
from packages.core.workers.registry import (
    INTERNAL_WORKER_KIND,
    bind_subscription,
    get_worker,
    list_workers_for_subscription,
    mark_stale_external_workers_offline,
    register_external_worker,
    rotate_worker_secret,
    update_worker_status,
)
from packages.core.workers.protocol import (
    CURRENT_WORKER_PROTOCOL_VERSION,
    LEASE_ATTEMPT_TOKEN_CAPABILITY,
    UnsupportedWorkerProtocol,
    WORKER_PROTOCOL_HEADER,
    WorkerLeaseFencingVersion,
    WorkerProtocolVersion,
    require_current_worker_protocol,
    supports_lease_attempt_token,
    worker_protocol_header_value,
)

__all__ = [
    "INTERNAL_WORKER_KIND",
    "register_external_worker",
    "rotate_worker_secret",
    "bind_subscription",
    "list_workers_for_subscription",
    "mark_stale_external_workers_offline",
    "get_worker",
    "update_worker_status",
    "CURRENT_WORKER_PROTOCOL_VERSION",
    "LEASE_ATTEMPT_TOKEN_CAPABILITY",
    "UnsupportedWorkerProtocol",
    "WORKER_PROTOCOL_HEADER",
    "WorkerLeaseFencingVersion",
    "WorkerProtocolVersion",
    "require_current_worker_protocol",
    "supports_lease_attempt_token",
    "worker_protocol_header_value",
]
