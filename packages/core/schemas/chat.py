"""Chat request/response schemas — shared between routers and services."""
from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None


class ChatMessageResponse(BaseModel):
    conversation_id: str
    message_id: str | None = None
    content: str
    tool_calls_made: list[str] = []
    usage: dict = {}
    rounds: int = 1
    stop_reason: str | None = None
    error: str | None = None
    limit_detail: dict | None = None
    hitl_requests: list[dict] | None = None
    attachments: list[dict] | dict | None = None


class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str | None = None
    tool_calls: list | dict | None = None
    assistant_blocks: list[dict] | None = None
    token_usage: dict | None = None
    stop_reason: str | None = None
    error: str | None = None
    limit_detail: dict | None = None
    hitl_requests: list[dict] | None = None
    attachments: list[dict] | dict | None = None
    created_at: str | None = None


class ConversationResponse(BaseModel):
    id: str
    entity_id: str
    user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    title: str | None = None
    summary: str | None = None
    channel: str = "web"
    status: str = "active"
    message_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None


class RenameConversationRequest(BaseModel):
    title: str


class CreateShareRequest(BaseModel):
    expires_hours: int | None = None


class ShareResponse(BaseModel):
    id: str
    conversation_id: str
    share_token: str
    expires_at: str | None = None
    is_active: bool = True
    created_at: str | None = None


class SharedConversationResponse(BaseModel):
    conversation: ConversationResponse
    messages: list[MessageResponse]
