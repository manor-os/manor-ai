"""Manor Worker SDK — write a custom Manor worker in <50 lines.

Standalone package: only depends on ``httpx`` + ``pydantic``. No
imports from ``packages.core.*`` so the SDK can be split into its own
pip package whenever we want — or copy-pasted into a user's repo.

Quickstart:

    from manor_worker_sdk import ManorWorker, Lease

    worker = ManorWorker(
        endpoint="https://manor.example.com",
        worker_id="wkr_xxx",
        secret="wks_xxx",
    )

    @worker.handle(kind="action", provider="shopify")
    async def handle_shopify(lease: Lease, ctx) -> dict:
        action = lease.action_key
        creds = lease.credentials[0]["value"]
        ...
        return {
            "result": {"order_id": "..."},
            "cost": {"api_calls": 1, "usd": 0.0},
        }

    worker.run_forever()

That's it — heartbeat, lease pickup, completion reporting, retry/backoff
on transient failures, and clean shutdown all handled by the SDK.
"""
from packages.worker_sdk.client import ManorClient, WorkerClientError
from packages.worker_sdk.types import (
    Lease,
    LeaseResult,
    NeedHumanInput,
    HeartbeatRequest,
    HeartbeatResponse,
)
from packages.worker_sdk.worker import (
    ManorWorker,
    LeaseContext,
    NoHandlerError,
)

__all__ = [
    "ManorClient",
    "ManorWorker",
    "Lease",
    "LeaseResult",
    "LeaseContext",
    "NeedHumanInput",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "WorkerClientError",
    "NoHandlerError",
]
