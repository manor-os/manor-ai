"""Conversational workspace draft endpoints.

The user creates a draft, has a multi-turn LLM conversation that fills in
the operating model, optionally accepts a marketplace blueprint suggestion,
then finalizes -- which materializes a real Workspace via the same code
path the in-place setup wizard uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from packages.core.ai.tools.workspace_arch_tools import PENDING_QUESTION_FIELD
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.constants.workspace_drafts import STAFF_REQUEST_TEXT_FIELD
from packages.core.database import async_session, get_db
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.user import User
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_draft_service as draft_service
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
)
from packages.core.services.plan_gate import WorkspacePlanLimitError
from packages.core.services.sse_events import format_sse
from packages.core.services.workspace_draft_sources import strip_source_text
from apps.api.deps import enforce_plan_resource, get_current_user, require_plan
from apps.api.streaming_concurrency import LeasedStreamingResponse, acquire_chat_stream_lease

logger = logging.getLogger(__name__)


_sse = format_sse

_DRAFT_STREAM_KEEPALIVE_SECONDS = 15.0
# An Architect turn that sends nothing but keepalives for this long is stuck
# (capability matching is bounded well below it; a design wave has its own
# longer bound and reports progress meanwhile): stop it and free its chat stream
# slot. Its saved request runs it again, so the client is told it continues.
# Longer than one model round that streams only tool-call arguments, which sends
# no events either.
_DRAFT_TURN_STALL_SECONDS = 420.0
# A turn waits for another live turn only this far into the stall window, so a
# stall always finds this turn's message saved.
_DRAFT_TURN_STALL_CLAIM_MARGIN_SECONDS = 60.0


def _draft_turn_stall_seconds() -> float:
    try:
        seconds = float(os.getenv("WORKSPACE_DRAFT_STREAM_STALL_SECONDS") or _DRAFT_TURN_STALL_SECONDS)
    except ValueError:
        seconds = _DRAFT_TURN_STALL_SECONDS
    return seconds if seconds > 0 else _DRAFT_TURN_STALL_SECONDS


def _is_turn_progress(frame: str) -> bool:
    return not frame.startswith("event: keepalive\n")


def _draft_turn_continuing_frame() -> str:
    return _sse("continuing", {"working": True})


class _HiddenBlockTokenFilter:
    """Drop the model's hidden ``<workspace_setup>...</workspace_setup>``
    JSON status block from a token stream as it arrives.

    The setup wizard prompt instructs the model to emit a JSON status
    block at the end of every reply. That block is for the orchestrator,
    not the user, and ``_hydrate_response`` strips it from persisted
    transcripts. But the streaming pipe sees raw tokens, so without a
    filter the user briefly sees the JSON typed out before the final
    ``done`` event replaces the bubble. This filter:

    * Accumulates raw text.
    * Emits any prefix that's safe (not the start of the hidden tag).
    * Once it finds ``<workspace_setup>``, stops emitting; everything
      after that is dropped from the visible stream.
    * Holds back any trailing characters that could still be the start
      of the tag (look-behind), and flushes them on the next chunk if
      they turn out not to be.
    """

    HIDDEN_TAG = "<workspace_setup>"

    def __init__(self) -> None:
        self._raw = ""
        self._emitted = 0
        self._hidden = False

    def reset(self) -> None:
        self._raw = ""
        self._emitted = 0
        self._hidden = False

    def feed(self, chunk: str) -> str:
        if self._hidden:
            self._raw += chunk
            return ""

        self._raw += chunk
        idx = self._raw.find(self.HIDDEN_TAG, self._emitted)
        if idx >= 0:
            visible = self._raw[self._emitted:idx]
            self._emitted = idx
            self._hidden = True
            return visible

        # Hold back any trailing chars that could be the start of the tag.
        tail_hold = 0
        max_check = min(len(self.HIDDEN_TAG), len(self._raw) - self._emitted)
        for i in range(max_check, 0, -1):
            if self._raw.endswith(self.HIDDEN_TAG[:i]):
                tail_hold = i
                break

        emit_until = len(self._raw) - tail_hold
        if emit_until <= self._emitted:
            return ""
        visible = self._raw[self._emitted:emit_until]
        self._emitted = emit_until
        return visible

router = APIRouter(prefix="/api/v1/workspace-drafts", tags=["workspace-drafts"])


# ── Schemas ─────────────────────────────────────────────────────────────────

class BlueprintSuggestionResponse(BaseModel):
    id: str
    title: str
    summary: Optional[str] = None
    tags: list[str] = []
    install_count: int = 0


class DraftResponse(BaseModel):
    id: str
    entity_id: str
    user_id: Optional[str] = None
    status: str
    fields: dict[str, Any] = {}
    messages: list[dict[str, Any]] = []
    missing: list[str] = []
    ready: bool = False
    # An Architect turn is running, or one that lost its process is being run
    # again; its reply and readiness land when it ends. False with a user
    # message last: that turn ended without a reply (it was lost too long ago
    # or kept failing), and the user's next message continues the conversation.
    working: bool = False
    suggested_blueprint: Optional[BlueprintSuggestionResponse] = None
    applied_blueprint_id: Optional[str] = None
    finalized_workspace_id: Optional[str] = None
    finalized_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Each part of a team build (agents, knowledge, workflows, automations,
    # app): pending, candidate, accepted, skipped or lead. Empty without one.
    build_progress: dict[str, str] = {}


class DraftTurnResponse(BaseModel):
    reply: str
    draft: DraftResponse


_ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"


def _validate_ulid_list(value: Optional[list[str]]) -> Optional[list[str]]:
    if value is None:
        return None
    import re

    cleaned = [str(item).strip() for item in value if str(item or "").strip()]
    bad = [item for item in cleaned if not re.match(_ULID_PATTERN, item)]
    if bad:
        raise ValueError(f"document_ids must be ULIDs: {bad}")
    return cleaned


class StartDraftRequest(BaseModel):
    initial_brief: Optional[str] = None
    draft_id: Optional[str] = Field(
        default=None,
        pattern=_ULID_PATTERN,
    )
    document_ids: Optional[list[str]] = Field(default=None, max_length=20)

    @field_validator("document_ids")
    @classmethod
    def _validate_document_ids(cls, value):
        return _validate_ulid_list(value)


class DraftMessageRequest(BaseModel):
    message: str
    document_ids: Optional[list[str]] = Field(default=None, max_length=20)

    @field_validator("document_ids")
    @classmethod
    def _validate_document_ids(cls, value):
        return _validate_ulid_list(value)


class ApplyBlueprintRequest(BaseModel):
    blueprint_id: str


class FinalizeDraftResponse(BaseModel):
    workspace_id: str
    draft: DraftResponse


# ── Helpers ─────────────────────────────────────────────────────────────────

def _draft_busy(exc: Exception) -> HTTPException:
    retry = draft_service.DRAFT_BUSY_RETRY_SECONDS
    return HTTPException(
        409,
        detail={"code": "draft_busy", "message": str(exc), "retry_after_seconds": retry},
        headers={"Retry-After": str(retry)},
    )


def _is_mcp_client(request: Request) -> bool:
    """The caller is an agent on Manor's MCP tools (its token's ``act`` claim)."""
    claims = getattr(request.state, "auth_claims", None) or {}
    return str((claims.get("act") or {}).get("sub") or "").startswith("mcp:")


async def _hydrate_response(
    db: AsyncSession, draft: WorkspaceDraft,
    *, blueprints: dict[str, WorkspaceBlueprint] | None = None,
) -> DraftResponse:
    draft_service.reconcile_draft_fields(draft)

    suggestion: Optional[BlueprintSuggestionResponse] = None
    if draft.suggested_blueprint_id and not draft.applied_blueprint_id:
        bp = (
            blueprints.get(draft.suggested_blueprint_id)
            if blueprints is not None
            else await db.get(WorkspaceBlueprint, draft.suggested_blueprint_id)
        )
        if bp is not None and bp.status == BlueprintStatus.PUBLISHED:
            suggestion = BlueprintSuggestionResponse(
                id=bp.id,
                title=bp.title,
                summary=bp.summary,
                tags=list(bp.tags or []),
                install_count=int(bp.install_count or 0),
            )

    # Hide the hidden <workspace_setup_context> / <workspace_setup>
    # status blocks the LLM emits -- those are control plumbing, not
    # part of the user-visible transcript. The DB keeps them so the
    # next turn has full context. Also drop the synthesized opening
    # signal ("begin") and any user message that is empty after the
    # control blocks are stripped -- those are seed messages, not
    # things the user actually typed.
    visible_messages: list[dict[str, Any]] = []
    for m in draft.messages or []:
        role = m.get("role")
        content = m.get("content", "") or ""
        stripped = _strip_hidden_blocks(content)
        if role == "user":
            if not stripped or stripped.lower() == "begin":
                continue
            visible_messages.append({"role": "user", "content": stripped})
        elif role == "assistant":
            if not stripped:
                continue
            reply: dict[str, Any] = {"role": "assistant", "content": stripped}
            # The architect's question rides on its reply as answer buttons.
            if isinstance(m.get("question"), dict):
                reply["question"] = m["question"]
            visible_messages.append(reply)
        # silently skip any system/tool messages persisted for context

    return DraftResponse(
        id=draft.id,
        entity_id=draft.entity_id,
        user_id=draft.user_id,
        status=draft.status,
        fields=strip_source_text({
            key: value for key, value in (draft.fields or {}).items()
            if key not in {
                "_blueprint_source_payload", draft_service.TURN_CLAIM_FIELD,
                draft_service.TURN_REQUEST_FIELD, PENDING_QUESTION_FIELD, STAFF_REQUEST_TEXT_FIELD,
            }
        }),
        messages=visible_messages,
        missing=list(draft.missing or []),
        ready=bool(draft.ready),
        working=draft_service.turn_working(draft),
        suggested_blueprint=suggestion,
        applied_blueprint_id=draft.applied_blueprint_id,
        finalized_workspace_id=draft.finalized_workspace_id,
        finalized_at=draft.finalized_at,
        created_at=draft.created_at,
        updated_at=getattr(draft, "updated_at", None),
        build_progress=_build_progress(draft.fields or {}),
    )


def _build_progress(fields: dict[str, Any]) -> dict[str, str]:
    from packages.core.services.workspace_build_spec import build_spec

    spec = build_spec(fields)
    if spec is None or spec.get("mode") != "team":
        return {}
    return {str(part): str(state) for part, state in (spec.get("progress") or {}).items()}


def _strip_hidden_blocks(text: str) -> str:
    import re
    text = re.sub(
        r"<workspace_setup_context>.*?</workspace_setup_context>\s*",
        "", text, flags=re.DOTALL,
    )
    text = re.sub(
        r"<workspace_setup>.*?</workspace_setup>\s*",
        "", text, flags=re.DOTALL,
    )
    text = re.sub(
        r"<workspace_setup_note>.*?</workspace_setup_note>\s*",
        "", text, flags=re.DOTALL,
    )
    return text.strip()


# ── Endpoints ───────────────────────────────────────────────────────────────

@router.post("", response_model=DraftTurnResponse, status_code=201)
async def create_draft(
    req: StartDraftRequest,
    request: Request,
    _gate=Depends(require_plan("workspaces")),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a new workspace draft and return the assistant's opening turn.

    Plan gate fires here on purpose -- if the entity has hit its
    workspace cap, telling the user upfront is kinder than letting them
    chat for ten minutes only to be blocked at finalize. The frontend
    catches 402 and renders a plan-limit overlay with the exact
    current/limit numbers from the gate's structured detail payload.

    Like ``/stream``, the shell is committed first and the turn holds its
    claim, not a transaction, so other requests see the draft as busy (409)
    rather than waiting on its row lock. A caller-chosen ``draft_id`` makes
    the call retryable: a finished opening turn is returned, not re-run.
    """
    draft_id = await _persist_stream_draft_shell(
        entity_id=user.entity_id,
        user_id=user.id,
        initial_brief=req.initial_brief,
        draft_id=req.draft_id,
        document_ids=req.document_ids,
    )
    opening_message = (
        await _resume_stream_draft_shell(entity_id=user.entity_id, user_id=user.id, draft_id=draft_id)
        if req.draft_id else draft_service.opening_user_message(req.initial_brief)
    )
    try:
        reply, draft = await draft_service.process_draft_message(
            db,
            draft_id=draft_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_message=opening_message,
            dedupe_opening=True,
            claim_wait_seconds=0,
            mcp_client=_is_mcp_client(request),
        )
    except draft_service.WorkspaceDraftBusyError as exc:
        raise _draft_busy(exc) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    return DraftTurnResponse(reply=reply, draft=await _hydrate_response(db, draft))


