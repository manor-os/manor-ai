"""Human participation service layer (M9).

Profiles, authority resolution, commitments, and contributions — the
data layer that lets the Strategist see humans as first-class
participants without ever building hidden per-person scorecards.
"""
from packages.core.humans.service import (
    PERMISSION_KEYS,
    ROLE_DEFAULT_AUTHORITY,
    get_or_create_profile,
    list_open_commitments,
    open_commitment,
    participant_can,
    record_contribution,
    resolve_commitment,
    resolve_commitments_for_step,
    update_profile,
)

__all__ = [
    "PERMISSION_KEYS",
    "ROLE_DEFAULT_AUTHORITY",
    "get_or_create_profile",
    "list_open_commitments",
    "open_commitment",
    "participant_can",
    "record_contribution",
    "resolve_commitment",
    "resolve_commitments_for_step",
    "update_profile",
]
