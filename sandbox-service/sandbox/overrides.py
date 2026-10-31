"""Which container settings a caller may override, and how far.

A create request may carry ``config_overrides``. Only resource and timeout
fields are the caller's to choose, each within an operator-set maximum; the
network stays within ``SANDBOX_ALLOWED_NETWORKS`` (checked by the runner's
config validation). Everything else — image, security posture, mounts,
naming and scoping, the workdir — is runner-owned, so a token holder cannot
swap the image, add capabilities, drop the read-only root, or mount host
paths. A rejected field answers 400 ``config_override_rejected``.

``OVERRIDABLE_FIELDS`` and ``RUNNER_OWNED_FIELDS`` partition
``ContainerConfig.model_fields`` exactly; a new field is runner-owned until
someone adds a bounded check for it here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sandbox_config import config as app_config

from .capacity import format_size, parse_size
from .errors import ConfigOverrideRejected
from .models import ContainerConfig

# The smallest memory limit worth a container (Docker's own floor is 6m).
_MIN_MEMORY_BYTES = 64 * 1024 * 1024


def _reject(field: str, reason: str, message: str, **extra: Any) -> ConfigOverrideRejected:
    return ConfigOverrideRejected(
        message,
        extra={"field": field, "reason": reason, **extra},
    )


def _size(field: str, value: Any, maximum: str, minimum: int = 1) -> str:
    try:
        size = parse_size(value)
    except ValueError:
        raise _reject(field, "invalid_value", f"{field} must be a size such as 512m or 2g") from None
    if size < minimum:
        raise _reject(field, "invalid_value", f"{field} is below the minimum ({minimum} bytes)")
    if size > parse_size(maximum):
        raise _reject(
            field,
            "above_maximum",
            f"{field}={value} exceeds this runner's maximum {maximum}",
            maximum=maximum,
        )
    return format_size(size)


def _bounded_number(field: str, value: Any, maximum: float, *, integer: bool) -> Any:
    if isinstance(value, bool):
        raise _reject(field, "invalid_value", f"{field} must be a number")
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise _reject(field, "invalid_value", f"{field} must be a number") from None
    # Zero or a negative value would drop the limit altogether.
    if number <= 0:
        raise _reject(field, "invalid_value", f"{field} must be positive")
    if number > maximum:
        raise _reject(
            field,
            "above_maximum",
            f"{field}={value} exceeds this runner's maximum {maximum}",
            maximum=maximum,
        )
    return number


def _memory(value: Any) -> str:
    return _size("memory", value, app_config.OVERRIDE_MAX_MEMORY, _MIN_MEMORY_BYTES)


def _cpus(value: Any) -> float:
    return _bounded_number("cpus", value, float(app_config.OVERRIDE_MAX_CPUS), integer=False)


def _pids_limit(value: Any) -> int:
    return _bounded_number("pids_limit", value, int(app_config.OVERRIDE_MAX_PIDS), integer=True)


def _workdir_tmpfs_size(value: Any) -> str:
    return _size("workdir_tmpfs_size", value, app_config.OVERRIDE_MAX_WORKDIR_TMPFS)


def _exec_timeout(value: Any) -> int:
    return _bounded_number(
        "exec_timeout", value, int(app_config.OVERRIDE_MAX_EXEC_TIMEOUT), integer=True
    )


def _install_timeout(value: Any) -> int:
    return _bounded_number(
        "install_timeout", value, int(app_config.OVERRIDE_MAX_INSTALL_TIMEOUT), integer=True
    )


def _network(value: Any) -> str:
    # Bounded by SANDBOX_ALLOWED_NETWORKS in SkillRunner._validate_config,
    # which also covers the runner's own default.
    if not isinstance(value, str) or not value.strip():
        raise _reject("network", "invalid_value", "network must be a Docker network name")
    return value.strip()


#: field -> check returning the normalised value (or raising 400).
OVERRIDABLE_FIELDS: dict[str, Callable[[Any], Any]] = {
    "memory": _memory,
    "cpus": _cpus,
    "pids_limit": _pids_limit,
    "workdir_tmpfs_size": _workdir_tmpfs_size,
    "network": _network,
    "exec_timeout": _exec_timeout,
    "install_timeout": _install_timeout,
}

RUNNER_OWNED_FIELDS: frozenset[str] = frozenset(
    set(ContainerConfig.model_fields) - set(OVERRIDABLE_FIELDS)
)


def apply_overrides(cfg: ContainerConfig, overrides: ContainerConfig | None) -> ContainerConfig:
    """Apply the fields the caller actually sent; reject the ones it may not set.

    Only explicitly set fields count (``model_fields_set``): a request that
    omits a field keeps the runner's default for it.
    """

    if overrides is None:
        return cfg
    requested = sorted(getattr(overrides, "model_fields_set", set()) or set())
    owned = [field for field in requested if field not in OVERRIDABLE_FIELDS]
    if owned:
        raise _reject(
            owned[0],
            "not_overridable",
            "Container config field(s) "
            + ", ".join(owned)
            + " are set by the runner and cannot be overridden; "
            + "overridable: "
            + ", ".join(sorted(OVERRIDABLE_FIELDS)),
            fields=owned,
        )
    for field in requested:
        setattr(cfg, field, OVERRIDABLE_FIELDS[field](getattr(overrides, field)))
    return cfg
