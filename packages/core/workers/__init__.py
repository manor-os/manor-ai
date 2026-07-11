"""Worker layer — registry, auth, lifecycle.

Two flavours of worker:

  internal — one per entity, runs as an asyncio task inside the Manor
             process. Ensured by ``ensure_internal_worker(entity_id)``
             on app startup. No secret. Auto-bound to all subscriptions
             that don't have an explicit worker preference.

  external — registered via ``POST /api/v1/workers/register`` (M3.5).
             Has a bcrypt-hashed secret used to authenticate heartbeats.
             Examples: a developer's Claude Code, a custom HTTP daemon,
             a shell-script-based worker.

This package is the *registry* layer: who exists, what they can do,
how they authenticate. The ``packages.core.dispatcher`` package on top
decides who gets which lease.
"""
from packages.core.workers.registry import (
    DEFAULT_INTERNAL_CAPABILITIES,
    INTERNAL_WORKER_KIND,
    bind_subscription,
    ensure_internal_worker,
    get_worker,
    list_workers_for_subscription,
    register_external_worker,
    rotate_worker_secret,
    update_worker_status,
    verify_worker_secret,
)

__all__ = [
    "DEFAULT_INTERNAL_CAPABILITIES",
    "INTERNAL_WORKER_KIND",
    "ensure_internal_worker",
    "register_external_worker",
    "rotate_worker_secret",
    "verify_worker_secret",
    "bind_subscription",
    "list_workers_for_subscription",
    "get_worker",
    "update_worker_status",
]
