"""Public webchat endpoints — accessed via public_token.

Serves the public-facing chat interface for webchat channels.
Visitors access via a shareable URL containing the public_token.
Channels may optionally require a Manor login before a visitor can chat.

URLs:
  GET  /api/v1/public/chat/{token}             — get channel info (name, avatar, welcome)
  POST /api/v1/public/chat/{token}/session      — create or resume a chat session
  POST /api/v1/public/chat/{token}/message       — send a message
  POST /api/v1/public/chat/{token}/message/stream — send a message and stream reply
  GET  /api/v1/public/chat/{token}/messages      — poll for messages
  GET  /api/v1/public/chat/{token}/qr            — get QR code image (PNG)
  GET  /api/v1/public/chat/{token}/embed         — get embed URLs/snippet JSON
  GET  /api/v1/public/chat/{token}/embed.js      — get website embed script
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from html import escape
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.runtime import ChannelRuntimeContext, ChatSurface
from packages.core.cache import cache
from packages.core.contracts.webchat_page import (
    WebchatPage,
    public_webchat_page,
)
from packages.core.database import get_db
from packages.core.models.channel import ChannelConfig, ChannelContact
from packages.core.models.document import Channel
from packages.core.models.task import Conversation
from packages.core.models.user import User
from packages.core.config import get_settings
from packages.core.services.channel_bindings import (
    channel_runtime_config,
    channel_workspace_is_routable,
    resolve_public_webchat_channel_by_token,
)
from packages.core.services.channel_contacts import (
    channel_contact_claimed_by_other_user,
    channel_contact_requires_claimed_user,
    find_channel_session_contact,
    find_claimed_webchat_contact_for_user,
    link_channel_contact_to_user,
    upsert_channel_contact,
)
from packages.core.services.channel_conversations import (
    find_public_webchat_conversation_by_session,
    get_or_create_channel_conversation,
    list_public_webchat_messages,
)
from packages.core.services.runtime_file_context import (
    prepare_runtime_file_context_turn,
    runtime_message_with_file_attachments,
    runtime_saved_message_with_file_references,
)
from apps.api.deps import get_current_user, security
from apps.api.middleware.rate_limit import RateLimiter, client_ip
from apps.api.chat_audio import (
    ChatAudioScope,
    SPEECH_CHUNK_LENGTH,
    acquire_audio_lease,
    chat_speech_response,
    enforce_public_audio_budget,
    transcribe_chat_upload,
)
from apps.api.streaming_concurrency import acquire_chat_stream_lease

logger = logging.getLogger(__name__)

_BLOCKED_VISITOR_DETAIL = {
    "code": "visitor_blocked",
    "message": "This chat visitor is blocked.",
}

router = APIRouter(prefix="/api/v1/public/chat", tags=["public-chat"])
_CLOUD_EMAIL_VERIFICATION_ENABLED = False
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
_public_action_limiter = RateLimiter()
_PUBLIC_ACTION_QUEUE_ERROR = "Workflow could not be queued. Please try again."
_PUBLIC_ACTION_ATTEMPT_LIMIT = 30
_PUBLIC_ACTION_ATTEMPT_WINDOW_SECONDS = 300
_PUBLIC_ACTION_CREATE_LIMIT = 10
_PUBLIC_ACTION_CREATE_WINDOW_SECONDS = 3600
_PUBLIC_ACTION_RETRY_LIMIT = 6
_PUBLIC_ACTION_RETRY_WINDOW_SECONDS = 300

_QR_ECC_CODEWORDS_PER_BLOCK_LOW = [
    -1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18,
]
_QR_NUM_ERROR_CORRECTION_BLOCKS_LOW = [
    -1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4,
]


# ── Helpers ────────────────────────────────────────────────────────────────

async def _resolve_channel_by_token(
    db: AsyncSession, token: str,
) -> tuple[ChannelConfig, Channel]:
    """Find the ChannelConfig and Channel binding for a public_token."""
    cc, binding = await resolve_public_webchat_channel_by_token(db, token)
    if not cc:
        raise HTTPException(404, "Chat not found")
    if not binding:
        raise HTTPException(404, "Chat not configured")
    return cc, binding


async def _resolve_public_page(
    db: AsyncSession,
    *,
    value: object,
    binding: Channel,
) -> WebchatPage | None:
    """Resolve explicitly published Workspace references at read time."""
    from packages.core.services.webchat_page import resolve_workspace_webchat_page

    return await resolve_workspace_webchat_page(
        db,
        value=value,
        entity_id=binding.entity_id,
        workspace_id=binding.workspace_id,
    )


def _safe_next_path(token: str) -> str:
    return f"/chat/public/{token}"


def _public_web_base(request: Request) -> str:
    """Return the browser-facing app origin for public chat links.

    Thin alias around the shared :func:`apps.api.web_base.public_web_base`
    helper — kept here for the local module-private name so existing
    call sites don't need to change. The shared helper centralizes the
    APP_URL / X-Forwarded-Host / Host / PUBLIC_BASE_URL precedence so
    every backend-minted public URL resolves the same way.
    """
    from apps.api.web_base import public_web_base as _shared

    return _shared(request)


def _chat_auth_urls(config: dict[str, Any], token: str) -> tuple[str, str]:
    """Return login/signup URLs that send visitors back to this public chat."""
    next_path = quote(_safe_next_path(token), safe="")
    login_url = str(config.get("login_url") or f"/login?next={next_path}")
    signup_url = str(config.get("signup_url") or f"/login?tab=register&next={next_path}")
    return login_url, signup_url


def _public_chat_sender_display(
    *,
    user: User | None,
    contact: ChannelContact | None,
    conversation: Conversation | None = None,
) -> str | None:
    if user:
        return _display_name_for_user(user)
    profile = getattr(contact, "profile", None)
    profile = profile if isinstance(profile, dict) else {}
    meta = getattr(conversation, "meta", None)
    meta = meta if isinstance(meta, dict) else {}
    for value in (
        getattr(contact, "display_name", None),
        profile.get("verified_customer_name"),
        meta.get("visitor_name"),
        meta.get("sender_name"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return None


def _display_name_for_user(user: User) -> str:
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip()
    return user.display_name or full_name or user.email.split("@")[0]


async def _optional_current_user(request: Request, db: AsyncSession) -> User | None:
    """Use the shared auth contract, including token/session revocation."""
    credentials = await security(request)
    if not credentials:
        return None
    try:
        return await get_current_user(request, credentials=credentials, db=db)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        # Expired/revoked credentials can only use genuinely anonymous sessions.
        # The channel login gate and claimed-contact check still apply below.
        from packages.core.services.auth_context import set_current_mfa_verified

        set_current_mfa_verified(False)
        request.state.auth_claims = {}
        request.state.impersonation = None
        return None


def _qr_num_raw_data_modules(version: int) -> int:
    result = (16 * version + 128) * version + 64
    if version >= 2:
        num_align = version // 7 + 2
        result -= (25 * num_align - 10) * num_align - 55
        if version >= 7:
            result -= 36
    return result


def _qr_get_alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    size = version * 4 + 17
    num_align = version // 7 + 2
    step = 26 if version == 32 else ((version * 4 + num_align * 2 + 1) // (num_align * 2 - 2)) * 2
    result = [6]
    pos = size - 7
    for _ in range(num_align - 1):
        result.insert(1, pos)
        pos -= step
    return result


def _qr_gf_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        if (y >> i) & 1:
            z ^= x
    return z & 0xFF


def _qr_reed_solomon_generator(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _qr_gf_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _qr_gf_multiply(root, 0x02)
    return result


def _qr_reed_solomon_remainder(data: bytes, generator: list[int]) -> list[int]:
    result = [0] * len(generator)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(generator):
            result[i] ^= _qr_gf_multiply(coef, factor)
    return result


def _qr_bits_to_bytes(bits: list[int]) -> bytes:
    return bytes(
        sum(bits[i + j] << (7 - j) for j in range(8))
        for i in range(0, len(bits), 8)
    )


def _qr_add_ecc_and_interleave(data_codewords: bytes, version: int) -> bytes:
    num_blocks = _QR_NUM_ERROR_CORRECTION_BLOCKS_LOW[version]
    block_ecc_len = _QR_ECC_CODEWORDS_PER_BLOCK_LOW[version]
    raw_codewords = _qr_num_raw_data_modules(version) // 8
    num_short_blocks = num_blocks - raw_codewords % num_blocks
    short_block_len = raw_codewords // num_blocks
    generator = _qr_reed_solomon_generator(block_ecc_len)
    blocks: list[tuple[list[int], list[int]]] = []
    k = 0
    for i in range(num_blocks):
        dat_len = short_block_len - block_ecc_len + (0 if i < num_short_blocks else 1)
        dat = list(data_codewords[k:k + dat_len])
        k += dat_len
        ecc = _qr_reed_solomon_remainder(bytes(dat), generator)
        if i < num_short_blocks:
            dat.append(0)
        blocks.append((dat, ecc))

    result: list[int] = []
    for i in range(short_block_len - block_ecc_len + 1):
        for block_index, (dat, _) in enumerate(blocks):
            if i != short_block_len - block_ecc_len or block_index >= num_short_blocks:
                result.append(dat[i])
    for i in range(block_ecc_len):
        for _, ecc in blocks:
            result.append(ecc[i])
    return bytes(result)


def _qr_encode_data(data: str, version: int) -> bytes:
    payload = data.encode("utf-8")
    data_capacity_bits = (
        _qr_num_raw_data_modules(version) // 8
        - _QR_ECC_CODEWORDS_PER_BLOCK_LOW[version] * _QR_NUM_ERROR_CORRECTION_BLOCKS_LOW[version]
    ) * 8
    count_bits = 8 if version <= 9 else 16
    bits = [0, 1, 0, 0]
    bits.extend((len(payload) >> i) & 1 for i in reversed(range(count_bits)))
    for b in payload:
        bits.extend((b >> i) & 1 for i in reversed(range(8)))
    bits.extend([0] * min(4, data_capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    pad = (0xEC, 0x11)
    pad_index = 0
    while len(bits) < data_capacity_bits:
        bits.extend((pad[pad_index % 2] >> i) & 1 for i in reversed(range(8)))
        pad_index += 1
    return _qr_add_ecc_and_interleave(_qr_bits_to_bytes(bits), version)


def _qr_bch_remainder(value: int, poly: int, degree: int) -> int:
    value <<= degree
    while value.bit_length() >= poly.bit_length():
        value ^= poly << (value.bit_length() - poly.bit_length())
    return value


def _qr_draw_format_bits(modules: list[list[bool | None]], is_function: list[list[bool]], mask: int) -> None:
    size = len(modules)
    data = (0b01 << 3) | mask  # Low error correction.
    bits = ((data << 10) | _qr_bch_remainder(data, 0x537, 10)) ^ 0x5412

    def set_func(x: int, y: int, val: bool) -> None:
        modules[y][x] = val
        is_function[y][x] = True

    for i in range(6):
        set_func(8, i, ((bits >> i) & 1) != 0)
    set_func(8, 7, ((bits >> 6) & 1) != 0)
    set_func(8, 8, ((bits >> 7) & 1) != 0)
    set_func(7, 8, ((bits >> 8) & 1) != 0)
    for i in range(9, 15):
        set_func(14 - i, 8, ((bits >> i) & 1) != 0)
    for i in range(8):
        set_func(size - 1 - i, 8, ((bits >> i) & 1) != 0)
    for i in range(8, 15):
        set_func(8, size - 15 + i, ((bits >> i) & 1) != 0)
    set_func(8, size - 8, True)


def _qr_generate_matrix(data: str) -> list[list[bool]]:
    payload_len = len(data.encode("utf-8"))
    version = 1
    for candidate in range(1, len(_QR_ECC_CODEWORDS_PER_BLOCK_LOW)):
        data_capacity = (
            _qr_num_raw_data_modules(candidate) // 8
            - _QR_ECC_CODEWORDS_PER_BLOCK_LOW[candidate] * _QR_NUM_ERROR_CORRECTION_BLOCKS_LOW[candidate]
        )
        count_bytes = 1 if candidate <= 9 else 2
        if payload_len + count_bytes + 1 <= data_capacity:
            version = candidate
            break
    else:
        raise ValueError("QR payload is too long")

    size = version * 4 + 17
    modules: list[list[bool | None]] = [[None] * size for _ in range(size)]
    is_function = [[False] * size for _ in range(size)]

    def set_func(x: int, y: int, val: bool) -> None:
        modules[y][x] = val
        is_function[y][x] = True

    def draw_finder(cx: int, cy: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    dist = max(abs(dx), abs(dy))
                    set_func(x, y, dist != 2 and dist != 4)

    draw_finder(3, 3)
    draw_finder(size - 4, 3)
    draw_finder(3, size - 4)

    for i in range(size):
        if not is_function[6][i]:
            set_func(i, 6, i % 2 == 0)
        if not is_function[i][6]:
            set_func(6, i, i % 2 == 0)

    align_positions = _qr_get_alignment_positions(version)
    for y in align_positions:
        for x in align_positions:
            if is_function[y][x]:
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_func(x + dx, y + dy, max(abs(dx), abs(dy)) != 1)

    _qr_draw_format_bits(modules, is_function, 0)
    codewords = _qr_encode_data(data, version)
    bit_index = 0
    upward = True
    right = size - 1
    while right >= 1:
        if right == 6:
            right -= 1
        for vert in range(size):
            y = size - 1 - vert if upward else vert
            for j in range(2):
                x = right - j
                if is_function[y][x]:
                    continue
                bit = False
                if bit_index < len(codewords) * 8:
                    bit = ((codewords[bit_index >> 3] >> (7 - (bit_index & 7))) & 1) != 0
                    bit_index += 1
                if (x + y) % 2 == 0:
                    bit = not bit
                modules[y][x] = bit
        upward = not upward
        right -= 2

    return [[bool(cell) for cell in row] for row in modules]


def _qr_svg(data: str, *, box_size: int = 6, border: int = 4) -> str:
    matrix = _qr_generate_matrix(data)
    module_count = len(matrix)
    size = (module_count + border * 2) * box_size
    rects = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                rects.append(
                    f'<rect x="{(x + border) * box_size}" y="{(y + border) * box_size}" '
                    f'width="{box_size}" height="{box_size}"/>'
                )
    title = escape(data)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="QR code">'
        f'<title>{title}</title><rect width="100%" height="100%" fill="#fff"/>'
        f'<g fill="#000">{"".join(rects)}</g></svg>'
    )


async def _require_chat_access(
    cc: ChannelConfig,
    token: str,
    request: Request,
    db: AsyncSession,
    binding: Channel | None = None,
) -> User | None:
    """Enforce optional login_required while keeping public channels anonymous."""
    user = await _optional_current_user(request, db)
    config = channel_runtime_config(cc, binding)
    if bool(config.get("login_required")) and not user:
        login_url, signup_url = _chat_auth_urls(config, token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "message": "Please sign in to continue this chat.",
                "auth_required": True,
                "login_url": login_url,
                "signup_url": signup_url,
            },
        )
    if user and await find_claimed_webchat_contact_for_user(
        db, cc=cc, user=user, status="blocked",
    ):
        # A new or already-active session cannot reset this channel's block.
        raise HTTPException(status.HTTP_403_FORBIDDEN, _BLOCKED_VISITOR_DETAIL)
    return user


async def _external_reply_policy_blocks_stream(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
) -> bool:
    """Return true when public replies must go through the approval path.

    Public webchat is a live customer surface, so it auto-replies by default
    instead of waiting for the workspace owner to approve every response.
    """
    return False


async def _single_message_stream(
    *,
    conversation_id: str | None,
    content: str,
    status_value: str,
):
    from packages.core.services.sse_events import format_sse

    yield format_sse("stream_start", {"conversation_id": conversation_id})
    if content:
        yield format_sse("text_delta", {"content": content, "status": status_value})
    yield format_sse(
        "stream_end",
        {
            "conversation_id": conversation_id,
            "usage": {},
            "rounds": 0,
            "tool_calls": [],
            "status": status_value,
        },
    )


async def _ensure_session_contact_for_user(
    db: AsyncSession,
    *,
    cc: ChannelConfig,
    session_id: str,
    user: User | None,
    conversation: Conversation | None = None,
    sender_name: str | None = None,
) -> ChannelContact:
    """Find/create the session contact and claim it when login is required."""
    contact = await find_channel_session_contact(
        db,
        cc=cc,
        session_id=session_id,
        conversation=conversation,
    )
    if not contact:
        contact = await upsert_channel_contact(
            db,
            entity_id=cc.entity_id,
            channel_type="webchat",
            channel_config_id=cc.id,
            source_id=session_id,
            sender_name=sender_name,
        )
    # Only a different authenticated visitor may replace a blocked session;
    # missing or revoked credentials must still honor the known contact block.
    if user and channel_contact_claimed_by_other_user(contact, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "session_mismatch", "message": "This chat session belongs to another signed-in visitor."},
        )
    if contact.status == "blocked":
        raise HTTPException(status.HTTP_403_FORBIDDEN, _BLOCKED_VISITOR_DETAIL)
    if not user and channel_contact_requires_claimed_user(contact):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "session_mismatch", "message": "This chat session belongs to a signed-in visitor."},
        )
    if sender_name and not contact.display_name:
        contact.display_name = sender_name
    if user:
        link_channel_contact_to_user(
            contact,
            user,
            cc,
            display_name=_display_name_for_user(user),
        )
    if conversation is not None:
        meta = dict(conversation.meta or {})
        meta.update({
            "channel_contact_id": contact.id,
            "channel_config_id": cc.id,
            "sender_id": session_id,
            "sender_name": _public_chat_sender_display(
                user=user,
                contact=contact,
                conversation=conversation,
            ),
            "chat_id": session_id,
        })
        conversation.meta = meta
    return contact


# ── Request / Response models ──────────────────────────────────────────────

class SessionRequest(BaseModel):
    session_id: str | None = None  # resume existing, or None to create new
    visitor_name: str | None = None
    visitor_email: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    conversation_id: str
    channel_config_id: str


class MessageRequest(BaseModel):
    session_id: str
    text: str
    attachments: list[dict] | None = None


class CustomerRegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class PublicActionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    submission_id: str = Field(
        min_length=16,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    values: dict[str, str] = Field(default_factory=dict, max_length=6)

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or len(key) > 80 or not value.strip() or len(value) > 1000 for key, value in values.items()):
            raise ValueError("Public action fields must be 1-80 characters with values up to 1000 characters")
        return {key: value.strip() for key, value in values.items()}


class ChatInfoResponse(BaseModel):
    channel_name: str
    workspace_name: str | None = None
    agent_name: str | None = None
    agent_avatar: str | None = None
    welcome_message: str | None = None
    purpose: str | None = None
    language: str = "en"
    login_required: bool = False
    login_url: str | None = None
    signup_url: str | None = None
    auth_hint: str | None = None
    public_page: WebchatPage | None = None


class ChatEmbedResponse(BaseModel):
    public_chat_url: str
    qr_code_url: str
    embed_script_url: str
    embed_script: str


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/{token}", response_model=ChatInfoResponse)
async def get_chat_info(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Get public info about a webchat channel (name, welcome message, avatar)."""
    cc, binding = await _resolve_channel_by_token(db, token)

    # Load the bound agent for name + avatar
    agent_name = None
    agent_avatar = None
    agent_id = binding.agent_id
    if binding.agent_subscription_id:
        from packages.core.models.workspace import AgentSubscription
        sub = (await db.execute(
            select(AgentSubscription).where(AgentSubscription.id == binding.agent_subscription_id)
        )).scalar_one_or_none()
        if sub:
            agent_name = sub.name or agent_name
            agent_id = agent_id or sub.agent_id

    if agent_id:
        from packages.core.models.workspace import Agent
        agent = (await db.execute(
            select(Agent).where(Agent.id == agent_id)
        )).scalar_one_or_none()
        if agent:
            agent_name = agent_name or getattr(agent, "display_name", None) or agent.name
            agent_avatar = agent.avatar_url

    # Load the same deliberate public identity used by page modules/editor.
    workspace_name = None
    if binding.workspace_id:
        from packages.core.models.workspace import Workspace
        from packages.core.services.webchat_page import public_workspace_name

        workspace = await db.scalar(select(Workspace).where(
            Workspace.id == binding.workspace_id,
            Workspace.entity_id == binding.entity_id,
            Workspace.deleted_at.is_(None),
        ))
        if workspace:
            workspace_name = public_workspace_name(workspace) or None

    config = channel_runtime_config(cc, binding)
    login_url, signup_url = _chat_auth_urls(config, token)
    return ChatInfoResponse(
        channel_name=cc.name or "Chat",
        workspace_name=workspace_name,
        agent_name=agent_name,
        agent_avatar=agent_avatar,
        welcome_message=config.get("welcome_message"),
        purpose=config.get("purpose"),
        language=config.get("language", "en"),
        login_required=bool(config.get("login_required", False)),
        login_url=login_url,
        signup_url=signup_url,
        auth_hint=config.get("auth_hint"),
        public_page=await _resolve_public_page(
            db,
            value=config.get("public_page"),
            binding=binding,
        ),
    )


