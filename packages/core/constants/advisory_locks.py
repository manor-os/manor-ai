"""Stable PostgreSQL advisory-lock namespaces for runtime side effects."""
from enum import IntEnum


class AdvisoryLockNamespace(IntEnum):
    """Keep unrelated logical resources out of the same 32-bit key space."""

    CHANNEL_INBOUND_DISPATCH = 0x4D414E02
    APPROVED_EXTERNAL_REPLY = 0x4D414E03
    NOTIFICATION_DELIVERY_CALLBACK = 0x4D414E04
    TASK_RUNTIME_APPROVAL = 0x4D414E05
    PLAN_EXECUTION_CYCLE = 0x4D414E06
    WORKER_LEASE_ADMISSION = 0x4D414E07
    PLAN_CYCLE_RECOVERY_SWEEP = 0x4D414E08
