"""Request-local authentication assurance shared with in-process tools."""
from __future__ import annotations

import contextvars


_mfa_verified_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "manor_mfa_verified",
    default=False,
)


def set_current_mfa_verified(value: bool) -> None:
    _mfa_verified_var.set(bool(value))


def current_mfa_verified() -> bool:
    return bool(_mfa_verified_var.get())
