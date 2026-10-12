"""Trusted per-call group output identity for ordinary file generators.

This context is set by the room runtime after it claims an exact group tool
receipt. It is deliberately not a model argument or a Conversation id.
Asynchronous media workers must use their persisted MediaJob scope instead.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class RoomGenerationScope:
    room_id: str
    run_id: str
    approval_id: str
    requester_user_id: str
    execution_revision: int
    tool_name: str
    arguments: dict


_scope: ContextVar[RoomGenerationScope | None] = ContextVar(
    "room_generation_scope", default=None,
)


@contextmanager
def room_generation_scope(scope: RoomGenerationScope) -> Iterator[None]:
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def current_room_generation_scope() -> RoomGenerationScope | None:
    return _scope.get()
