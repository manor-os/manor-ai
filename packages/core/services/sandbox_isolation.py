"""Isolation references: what a sandbox must be created with beyond its config.

A ``SandboxBundle`` may name an ``isolation_ref`` (an opaque string such as
``"<kind>:<id>"``) and the ``isolation_key`` the container is made for. The
ref travels in reservation tickets and is resolved only where the container is
created, by the resolver registered for its prefix, into extra keyword
arguments of the runner create call. Whatever it resolves to (credentials, for
one) therefore never sits in a ticket or the warm registry. A container is
reused only for a bundle with the same ``isolation_key``.

An unknown or failing ref fails closed: the create is refused rather than made
without its isolation.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

IsolationResolver = Callable[[str], Awaitable[dict[str, Any]]]

_RESOLVERS: dict[str, IsolationResolver] = {}


class SandboxIsolationUnavailable(RuntimeError):
    """An isolation ref no resolver can turn into create arguments.

    Permanent for its ticket (a retry would resolve the same ref): the
    allocation classifies it as fatal, like a request the runner rejected.
    """

    status_code = 422
    code = "isolation_unavailable"


def register_isolation_resolver(prefix: str, resolver: IsolationResolver) -> None:
    """Resolve refs starting with ``prefix`` (e.g. ``"kind:"``) with ``resolver``."""
    if not prefix or not prefix.endswith(":"):
        raise ValueError("an isolation ref prefix ends with ':'")
    _RESOLVERS[prefix] = resolver


def _load_resolvers() -> None:
    return None


async def resolve_isolation_ref(ref: str) -> dict[str, Any]:
    """The extra create arguments ``ref`` stands for; raises ``SandboxIsolationUnavailable``."""
    _load_resolvers()
    for prefix, resolver in _RESOLVERS.items():
        if ref.startswith(prefix):
            try:
                resolved = await resolver(ref)
            except SandboxIsolationUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - every failure is a refusal, never a plain create
                raise SandboxIsolationUnavailable(f"isolation {ref} could not be resolved: {exc}") from exc
            if not isinstance(resolved, dict):
                raise SandboxIsolationUnavailable(f"isolation {ref} resolved to no create arguments")
            return resolved
    raise SandboxIsolationUnavailable(f"no resolver for isolation {ref}")