# ── Streaming endpoints ─────────────────────────────────────────────────────
#
# Two SSE flavors:
#   * POST /stream            — start a NEW draft, stream the opening turn
#   * POST /{id}/messages/stream — send a message to an EXISTING draft, stream
#
# Event names emitted:
#   start    {draft_id, status}
#   token    {content}            -- one or more deltas of assistant text
#   keepalive {draft_id}           -- keeps quiet tool phases connected
#   tool_progress {step}          -- a long tool (a design wave) is still going
#   done     {reply, draft}       -- the full final reply + hydrated draft
#   continuing {working}          -- this stream ends, the turn does not: follow
#                                    the draft until ``working`` is false
#   error    {message}

async def _stream_turn(
    *,
    entity_id: str,
    user_id: str,
    draft_id: str,
    user_message: str,
    mode: Literal["create", "message"],
    document_ids: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """Run one turn against a durable draft and stream tokens as SSE.

    Uses its own short-lived DB session so the long-lived SSE generator
    does not pin the request-scoped connection -- same pattern
    chat_service.stream_chat_response uses.
    """
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    DONE: tuple[str, dict] = ("__done__", {})

    block_filter = _HiddenBlockTokenFilter()
    tool_step = {"i": 0}
    has_visible_text = {"value": False}
    reset_before_next_text = {"value": False}

    async def on_event(event_name: str, payload: dict) -> None:
        # The LLM client emits text_delta with {"content": "..."} and
        # occasionally text_reset. Forward only the deltas to the
        # frontend; the rest is internal plumbing. Run every delta
        # through the hidden-block filter so the user never sees the
        # <workspace_setup>...</workspace_setup> JSON status block.
        if event_name == "text_delta":
            content = payload.get("content", "")
            if content:
                visible = block_filter.feed(content)
                if visible:
                    if reset_before_next_text["value"]:
                        reset_before_next_text["value"] = False
                        if has_visible_text["value"]:
                            await queue.put(("text_reset", {}))
                    await queue.put(("token", {"content": visible}))
                    has_visible_text["value"] = True
        elif event_name == "text_reset":
            # The model occasionally retracts its in-progress text.
            # Reset both the filter and tell the frontend to drop what
            # it has so the new run starts on a clean bubble.
            block_filter.reset()
            reset_before_next_text["value"] = False
            await queue.put(("text_reset", {}))
        elif event_name == "turn_meta":
            # Tokens / duration / rounds for the just-completed turn.
            await queue.put(("turn_meta", payload))
        elif event_name == "tool_progress":
            await queue.put(("tool_progress", {"step": tool_step["i"]}))

    def on_tool_start(name: str, args: dict) -> None:
        tool_step["i"] += 1
        # Drop noisy fields from the args preview the user sees so
        # the construction log stays scannable.
        preview = {
            k: v for k, v in (args or {}).items()
            if k not in {"draft_id", "entity_id", "rationale"}
        }
        queue.put_nowait((
            "tool_start",
            {"step": tool_step["i"], "name": name, "args": preview},
        ))

    def on_tool_end(name: str, result: str) -> None:
        # Surface a tiny summary; the full result stays server-side.
        ok = True
        summary: dict[str, object] = {}
        try:
            parsed = json.loads(result)
            ok = bool(parsed.get("ok", True))
            for k in ("service_key", "goal_key", "rule_key", "automation_key", "agent_id", "agent_name", "role", "channel_type", "p0", "p1", "ready"):
                if k in parsed:
                    summary[k] = parsed[k]
            if not ok:
                summary["error"] = parsed.get("error", "tool error")
        except Exception:  # noqa: BLE001
            pass
        queue.put_nowait((
            "tool_end",
            {"step": tool_step["i"], "name": name, "ok": ok, "summary": summary},
        ))
        reset_before_next_text["value"] = True

    async def runner() -> None:
        try:
            async with async_session() as db:
                reply, draft = await draft_service.process_draft_message(
                    db,
                    draft_id=draft_id,
                    entity_id=entity_id,
                    user_id=user_id,
                    user_message=user_message,
                    stream_handler=on_event,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    dedupe_opening=mode == "create",
                    document_ids=document_ids,
                    claim_wait_seconds=min(
                        draft_service.TURN_CLAIM_WAIT_SECONDS,
                        max(0.0, _draft_turn_stall_seconds() - _DRAFT_TURN_STALL_CLAIM_MARGIN_SECONDS),
                    ),
                )
                await db.commit()
                hydrated = await _hydrate_response(db, draft)
                # Strip hidden blocks from the visible reply for the
                # final payload too (deltas are pre-strip but the user
                # only sees text after the model emits it; the status
                # block is at the very end so deltas stream cleanly).
                visible_reply = _strip_hidden_blocks(reply)
                await queue.put((
                    "done",
                    {"reply": visible_reply, "draft": hydrated.model_dump(mode="json")},
                ))
        except ValueError as exc:
            await queue.put(await turn_ended_event(str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("workspace draft stream failed")
            await queue.put(await turn_ended_event(f"Internal error: {exc}"))
        finally:
            await queue.put(DONE)

    async def turn_ended_event(message: str) -> tuple[str, dict]:
        """A failed turn whose saved message is run again is not over for the person."""
        try:
            async with async_session() as db:
                if await draft_service.turn_continues(db, draft_id=draft_id, user_message=user_message):
                    return ("continuing", {"working": True})
        except Exception:  # noqa: BLE001 - the turn's own failure is what to report
            logger.warning("Could not check draft %s for a continuing turn", draft_id, exc_info=True)
        return ("error", {"message": message})

    # The shell is already committed before this generator is created. Emit
    # its exact recovery ID before acquiring another database connection, so
    # pool exhaustion cannot hide the resumable draft from the client.
    yield _sse("start", {"draft_id": draft_id, "mode": mode})
    task = asyncio.create_task(runner())

    try:
        while True:
            try:
                event_name, payload = await asyncio.wait_for(
                    queue.get(),
                    timeout=_DRAFT_STREAM_KEEPALIVE_SECONDS,
                )
            except asyncio.TimeoutError:
                # Capability matching can spend several minutes inside a
                # single tool call without producing model deltas. Emit a
                # small frame so proxies and browsers do not mistake that
                # quiet period for an abandoned response and cancel runner
                # before it can enqueue the terminal ``done`` event.
                yield _sse("keepalive", {"draft_id": draft_id})
                continue
            if (event_name, payload) == DONE:
                break
            yield _sse(event_name, payload)
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def _workspace_draft_streaming_response(
    db: AsyncSession,
    lease,
    source: AsyncGenerator[str, None],
    *,
    status_code: int = 200,
    architect_turn: bool = False,
) -> StreamingResponse:
    """Release the request transaction before a long-lived SSE response.

    An Architect turn is stopped once it makes no progress for the stall
    window: closing its stream cancels the turn, the recovery sweep runs its
    saved request again, and a ``continuing`` frame sends the client to follow
    the draft.
    """
    try:
        await db.commit()
        await db.close()
        watchdog = {
            "stall_timeout_seconds": _draft_turn_stall_seconds(),
            "is_progress": _is_turn_progress,
            "stalled_chunk": _draft_turn_continuing_frame,
        } if architect_turn else {}
        return LeasedStreamingResponse(
            lease.wrap(source, **watchdog),
            status_code=status_code,
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    except BaseException:
        await lease.release()
        raise


async def _persist_stream_draft_shell(
    *,
    entity_id: str,
    user_id: str,
    initial_brief: str | None,
    draft_id: str | None = None,
    plan_user: User | None = None,
    document_ids: list[str] | None = None,
) -> str:
    """Commit or recover the exact shell before SSE can disconnect."""
    async with async_session() as db:
        draft = None
        if draft_id:
            draft = await draft_service.get_draft(
                db,
                draft_id,
                entity_id,
                user_id,
            )
            if draft is None:
                if plan_user is not None:
                    await _enforce_new_workspace_draft_plan(plan_user, db=db)
                try:
                    async with db.begin_nested():
                        draft = await draft_service.create_draft_shell(
                            db,
                            entity_id=entity_id,
                            user_id=user_id,
                            initial_brief=initial_brief,
                            draft_id=draft_id,
                            document_ids=document_ids,
                        )
                except IntegrityError:
                    draft = await draft_service.get_draft(
                        db,
                        draft_id,
                        entity_id,
                        user_id,
                    )
                    if draft is None:
                        raise HTTPException(
                            status_code=409,
                            detail="Draft id is already in use",
                        ) from None
        else:
            if plan_user is not None:
                await _enforce_new_workspace_draft_plan(plan_user, db=db)
            draft = await draft_service.create_draft_shell(
                db,
                entity_id=entity_id,
                user_id=user_id,
                initial_brief=initial_brief,
                document_ids=document_ids,
            )
        if draft.status not in {"active", "ready"}:
            raise HTTPException(
                status_code=409,
                detail=f"Draft is {draft.status} and cannot be changed",
            )
        await db.commit()
        return draft.id


async def _enforce_new_workspace_draft_plan(
    user: User,
    *,
    db: AsyncSession,
) -> None:
    """Gate creation of a shell without gating recovery of an existing one."""
    await enforce_plan_resource("workspaces", user=user, db=db)


async def _resume_stream_draft_shell(
    *,
    entity_id: str,
    user_id: str,
    draft_id: str,
) -> str:
    """Load the stored opening prompt for an owned, resumable shell."""
    async with async_session() as db:
        draft = await draft_service.get_draft(
            db,
            draft_id,
            entity_id,
            user_id,
        )
        if draft is None:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status not in {"active", "ready"}:
            raise HTTPException(
                status_code=409,
                detail=f"Draft is {draft.status} and cannot be changed",
            )
        initial_brief = (draft.fields or {}).get("initial_brief")
        return draft_service.opening_user_message(
            initial_brief if isinstance(initial_brief, str) else None,
        )


@router.post("/stream", status_code=201)
async def create_draft_stream(
    req: StartDraftRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start a new draft and stream the opening assistant turn as SSE.

    New shells use the same structured plan gate as ``create_draft``.
    Recovery of an already-persisted shell skips that creation gate so a
    refresh remains resumable even when capacity was filled meanwhile.
    """
    lease = await acquire_chat_stream_lease(scope="workspace-draft")
    try:
        draft_id = await _persist_stream_draft_shell(
            entity_id=user.entity_id,
            user_id=user.id,
            initial_brief=req.initial_brief,
            draft_id=req.draft_id,
            plan_user=user,
            document_ids=req.document_ids,
        )
        if req.draft_id:
            opening_message = await _resume_stream_draft_shell(
                entity_id=user.entity_id,
                user_id=user.id,
                draft_id=draft_id,
            )
        else:
            opening_message = draft_service.opening_user_message(req.initial_brief)
    except BaseException:
        await lease.release()
        raise
    stream = _stream_turn(
        entity_id=user.entity_id,
        user_id=user.id,
        draft_id=draft_id,
        user_message=opening_message,
        mode="create",
    )
    return await _workspace_draft_streaming_response(
        db,
        lease,
        stream,
        status_code=201,
        architect_turn=True,
    )


@router.get("/recommendations")
async def workspace_recommendations(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Workspaces worth creating for this person; a stale result refreshes in the background."""
    from packages.core.services.workspace_recommendations import read_workspace_recommendations

    return await read_workspace_recommendations(db, user)


@router.get("", response_model=list[DraftResponse])
async def list_my_drafts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
):
    """List the caller's drafts. Filter by status (active/ready/finalized/abandoned)."""
    stmt = select(WorkspaceDraft).where(
        WorkspaceDraft.entity_id == user.entity_id,
        WorkspaceDraft.user_id == user.id,
    )
    if status:
        stmt = stmt.where(WorkspaceDraft.status == status)
    stmt = stmt.order_by(WorkspaceDraft.created_at.desc()).limit(100)
    result = await db.execute(stmt)
    drafts = result.scalars().all()
    suggestion_ids = {
        draft.suggested_blueprint_id for draft in drafts
        if draft.suggested_blueprint_id and not draft.applied_blueprint_id
    }
    blueprints = {}
    if suggestion_ids:
        # Prefetch before hydration can dirty normalized draft fields. Fetch
        # display columns only; Blueprint payloads can contain large bundles.
        result = await db.execute(
            select(WorkspaceBlueprint)
            .where(
                WorkspaceBlueprint.id.in_(suggestion_ids),
                WorkspaceBlueprint.status == BlueprintStatus.PUBLISHED,
            )
            .options(load_only(
                WorkspaceBlueprint.id, WorkspaceBlueprint.status,
                WorkspaceBlueprint.title, WorkspaceBlueprint.summary,
                WorkspaceBlueprint.tags, WorkspaceBlueprint.install_count,
                raiseload=True,
            ))
        )
        blueprints = {bp.id: bp for bp in result.scalars()}
    return [await _hydrate_response(db, d, blueprints=blueprints) for d in drafts]


@router.get("/{draft_id}", response_model=DraftResponse)
async def get_one_draft(
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    draft = await draft_service.get_draft(
        db, draft_id, user.entity_id, user.id,
    )
    if draft is None:
        raise HTTPException(404, "Draft not found")
    return await _hydrate_response(db, draft)


@router.post("/{draft_id}/messages", response_model=DraftTurnResponse)
async def post_draft_message(
    draft_id: str,
    req: DraftMessageRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send the user's next turn and get the assistant's reply."""
    if not req.message.strip():
        raise HTTPException(400, "Message cannot be empty")
    try:
        reply, draft = await draft_service.process_draft_message(
            db,
            draft_id=draft_id,
            entity_id=user.entity_id,
            user_id=user.id,
            user_message=req.message,
            document_ids=req.document_ids,
            # A synchronous caller is told the draft is busy instead of
            # waiting out another turn before running its own.
            claim_wait_seconds=0,
            mcp_client=_is_mcp_client(request),
        )
    except draft_service.WorkspaceDraftBusyError as exc:
        raise _draft_busy(exc) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await db.commit()
    return DraftTurnResponse(reply=reply, draft=await _hydrate_response(db, draft))


@router.post("/{draft_id}/messages/stream")
async def post_draft_message_stream(
    draft_id: str,
    req: DraftMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message and stream the assistant's reply token-by-token (SSE)."""
    if not req.message.strip():
        raise HTTPException(400, "Message cannot be empty")
    lease = await acquire_chat_stream_lease(scope="workspace-draft")
    return await _workspace_draft_streaming_response(
        db,
        lease,
        _stream_turn(
            entity_id=user.entity_id,
            user_id=user.id,
            draft_id=draft_id,
            user_message=req.message,
            mode="message",
            document_ids=req.document_ids,
        ),
        architect_turn=True,
    )


@router.post("/{draft_id}/apply-blueprint", response_model=DraftResponse)
async def apply_blueprint_to_draft(
    draft_id: str,
    req: ApplyBlueprintRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pre-fill the draft from a marketplace blueprint."""
    try:
        draft = await draft_service.apply_blueprint(
            db,
            draft_id=draft_id,
            entity_id=user.entity_id,
            user_id=user.id,
            blueprint_id=req.blueprint_id,
        )
    except PermissionError as exc:
        # Paid blueprint without a completed purchase.
        raise HTTPException(402, str(exc))
    except draft_service.WorkspaceDraftBusyError as exc:
        raise _draft_busy(exc) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    await db.commit()
    return await _hydrate_response(db, draft)


@router.patch("/{draft_id}/fields")
async def update_draft_fields(
    draft_id: str,
    req: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Patch specific draft fields (e.g. toggle knowledge approved flags).

    Body: {"knowledge_attachments": [...], "channel_config": {...}, ...}
    Only provided keys are merged; others left unchanged.
    """
    internal_fields = sorted(key for key in req if key.startswith("_"))
    if internal_fields:
        raise HTTPException(
            400,
            "Internal draft fields cannot be updated: "
            + ", ".join(internal_fields),
        )
    if "heartbeat_enabled" in req and not isinstance(req["heartbeat_enabled"], bool):
        raise HTTPException(400, "heartbeat_enabled must be a boolean")
    if "blueprint_channel_config_ids" in req:
        selections = req["blueprint_channel_config_ids"]
        if not isinstance(selections, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in selections.items()
        ):
            raise HTTPException(400, "Blueprint channel selections must map keys to account IDs")
    try:
        draft = await draft_service.get_draft(
            db, draft_id, user.entity_id, user.id, for_update=True,
        )
    except draft_service.WorkspaceDraftBusyError as exc:
        raise _draft_busy(exc) from exc
    if not draft:
        raise HTTPException(404, "Draft not found")
    if draft.status not in {"active", "ready"}:
        raise HTTPException(409, f"Draft is {draft.status} and cannot be changed")
    draft_service.apply_public_field_updates(draft, req)
    await db.flush()
    await draft_service._refresh_missing_from_lint(db, draft)
    await db.flush()
    await db.refresh(draft)
    response = await _hydrate_response(db, draft)
    await db.commit()
    return response


@router.post("/{draft_id}/finalize", response_model=FinalizeDraftResponse)
async def finalize(
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Materialize the draft into a real Workspace (synchronous JSON variant)."""
    try:
        workspace_id, draft = await draft_service.finalize_draft(
            db,
            draft_id=draft_id,
            entity_id=user.entity_id,
            user_id=user.id,
        )
    except WorkspacePlanLimitError as exc:
        raise HTTPException(402, detail=exc.detail)
    except MarketplaceIdentityConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except draft_service.WorkspaceDraftMaterializationError as exc:
        # The Workspace shell and resource rows share this transaction.  A
        # resource failure must not be committed as a stale-ready correction.
        await db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except draft_service.WorkspaceDraftNotReadyError as exc:
        # Finalize has not started materialization. Persist only the verified
        # lint-derived readiness correction before the HTTP error rolls back.
        await db.commit()
        raise HTTPException(400, str(exc)) from exc
    except draft_service.WorkspaceDraftBusyError as exc:
        raise _draft_busy(exc) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await db.commit()
    from packages.core.services.workspace_setup_service import (
        dispatch_workspace_post_commit,
        record_workspace_post_commit_dispatch_failure,
    )

    try:
        await dispatch_workspace_post_commit(
            db,
            workspace_id=workspace_id,
            entity_id=user.entity_id,
        )
        await db.commit()
    except Exception as dispatch_exc:  # noqa: BLE001
        await db.rollback()
        logger.exception(
            "workspace %s was created but startup dispatch failed",
            workspace_id,
        )
        try:
            await record_workspace_post_commit_dispatch_failure(
                db,
                workspace_id=workspace_id,
                entity_id=user.entity_id,
                error=dispatch_exc,
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("could not record startup dispatch failure for %s", workspace_id)
    return FinalizeDraftResponse(
        workspace_id=workspace_id,
        draft=await _hydrate_response(db, draft),
    )


@router.post("/{draft_id}/finalize/stream")
async def finalize_stream(
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Materialize the draft and stream finalize progress as SSE events.

    Events emitted (in order):
      - ``agent_bindings_repair_started`` / ``agent_bindings_repair_done``
                                      optional pre-creation binding repair.
      - ``workspace_created``        DB row done.
      - ``provisioning_agents_started``  total / custom counts.
      - ``agent_provisioned``        per-agent (index/total + name).
      - ``agents_done``              all subscriptions in.
      - ``provisioning_team_and_knowledge``
      - ``team_and_knowledge_done``  staff/knowledge/channel counts.
      - ``default_skills_seeded``
      - ``memory_seeded``
      - ``runtime_scheduled``        heartbeat registered when autonomous mode is enabled.
      - ``post_commit_dispatch_pending`` durable materialization is ready to commit.
      - ``strategist_dispatched``    emitted after commit only for autonomous mode.
      - ``complete``                 final committed workspace result.
      - ``error``                    if anything blew up.
    """
    queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
    DONE: tuple[str, dict] = ("__done__", {})

    def progress(step: str, payload: dict) -> None:
        try:
            queue.put_nowait((step, payload))
        except Exception:
            pass

    async def runner() -> None:
        try:
            async with async_session() as db:
                try:
                    workspace_id, draft = await draft_service.finalize_draft(
                        db,
                        draft_id=draft_id,
                        entity_id=user.entity_id,
                        user_id=user.id,
                        progress=progress,
                    )
                except draft_service.WorkspaceDraftMaterializationError as exc:
                    await db.rollback()
                    await queue.put(("error", {"message": str(exc)}))
                    return
                except draft_service.WorkspaceDraftNotReadyError as exc:
                    # The stream owns this session, so commit the same
                    # readiness correction before returning its error event.
                    await db.commit()
                    await queue.put(("error", {"message": str(exc)}))
                    return
                await db.commit()
                from packages.core.services.workspace_setup_service import (
                    dispatch_workspace_post_commit,
                    record_workspace_post_commit_dispatch_failure,
                )

                dispatch_warning: str | None = None
                try:
                    dispatch = await dispatch_workspace_post_commit(
                        db,
                        workspace_id=workspace_id,
                        entity_id=user.entity_id,
                        progress=progress,
                    )
                    await db.commit()
                except Exception as dispatch_exc:  # noqa: BLE001
                    dispatch_warning = "workspace_startup_dispatch_failed"
                    await db.rollback()
                    logger.exception(
                        "workspace %s was created but startup dispatch failed",
                        workspace_id,
                    )
                    dispatch = {"strategist_eta_seconds": None}
                    try:
                        await record_workspace_post_commit_dispatch_failure(
                            db,
                            workspace_id=workspace_id,
                            entity_id=user.entity_id,
                            error=dispatch_exc,
                        )
                        await db.commit()
                    except Exception:  # noqa: BLE001
                        await db.rollback()
                        logger.exception(
                            "could not record startup dispatch failure for %s",
                            workspace_id,
                        )
                    progress(
                        "dispatch_warning",
                        {"workspace_id": workspace_id, "message": dispatch_warning},
                    )
                progress(
                    "complete",
                    {
                        "workspace_id": workspace_id,
                        "strategist_eta_seconds": dispatch.get("strategist_eta_seconds"),
                    },
                )
                hydrated = await _hydrate_response(db, draft)
                await queue.put((
                    "done",
                    {
                        "workspace_id": workspace_id,
                        "draft": hydrated.model_dump(mode="json"),
                        "strategist_eta_seconds": dispatch.get("strategist_eta_seconds"),
                        "dispatch_warning": dispatch_warning,
                    },
                ))
        except WorkspacePlanLimitError as exc:
            await queue.put(("error", exc.detail))
        except MarketplaceIdentityConflictError as exc:
            await queue.put(("error", {
                "code": "marketplace_identity_conflict",
                "message": str(exc),
            }))
        except ValueError as exc:
            await queue.put(("error", {"message": str(exc)}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("workspace finalize stream failed")
            await queue.put(("error", {"message": f"Internal error: {exc}"}))
        finally:
            await queue.put(DONE)

    lease = await acquire_chat_stream_lease(scope="workspace-draft-finalize")

    async def gen() -> AsyncGenerator[str, None]:
        task = asyncio.create_task(runner())
        try:
            yield _sse("start", {"draft_id": draft_id})
            while True:
                try:
                    event_name, payload = await asyncio.wait_for(
                        queue.get(), timeout=_DRAFT_STREAM_KEEPALIVE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    # Agent repair can wait on capability matching between
                    # progress events, just like an ordinary Architect turn.
                    yield _sse("keepalive", {"draft_id": draft_id})
                    continue
                if (event_name, payload) == DONE:
                    break
                yield _sse(event_name, payload)
        finally:
            # A dropped SSE connection must not roll back a workspace whose
            # provisioning is already underway. The runner owns its DB session
            # and continues to a commit even when the client stops listening.
            if task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            else:
                def _consume_runner_result(done: asyncio.Task) -> None:
                    try:
                        done.result()
                    except (asyncio.CancelledError, Exception):
                        pass

                task.add_done_callback(_consume_runner_result)

    return await _workspace_draft_streaming_response(
        db,
        lease,
        gen(),
    )


@router.delete("/{draft_id}", status_code=204)
async def delete_draft(
    draft_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a draft abandoned. Finalized drafts cannot be abandoned."""
    try:
        ok = await draft_service.abandon_draft(
            db,
            draft_id=draft_id,
            entity_id=user.entity_id,
            user_id=user.id,
        )
    except draft_service.WorkspaceDraftBusyError as exc:
        raise _draft_busy(exc) from exc
    if not ok:
        raise HTTPException(404, "Draft not found or already finalized")
    await db.commit()
