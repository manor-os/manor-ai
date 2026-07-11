"""Channel-binding flows.

  pairing.py    Short-lived pairing codes for ops-driven DM binding.
                Used by the M10 webchat / Telegram / iMessage onboarding
                flow.
"""
from packages.core.channels.pairing import (
    PairingError,
    PairingExpired,
    PairingMismatch,
    create_pairing_code,
    redeem_pairing_code,
)

__all__ = [
    "PairingError",
    "PairingExpired",
    "PairingMismatch",
    "create_pairing_code",
    "redeem_pairing_code",
]