@router.post("/{token}/session", response_model=SessionResponse)
async def create_or_resume_session(
    token: str,
    req: SessionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new chat session or resume an existing one."""
    cc, binding = await _resolve_channel_by_token(db, token)
    user = await _require_chat_access(cc, token, request, db, binding)

    claimed_contact = await find_claimed_webchat_contact_for_user(
        db, cc=cc, user=user,
    ) if not req.session_id else None
    session_id = req.session_id or (claimed_contact.source_id if claimed_contact else None) or secrets.token_urlsafe(16)
    visitor_name = req.visitor_name or (_display_name_for_user(user) if user else None)
    visitor_email = req.visitor_email or (user.email if user else None)

    existing = await find_public_webchat_conversation_by_session(
        db,
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        session_id=session_id,
    )

    if existing:
        await _ensure_session_contact_for_user(
            db,
            cc=cc,
            session_id=session_id,
            user=user,
            conversation=existing,
            sender_name=visitor_name,
        )
        meta = dict(existing.meta or {})
        meta.update({
            "session_id": session_id,
            "visitor_name": visitor_name,
            "visitor_email": visitor_email,
            "channel_config_id": cc.id,
        })
        existing.meta = meta
        await db.flush()
        return SessionResponse(
            session_id=session_id,
            conversation_id=existing.id,
            channel_config_id=cc.id,
        )

    contact = await _ensure_session_contact_for_user(
        db,
        cc=cc,
        session_id=session_id,
        user=user,
        sender_name=visitor_name,
    )
    conv = await get_or_create_channel_conversation(
        db,
        entity_id=cc.entity_id,
        channel_type="webchat",
        channel_config_id=cc.id,
        channel_contact_id=contact.id,
        sender_id=session_id,
        sender_name=visitor_name,
        chat_id=session_id,
        agent_id=binding.agent_id,
        user_id=binding.user_id,
        workspace_id=binding.workspace_id,
        agent_subscription_id=binding.agent_subscription_id,
    )
    meta = dict(conv.meta or {})
    meta.update({
        "session_id": session_id,
        "visitor_name": visitor_name,
        "visitor_email": visitor_email,
        "channel_config_id": cc.id,
    })
    conv.meta = meta
    await db.flush()

    return SessionResponse(
        session_id=session_id,
        conversation_id=conv.id,
        channel_config_id=cc.id,
    )


@router.post("/{token}/actions/{module_id}", status_code=202)
async def run_public_page_action(
    token: str,
    module_id: str,
    body: PublicActionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Start one Workflow explicitly published by this Webchat binding."""
    cc, channel_binding = await _resolve_channel_by_token(db, token)
    user = await _require_chat_access(cc, token, request, db, channel_binding)
    config = channel_runtime_config(cc, channel_binding)
    if not channel_binding.workspace_id:
        raise HTTPException(404, "Public action not found")
    page = public_webchat_page(config.get("public_page"))
    module = next((item for item in page.modules if item.id == module_id), None) if page else None
    if module is None or module.type != "workspace_action":
        raise HTTPException(404, "Public action not found")
    if set(body.values) != set(module.fields):
        raise HTTPException(422, "Public action fields do not match the published form")

    actor_key = f"user:{user.id}" if user else f"ip:{client_ip(request)}"
    attempt_limit = await _public_action_limiter.check(
        f"webchat-action-attempt:{token}:{actor_key}",
        _PUBLIC_ACTION_ATTEMPT_LIMIT,
        _PUBLIC_ACTION_ATTEMPT_WINDOW_SECONDS,
    )
    if not attempt_limit.allowed:
        raise HTTPException(
            429,
            "Too many action attempts",
            headers={
                "Retry-After": str(
                    attempt_limit.retry_after
                    or _PUBLIC_ACTION_ATTEMPT_WINDOW_SECONDS
                )
            },
        )

    conversation = await find_public_webchat_conversation_by_session(
        db,
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        session_id=body.session_id,
    )
    if conversation is None:
        raise HTTPException(404, "Chat session not found")
    await _ensure_session_contact_for_user(
        db,
        cc=cc,
        session_id=body.session_id,
        user=user,
        conversation=conversation,
        sender_name=_display_name_for_user(user) if user else None,
    )

    from packages.core.models.workflow import WorkflowBinding, WorkflowRun
    from packages.core.services.workflow_service import start_workflow_from_binding

    workflow_binding = await db.scalar(
        select(WorkflowBinding)
        .where(
            WorkflowBinding.id == module.binding_id,
            WorkflowBinding.entity_id == cc.entity_id,
            WorkflowBinding.workspace_id == channel_binding.workspace_id,
            WorkflowBinding.trigger_type == "manual",
            WorkflowBinding.enabled.is_(True),
            WorkflowBinding.status == "active",
        )
        .with_for_update()
    )
    if workflow_binding is None:
        raise HTTPException(409, "Public action is unavailable")
    existing_run = await db.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.binding_id == workflow_binding.id,
            WorkflowRun.entity_id == cc.entity_id,
            WorkflowRun.workspace_id == channel_binding.workspace_id,
            WorkflowRun.trigger_source == "public_webchat",
            WorkflowRun.webchat_submission_id == body.submission_id,
            WorkflowRun.webchat_module_id == module.id,
            WorkflowRun.webchat_session_id == body.session_id,
        )
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
    )
    if existing_run is not None:
        if existing_run.status == "pending":
            retry_limit = await _public_action_limiter.check(
                f"webchat-action-retry:{token}:{body.submission_id}:{actor_key}",
                _PUBLIC_ACTION_RETRY_LIMIT,
                _PUBLIC_ACTION_RETRY_WINDOW_SECONDS,
            )
            if not retry_limit.allowed:
                await db.commit()
                raise HTTPException(
                    429,
                    "Too many action retry attempts",
                    headers={
                        "Retry-After": str(
                            retry_limit.retry_after
                            or _PUBLIC_ACTION_RETRY_WINDOW_SECONDS
                        )
                    },
                )
            from packages.core.ai.workflow_runner import WorkflowRunner

            queued = WorkflowRunner.enqueue(existing_run.id)
            if not queued:
                await db.commit()
                raise HTTPException(503, _PUBLIC_ACTION_QUEUE_ERROR)
        await db.commit()
        return {"accepted": True, "queued": True, "duplicate": True}

    creation_limit = await _public_action_limiter.check(
        f"webchat-action-create:{token}:{actor_key}",
        _PUBLIC_ACTION_CREATE_LIMIT,
        _PUBLIC_ACTION_CREATE_WINDOW_SECONDS,
    )
    if not creation_limit.allowed:
        raise HTTPException(
            429,
            "Too many action submissions",
            headers={
                "Retry-After": str(
                    creation_limit.retry_after
                    or _PUBLIC_ACTION_CREATE_WINDOW_SECONDS
                )
            },
        )
    trigger_data = {
        **body.values,
        "fields": body.values,
        "webchat_submission_id": body.submission_id,
        "webchat_module_id": module.id,
        "webchat_session_id": body.session_id,
        "webchat_channel_config_id": cc.id,
    }
    try:
        run = await start_workflow_from_binding(
            db,
            workflow_binding,
            trigger_data=trigger_data,
            trigger_source="public_webchat",
            started_by=user.id if user else None,
            execution_workspace_id=channel_binding.workspace_id,
        )
    except ValueError as exc:
        raise HTTPException(409, "Public action is unavailable") from exc
    # Persist a durable dispatch boundary before talking to the broker. The
    # runner transitions pending -> running after it acquires execution.
    run.status = "pending"
    run.webchat_session_id = body.session_id
    run.webchat_module_id = module.id
    run.webchat_submission_id = body.submission_id
    await db.commit()

    from packages.core.ai.workflow_runner import WorkflowRunner

    queued = WorkflowRunner.enqueue(run.id)
    if not queued:
        raise HTTPException(503, _PUBLIC_ACTION_QUEUE_ERROR)
    return {"accepted": True, "queued": True}


