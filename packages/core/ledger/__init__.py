"""Workspace event ledger (M1) — append-only facts, one write path.

Public surface:

* ``record_event`` — same-transaction, idempotent append (see ``service``).
* ``event_types`` — the closed event-type vocabulary (``ALL_EVENT_TYPES``).
"""
from packages.core.ledger import event_types
from packages.core.ledger.event_types import ALL_EVENT_TYPES
from packages.core.ledger.service import MAX_PAYLOAD_BYTES, record_event

__all__ = ["ALL_EVENT_TYPES", "MAX_PAYLOAD_BYTES", "event_types", "record_event"]
