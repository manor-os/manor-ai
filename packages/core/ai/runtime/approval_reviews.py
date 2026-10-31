"""Review providers: what an approval card shows of the thing being approved.

Most approvals render from the tool call alone. A few need more, prepared
when the request is minted and stored on it: the exact post a TikTok upload
publishes, the documents a Chrome publication binds, the screen of a cloud
computer before a click. Each such provider is registered here with the
calls it covers; the runtime approval service asks this registry instead of
naming tools. A provider returns the fields it adds to the request's render
context (``review`` is what the card shows); ``None`` adds nothing. A provider
that finds the call cannot run now raises ``ReviewTargetUnavailable``: no card
is minted, and the call gets the provider's result instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from packages.core.ai.runtime.approval_messages import RuntimeApprovalContinuationError
from packages.core.ai.runtime.approvals import RuntimeApprovalAction


class ReviewTargetUnavailable(RuntimeApprovalContinuationError):
    """The call cannot run now, so nobody is asked to approve it.

    ``result`` is the tool result the call gets instead of a card: what is
    missing and how to get it (the machine it runs on is off: start it first).
    """

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result.get("error") or result.get("message") or "The action cannot run now."))
        self.result = dict(result)


@dataclass(frozen=True)
class ReviewRequest:
    db: Any
    tool_name: str
    arguments: dict[str, Any]
    action: RuntimeApprovalAction
    entity_id: str
    user_id: str
    conversation_id: str | None
    workspace_id: str | None


ReviewProvider = Callable[[ReviewRequest], Awaitable[dict[str, Any] | None]]
ReviewMatcher = Callable[[str, str], bool]


@dataclass(frozen=True)
class _Registration:
    name: str
    matches: ReviewMatcher
    prepare: ReviewProvider
    # One-time options only (no "Always"): the review is of this exact item.
    one_time: bool = False
    # The card shows the review but the user cannot edit it.
    read_only: bool = False


_PROVIDERS: list[_Registration] = []


def register_review_provider(
    name: str, matches: ReviewMatcher, prepare: ReviewProvider, *, one_time: bool = False, read_only: bool = False,
) -> None:
    """Register (or replace, by name) the review for calls ``matches(tool_name, action_key)`` covers."""
    _PROVIDERS[:] = [entry for entry in _PROVIDERS if entry.name != name]
    _PROVIDERS.append(_Registration(name, matches, prepare, one_time, read_only))


def review_provider_for(tool_name: str, action_key: str) -> _Registration | None:
    _load_providers()
    for entry in _PROVIDERS:
        if entry.matches(tool_name, action_key):
            return entry
    return None


async def prepare_review(request: ReviewRequest) -> dict[str, Any]:
    entry = review_provider_for(request.tool_name, request.action.action_key)
    if entry is None:
        return {}
    return dict(await entry.prepare(request) or {})


def _load_providers() -> None:
    from packages.core.ai.runtime import approval_service  # noqa: F401  (registers the built-in reviews)

    return None