@router.post("/{token}/auth/register")
async def register_customer_for_chat(
    token: str,
    req: CustomerRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a customer account from a public chat without Manor invite gates."""
    cc, _ = await _resolve_channel_by_token(db, token)
    config = cc.config or {}
    if not bool(config.get("login_required")):
        raise HTTPException(400, "This chat does not require a customer account.")

    from packages.core.services.auth_service import (
        create_access_token,
        mark_user_login,
        register_user,
    )

    try:
        user, entity = await register_user(
            db,
            email=req.email.strip().lower(),
            password=req.password,
            entity_name=f"{(req.display_name or req.email).strip()}'s Customer Account",
            display_name=(req.display_name or "").strip(),
        )
    except ValueError as e:
        detail = str(e)
        if "already registered" in detail:
            detail = detail.replace("already registered", "already taken")
        raise HTTPException(400, detail)
    user.role = "external"

    if _CLOUD_EMAIL_VERIFICATION_ENABLED:
        from packages.core.services.email_service import send_verification_email
        from packages.core.services.email_verification_service import create_verification

        user.status = "pending"
        await db.commit()
        code = await create_verification(
            user.email,
            user.id,
            password_hash=user.password_hash,
        )
        await send_verification_email(user.email, code)
        return {
            "requires_verification": True,
            "email": user.email,
            "message": "Verification code sent to your email",
        }

    mark_user_login(user, source="public_chat.register")
    await db.commit()
    token_value = create_access_token(
        user.id,
        entity.id,
        user.role,
        token_version=user.token_version,
    )
    return {
        "access_token": token_value,
        "token_type": "bearer",
        "user_id": user.id,
        "entity_id": entity.id,
        "role": user.role,
    }


@router.post("/{token}/message")
async def send_message(
    token: str,
    req: MessageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Send a visitor message and trigger agent response."""
    cc, binding = await _resolve_channel_by_token(db, token)
    user = await _require_chat_access(cc, token, request, db, binding)
    sender_name = _display_name_for_user(user) if user else None

    # Persist a verified customer/staff identity before dispatch_inbound opens
    # its own DB session and resolves the same ChannelContact.
    await _ensure_session_contact_for_user(
        db,
        cc=cc,
        session_id=req.session_id,
        user=user,
        sender_name=sender_name,
    )
    await db.commit()

    # Dispatch through the standard channel gateway so the agent runs,
    # tools fire, conversation history persists, and usage is recorded.
    from packages.core.services.channel_gateway import dispatch_inbound

    result = await dispatch_inbound(
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        channel_type="webchat",
        sender_id=req.session_id,
        sender_name=sender_name,
        chat_id=req.session_id,
        content=req.text,
        attachments=req.attachments,
        runtime_metadata=(
            {
                "voice_session_mode": "chat_gateway",
                **(
                    {
                        "voice_origin_message_id": str(
                            request.state.voice_origin_message_id
                        ),
                        "origin_user_message_id": str(
                            request.state.voice_origin_message_id
                        ),
                    }
                    if getattr(request.state, "voice_origin_message_id", None)
                    else {}
                ),
            }
            if getattr(request.state, "voice_session_mode", None) == "chat_gateway"
            else None
        ),
    )

    response = {
        "status": result.get("status", "ok"),
        "reply": result.get("reply"),
    }
    for key in (
        "sent",
        "reason",
        "conversation_id",
        "approval_message_id",
        "blocked_message_id",
        "matched_rule",
    ):
        if key in result:
            response[key] = result.get(key)
    return response


@router.post("/{token}/message/stream")
async def stream_message(
    token: str,
    request: Request,
    session_id: str = Form(...),
    message: str = Form(""),
    client_turn_id: str | None = Form(None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    files: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """Send a visitor message and stream the AI response via SSE."""
    text = (message or "").strip()
    if not text and not files:
        raise HTTPException(422, "message or file is required")

    lease = await acquire_chat_stream_lease(scope="public-chat")
    try:
        cc, binding = await _resolve_channel_by_token(db, token)
        user = await _require_chat_access(cc, token, request, db, binding)
        sender_name = _display_name_for_user(user) if user else None

        contact = await _ensure_session_contact_for_user(
            db,
            cc=cc,
            session_id=session_id,
            user=user,
            sender_name=sender_name,
        )

        from packages.core.services.agent_subscription_service import resolve_subscription
        from packages.core.services.channel_gateway import dispatch_inbound
        from packages.core.services.conversation_messages import add_message
        from packages.core.services.conversation_messages import create_assistant_stream_placeholder
        from packages.core.ai.runtime import runtime_stream_chat_turn

        sub = await resolve_subscription(db, binding=binding, contact=contact)
        if sub.source == "invalid":
            raise HTTPException(404, "Chat not configured")
        if not await channel_workspace_is_routable(
            db,
            sub.workspace_id,
            entity_id=cc.entity_id,
        ):
            raise HTTPException(404, "Chat not found")

        if await _external_reply_policy_blocks_stream(
            db,
            entity_id=cc.entity_id,
            workspace_id=sub.workspace_id,
        ):
            await db.commit()
            attachment_names = [f.filename or "attachment" for f in files]
            dispatch_text = text or "Attached file(s)"
            if attachment_names:
                dispatch_text = (
                    f"{dispatch_text}\n\n"
                    f"[Attached files: {', '.join(attachment_names)}]"
                ).strip()
            result = await dispatch_inbound(
                entity_id=cc.entity_id,
                channel_config_id=cc.id,
                channel_type="webchat",
                sender_id=session_id,
                sender_name=sender_name,
                chat_id=session_id,
                content=dispatch_text,
                attachments=[
                    {
                        "name": f.filename or "attachment",
                        "content_type": f.content_type,
                    }
                    for f in files
                ] or None,
            )
            status_value = str(result.get("status") or "ok")
            if status_value == "approval_required":
                notice = "Thanks, your message was sent. The reply is waiting for team approval and will appear here once approved."
            elif status_value in {"blocked_by_governance", "no_reply", "error"}:
                notice = "Thanks, your message was sent. The team will follow up here shortly."
            else:
                notice = str(result.get("reply") or "Message sent. Waiting for a reply...")
            return StreamingResponse(
                lease.wrap(_single_message_stream(
                    conversation_id=result.get("conversation_id"),
                    content=notice,
                    status_value=status_value,
                )),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        contact_user_id = getattr(contact, "user_id", None)
        profile = contact.profile or {}
        verified_customer_user_id = profile.get("verified_customer_user_id")
        tool_user_id = (
            user.id
            if user
            else contact_user_id
            or verified_customer_user_id
        )

        conv = await get_or_create_channel_conversation(
            db,
            entity_id=cc.entity_id,
            channel_type="webchat",
            channel_config_id=cc.id,
            channel_contact_id=contact.id,
            sender_id=session_id,
            sender_name=sender_name,
            chat_id=session_id,
            agent_id=sub.agent_id,
            user_id=contact_user_id or binding.user_id,
            workspace_id=sub.workspace_id,
            agent_subscription_id=sub.id,
        )
        sender_display = _public_chat_sender_display(
            user=user,
            contact=contact,
            conversation=conv,
        )
        meta = dict(conv.meta or {})
        meta.update({
            "session_id": session_id,
            "visitor_name": sender_display,
            "visitor_email": user.email if user else profile.get("verified_customer_email"),
            "channel_config_id": cc.id,
            "sender_id": session_id,
            "sender_name": sender_display,
            "chat_id": session_id,
        })
        conv.meta = meta

        file_context_turn = await prepare_runtime_file_context_turn(
            message=text,
            document_ids=[],
            files=files,
            entity_id=cc.entity_id,
            db=db,
            workspace_id=sub.workspace_id,
            user_id=tool_user_id,
            surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
        )
        llm_base_message = file_context_turn.cleaned_message or "Please review the attached file(s)."
        llm_message = runtime_message_with_file_attachments(
            llm_base_message,
            file_context_turn.attachments,
        )
        saved_text = runtime_saved_message_with_file_references(
            text or "Attached file(s)",
            file_context_turn.attachments,
        )
        # Correlation survives losing the first SSE event; it grants no access.
        turn_meta = {"client_turn_id": client_turn_id} if client_turn_id else {}
        await add_message(
            db,
            conv.id,
            role="user",
            content=saved_text,
            meta={
                "channel_type": "webchat",
                "sender_id": session_id,
                "sender_name": sender_display,
                "chat_id": session_id,
                "attachment_count": len(files),
                **turn_meta,
            },
        )
        assistant_placeholder = await create_assistant_stream_placeholder(
            db,
            conv.id,
            entity_id=cc.entity_id,
            workspace_id=sub.workspace_id,
            agent_id=sub.agent_id,
            meta={
                "channel_type": "webchat",
                "sender_id": session_id,
                "chat_id": session_id,
                "session_id": session_id,
                **turn_meta,
            },
        )
        await db.commit()

        return StreamingResponse(
            lease.wrap(runtime_stream_chat_turn(
                llm_message,
                conv.id,
                surface=ChatSurface.PUBLIC_CUSTOMER_CHAT,
                entity_id=cc.entity_id,
                user_id=tool_user_id,
                agent_id=sub.agent_id,
                workspace_id=sub.workspace_id,
                assistant_message_id=assistant_placeholder.id,
                channel_context=ChannelRuntimeContext(
                    channel_type="webchat",
                    source_id=session_id,
                    display_name=sender_display,
                    user_id=tool_user_id,
                    role=getattr(contact, "role", None) or "external",
                    is_verified=bool(tool_user_id),
                    conversation_id=conv.id,
                    channel_contact_id=contact.id,
                    channel_language=channel_runtime_config(cc, binding).get("language"),
                ),
                runtime_metadata={
                    "channel_label": cc.name or binding.name or "public webchat",
                    "channel_language": channel_runtime_config(cc, binding).get("language"),
                    "visitor_entity_id": getattr(user, "entity_id", None),
                    "visitor_verified": bool(user),
                    **file_context_turn.runtime_metadata,
                },
            )),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    except BaseException:
        await lease.release()
        raise


class PublicSpeechRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=128)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=SPEECH_CHUNK_LENGTH, ge=1, le=SPEECH_CHUNK_LENGTH)


async def _public_audio_scope(db, token, request, session_id):
    cc, binding = await _resolve_channel_by_token(db, token)
    user = await _require_chat_access(cc, token, request, db, binding)
    conv = await find_public_webchat_conversation_by_session(
        db, entity_id=cc.entity_id, channel_config_id=cc.id, session_id=session_id,
    )
    if not conv:
        raise HTTPException(404, "Chat session not found")
    contact = await _ensure_session_contact_for_user(
        db, cc=cc, session_id=session_id, user=user, conversation=conv,
    )
    from packages.core.services.agent_subscription_service import resolve_subscription

    sub = await resolve_subscription(db, binding=binding, contact=contact)
    if sub.source == "invalid" or not await channel_workspace_is_routable(
        db, sub.workspace_id, entity_id=cc.entity_id,
    ):
        raise HTTPException(404, "Chat not found")
    # Use the channel's configured billing owner, never an anonymous visitor
    # or a customer account from a different Entity.
    owner_id = cc.owner_user_id
    if not owner_id:
        raise HTTPException(503, "Chat voice is not configured")
    return ChatAudioScope(cc.entity_id, owner_id, sub.workspace_id, conv.id, sub.agent_id)


@router.post("/{token}/audio/transcribe")
async def transcribe_public_chat_audio(
    token: str,
    request: Request,
    session_id: str = Form(..., min_length=1, max_length=128),
    file: UploadFile = File(...),
    language: str | None = Form(None, max_length=35),
    db: AsyncSession = Depends(get_db),
):
    await enforce_public_audio_budget(request, token, session_id, "transcribe")
    scope = await _public_audio_scope(db, token, request, session_id)
    lease = await acquire_audio_lease()
    try:
        return await transcribe_chat_upload(db, scope, file, language)
    finally:
        await lease.release()


@router.post("/{token}/audio/speech")
async def public_chat_speech(
    token: str,
    body: PublicSpeechRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await enforce_public_audio_budget(request, token, body.session_id, "speech")
    scope = await _public_audio_scope(db, token, request, body.session_id)
    messages = await list_public_webchat_messages(
        db, scope.conversation_id, session_id=body.session_id, message_ids=[body.message_id],
    )
    message = next((m for m in messages if m["id"] == body.message_id and m["role"] == "assistant"), None)
    if not message:
        raise HTTPException(404, "Reply not found")
    if message.get("stream_status") in {"running", "streaming", "interrupted", "error"}:
        raise HTTPException(409, "Reply is not complete")
    # Never accept arbitrary public TTS text or read internal/unpublished replies.
    text = message["content"][body.offset:body.offset + body.length]
    cache_identity = hashlib.sha256(
        "\0".join(
            (
                scope.entity_id,
                scope.conversation_id or "",
                body.message_id,
                str(body.offset),
                str(body.length),
                text,
            )
        ).encode("utf-8")
    ).hexdigest()
    cache_key = f"public-voice-speech:v1:{cache_identity}"
    cached = await cache.get(cache_key)
    if isinstance(cached, dict):
        encoded = cached.get("audio")
        media_type = cached.get("media_type")
        if isinstance(encoded, str) and isinstance(media_type, str):
            try:
                audio = base64.b64decode(encoded, validate=True)
            except ValueError:
                audio = b""
            if audio:
                return Response(
                    audio,
                    media_type=media_type,
                    headers={"Cache-Control": "private, no-store"},
                )
    lease = await acquire_audio_lease()
    try:
        response = await chat_speech_response(db, scope, text)
        await cache.set(
            cache_key,
            {
                "audio": base64.b64encode(bytes(response.body)).decode("ascii"),
                "media_type": response.media_type,
            },
            ttl=10 * 60,
        )
        return response
    finally:
        await lease.release()


@router.get("/{token}/messages")
async def poll_messages(
    token: str,
    request: Request,
    session_id: str = Query(...),
    after: str = Query("", description="Message ID to fetch after (for pagination)"),
    refresh: list[str] = Query(default=[], max_length=50, description="Known message IDs to refresh after streaming"),
    db: AsyncSession = Depends(get_db),
):
    """Poll for messages in a webchat session. Returns newest messages."""
    cc, binding = await _resolve_channel_by_token(db, token)
    user = await _require_chat_access(cc, token, request, db, binding)

    conv = await find_public_webchat_conversation_by_session(
        db,
        entity_id=cc.entity_id,
        channel_config_id=cc.id,
        session_id=session_id,
    )

    if not conv:
        return {"messages": []}

    await _ensure_session_contact_for_user(
        db,
        cc=cc,
        session_id=session_id,
        user=user,
        conversation=conv,
    )

    response = {
        "messages": await list_public_webchat_messages(
            db,
            conv.id,
            session_id=session_id,
            after=after or None,
        ),
    }
    if refresh:
        response["updates"] = await list_public_webchat_messages(
            db, conv.id, session_id=session_id, message_ids=refresh,
        )
    return response


@router.get("/{token}/qr")
async def get_qr_code(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Generate a QR code image (PNG) for this webchat channel."""
    # Validate the token exists
    await _resolve_channel_by_token(db, token)

    # Build the public chat URL
    base_url = _public_web_base(request)
    chat_url = f"{base_url}/chat/public/{token}"

    # Generate QR code as PNG bytes when qrcode[pil] is installed. Otherwise
    # return an SVG QR generated by the lightweight fallback above.
    try:
        import qrcode
        from io import BytesIO
        qr = qrcode.QRCode(version=None, box_size=10, border=4)
        qr.add_data(chat_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception:
        return Response(
            content=_qr_svg(chat_url),
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=3600"},
        )


@router.get("/{token}/embed", response_model=ChatEmbedResponse)
async def get_embed_metadata(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return copyable public URLs for website/webchat integration."""
    await _resolve_channel_by_token(db, token)
    base_url = _public_web_base(request)
    embed_script_url = f"{base_url}/api/v1/public/chat/{token}/embed.js"
    return ChatEmbedResponse(
        public_chat_url=f"{base_url}/chat/public/{token}",
        qr_code_url=f"{base_url}/api/v1/public/chat/{token}/qr",
        embed_script_url=embed_script_url,
        embed_script=f'<script async src="{embed_script_url}"></script>',
    )


@router.get("/{token}/embed.js")
async def get_embed_script(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Return a self-contained script that mounts webchat in the page corner."""
    cc, _ = await _resolve_channel_by_token(db, token)
    token_js = json.dumps(token)
    label_js = json.dumps(cc.name or "Manor AI")
    script = f"""(function() {{
  var token = {token_js};
  var defaultLabel = {label_js};
  var currentScript = document.currentScript;
  var options = currentScript ? currentScript.dataset : {{}};
  var rootId = "manor-webchat-root-" + token.replace(/[^a-zA-Z0-9_-]/g, "");
  if (document.getElementById(rootId)) return;

  function appOrigin() {{
    try {{
      if (currentScript && currentScript.src) {{
        return new URL(currentScript.src).origin;
      }}
    }} catch (err) {{}}
    return window.location.origin;
  }}

  function resolvedColor(value) {{
    if (!value || !document.body) return "";
    var probe = document.createElement("span");
    probe.style.color = String(value).trim();
    if (!probe.style.color) return "";
    probe.style.display = "none";
    document.body.appendChild(probe);
    var result = window.getComputedStyle(probe).color || "";
    probe.remove();
    return result;
  }}

  function firstThemeValue(styles, names) {{
    for (var i = 0; i < names.length; i += 1) {{
      var value = styles.getPropertyValue(names[i]).trim();
      if (value) return value;
    }}
    return "";
  }}

  function rgbChannels(value) {{
    var match = String(value || "").match(/rgba?\\(\\s*([\\d.]+)[,\\s]+([\\d.]+)[,\\s]+([\\d.]+)/i);
    if (!match) return null;
    return [Number(match[1]), Number(match[2]), Number(match[3])];
  }}

  function luminance(value) {{
    var channels = rgbChannels(value);
    if (!channels) return 1;
    var linear = channels.map(function (channel) {{
      var part = Math.max(0, Math.min(255, channel)) / 255;
      return part <= 0.03928 ? part / 12.92 : Math.pow((part + 0.055) / 1.055, 2.4);
    }});
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  }}

  function siteTheme() {{
    var rootStyles = window.getComputedStyle(document.documentElement);
    var bodyStyles = window.getComputedStyle(document.body);
    var themeMeta = document.querySelector('meta[name="theme-color"]');
    var accent = resolvedColor(
      options.color ||
      firstThemeValue(rootStyles, [
        "--manor-chat-accent", "--brand", "--primary", "--color-primary",
        "--accent-color", "--accent"
      ]) ||
      (themeMeta ? themeMeta.getAttribute("content") : "")
    ) || "rgb(67, 107, 101)";
    var surface = resolvedColor(
      options.surface ||
      firstThemeValue(rootStyles, [
        "--manor-chat-surface", "--bg", "--background", "--surface-app", "--surface"
      ]) ||
      bodyStyles.backgroundColor
    ) || "rgb(255, 255, 255)";
    var text = resolvedColor(
      options.text ||
      firstThemeValue(rootStyles, ["--manor-chat-text", "--text", "--foreground", "--text-strong"]) ||
      bodyStyles.color
    ) || "rgb(28, 25, 23)";
    var muted = resolvedColor(
      options.muted ||
      firstThemeValue(rootStyles, ["--manor-chat-muted", "--muted", "--text-muted"])
    ) || (luminance(surface) < 0.32 ? "rgb(184, 192, 217)" : "rgb(120, 113, 108)");
    var mode = options.theme === "dark" || options.theme === "light"
      ? options.theme
      : (luminance(surface) < 0.32 ? "dark" : "light");
    var font = String(options.font || bodyStyles.fontFamily || "").replace(/[;{{}}<>]/g, "").slice(0, 180);
    return {{
      accent: accent,
      accentText: luminance(accent) > 0.52 ? "rgb(17, 24, 39)" : "rgb(255, 255, 255)",
      surface: surface,
      text: text,
      muted: muted,
      mode: mode,
      font: font
    }};
  }}

  function mount() {{
    var origin = appOrigin();
    var theme = siteTheme();
    var chatParams = new URLSearchParams({{
      embed: "1",
      theme: theme.mode,
      accent: theme.accent,
      accentText: theme.accentText,
      surface: theme.surface,
      text: theme.text,
      muted: theme.muted,
      font: theme.font
    }});
    var chatUrl = origin + "/chat/public/" + encodeURIComponent(token) + "?" + chatParams.toString();
    var root = document.createElement("div");
    root.id = rootId;
    root.style.position = "fixed";
    root.style.right = options.right || "24px";
    root.style.bottom = options.bottom || "24px";
    root.style.zIndex = options.zIndex || "2147483000";
    root.style.fontFamily = theme.font || "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif";

    var iframe = document.createElement("iframe");
    iframe.src = chatUrl;
    iframe.title = (options.title || defaultLabel || "Manor AI") + " chat";
    iframe.allow = "clipboard-write; microphone";
    iframe.style.display = "none";
    iframe.style.position = "absolute";
    iframe.style.right = "0";
    iframe.style.bottom = "68px";
    iframe.style.width = "min(380px, calc(100vw - 32px))";
    iframe.style.height = "min(620px, calc(100vh - 112px))";
    iframe.style.border = "0";
    iframe.style.borderRadius = "20px";
    iframe.style.boxShadow = "0 24px 80px rgba(15, 23, 42, 0.28)";
    iframe.style.background = theme.surface;
    iframe.style.overflow = "hidden";

    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-label", options.label || "Open chat");
    button.title = options.label || "Open chat";
    button.style.width = "58px";
    button.style.height = "58px";
    button.style.borderRadius = "999px";
    button.style.border = "0";
    button.style.background = theme.accent;
    button.style.color = theme.accentText;
    button.style.boxShadow = "0 16px 40px rgba(0, 0, 0, 0.30)";
    button.style.display = "flex";
    button.style.alignItems = "center";
    button.style.justifyContent = "center";
    button.style.cursor = "pointer";
    button.style.transition = "transform 160ms ease, box-shadow 160ms ease";
    button.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4.5 6.75A3.25 3.25 0 0 1 7.75 3.5h8.5a3.25 3.25 0 0 1 3.25 3.25v6.5a3.25 3.25 0 0 1-3.25 3.25H12l-4.2 3.15a.8.8 0 0 1-1.28-.64V16.4A3.25 3.25 0 0 1 4.5 13.25v-6.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 9.25h8M8 12.25h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

    var open = false;
    function setOpen(next) {{
      var wasOpen = open;
      open = next;
      iframe.style.display = open ? "block" : "none";
      button.setAttribute("aria-label", open ? "Close chat" : (options.label || "Open chat"));
      button.innerHTML = open
        ? '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
        : '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4.5 6.75A3.25 3.25 0 0 1 7.75 3.5h8.5a3.25 3.25 0 0 1 3.25 3.25v6.5a3.25 3.25 0 0 1-3.25 3.25H12l-4.2 3.15a.8.8 0 0 1-1.28-.64V16.4A3.25 3.25 0 0 1 4.5 13.25v-6.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M8 9.25h8M8 12.25h5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
      if (open && !wasOpen) {{
        window.dispatchEvent(new CustomEvent("manor:webchat-open", {{ detail: {{ token: token }} }}));
      }}
    }}

    button.addEventListener("mouseenter", function() {{
      button.style.transform = "translateY(-1px)";
      button.style.boxShadow = "0 20px 48px rgba(0, 0, 0, 0.36)";
    }});
    button.addEventListener("mouseleave", function() {{
      button.style.transform = "translateY(0)";
      button.style.boxShadow = "0 16px 40px rgba(0, 0, 0, 0.30)";
    }});
    button.addEventListener("click", function() {{
      setOpen(!open);
    }});

    var focusStyles = document.createElement("style");
    focusStyles.textContent =
      "#" + rootId + " button:focus-visible{{outline:3px solid " + theme.accent + ";outline-offset:3px}}" +
      "@media (prefers-reduced-motion: reduce){{#" + rootId + " button{{transition:none!important}}}}";
    root.appendChild(focusStyles);
    root.appendChild(iframe);
    root.appendChild(button);
    document.body.appendChild(root);
    if (options.open === "true") setOpen(true);
  }}

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", mount, {{ once: true }});
  }} else {{
    mount();
  }}
}})();
"""
    return Response(
        content=script,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )
