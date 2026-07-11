from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from packages.core.ai.runtime.envelope import RuntimeEnvelope


def _middleware_name_tuple(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item or "").strip())
    return (str(value),)


def _append_middleware_names(
    envelope: RuntimeEnvelope,
    *,
    metadata_key: str,
    applied_names: list[str],
) -> None:
    if not applied_names:
        return
    existing = _middleware_name_tuple(envelope.metadata.get(metadata_key))
    envelope.metadata[metadata_key] = existing + tuple(applied_names)


class RuntimeMiddleware(Protocol):
    name: str

    async def apply(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        ...


class SyncRuntimeMiddleware(Protocol):
    name: str

    def apply_sync(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        ...


@dataclass(frozen=True)
class NoopRuntimeMiddleware:
    name: str

    async def apply(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        return envelope

    def apply_sync(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        return envelope


@dataclass(frozen=True)
class RuntimeMiddlewareStack:
    """Sequential middleware chain for one runtime envelope."""

    middleware: tuple[RuntimeMiddleware | SyncRuntimeMiddleware, ...] = ()
    metadata_key: str = "runtime_middleware"

    async def apply(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        current = envelope
        applied_names: list[str] = []
        for item in self.middleware:
            if hasattr(item, "apply_sync"):
                current = item.apply_sync(current)  # type: ignore[attr-defined]
            else:
                current = await item.apply(current)  # type: ignore[attr-defined]
            name = str(getattr(item, "name", "") or "").strip()
            if name:
                applied_names.append(name)
        if applied_names:
            _append_middleware_names(
                current,
                metadata_key=self.metadata_key,
                applied_names=applied_names,
            )
        return current

    def apply_sync(self, envelope: RuntimeEnvelope) -> RuntimeEnvelope:
        current = envelope
        applied_names: list[str] = []
        for item in self.middleware:
            if not hasattr(item, "apply_sync"):
                raise TypeError(
                    f"Runtime middleware {getattr(item, 'name', item)!r} cannot run synchronously"
                )
            current = item.apply_sync(current)  # type: ignore[attr-defined]
            name = str(getattr(item, "name", "") or "").strip()
            if name:
                applied_names.append(name)
        if applied_names:
            _append_middleware_names(
                current,
                metadata_key=self.metadata_key,
                applied_names=applied_names,
            )
        return current


async def apply_runtime_middleware(
    envelope: RuntimeEnvelope,
    middleware: Iterable[RuntimeMiddleware | SyncRuntimeMiddleware] | None = None,
    *,
    metadata_key: str = "runtime_middleware",
) -> RuntimeEnvelope:
    return await RuntimeMiddlewareStack(tuple(middleware or ()), metadata_key=metadata_key).apply(envelope)


def apply_runtime_middleware_sync(
    envelope: RuntimeEnvelope,
    middleware: Iterable[SyncRuntimeMiddleware] | None = None,
    *,
    metadata_key: str = "runtime_middleware",
) -> RuntimeEnvelope:
    return RuntimeMiddlewareStack(tuple(middleware or ()), metadata_key=metadata_key).apply_sync(envelope)


def default_resolver_middleware() -> tuple[SyncRuntimeMiddleware, ...]:
    """Trace-only resolver stages, kept stable for auditability."""
    return (
        NoopRuntimeMiddleware("surface"),
        NoopRuntimeMiddleware("principal"),
        NoopRuntimeMiddleware("tool"),
        NoopRuntimeMiddleware("capability"),
        NoopRuntimeMiddleware("memory"),
        NoopRuntimeMiddleware("file_context"),
        NoopRuntimeMiddleware("trace"),
    )
