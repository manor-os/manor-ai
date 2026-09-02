"""Shared route contract for first-token-sensitive chat/SSE paths."""
from __future__ import annotations

import re

CHAT_STREAM_ROUTE_EXAMPLES = (
    "/api/v1/chat/stream",
    "/api/v1/chat/runs/run-123/events",
    "/api/v1/public/chat/public-token/message/stream",
    "/api/v1/workspace-drafts/stream",
    "/api/v1/workspace-drafts/draft-123/messages/stream",
    "/api/v1/workspace-drafts/draft-123/finalize/stream",
    "/api/v1/agents/generate-stream",
    "/api/v1/agents/generate-draft-stream",
    "/api/v1/skills/generate-stream",
)

_CHAT_STREAM_EXACT_PATHS = {
    "/api/v1/chat/stream",
    "/api/v1/workspace-drafts/stream",
    "/api/v1/agents/generate-stream",
    "/api/v1/agents/generate-draft-stream",
    "/api/v1/skills/generate-stream",
}
_CHAT_STREAM_PATTERNS = (
    re.compile(r"^/api/v1/chat/runs/[^/]+/events$"),
    re.compile(r"^/api/v1/public/chat/[^/]+/message/stream$"),
    re.compile(r"^/api/v1/workspace-drafts/[^/]+/(messages|finalize)/stream$"),
)


def is_chat_stream_path(path: str) -> bool:
    return path in _CHAT_STREAM_EXACT_PATHS or any(pattern.match(path) for pattern in _CHAT_STREAM_PATTERNS)
