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
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.constants.blueprints import BlueprintStatus
from packages.core.database import async_session, get_db
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.user import User
from packages.core.models.workspace_draft import WorkspaceDraft
from packages.core.services import workspace_draft_service as draft_service
from packages.core.services.marketplace_resource_links import (
    MarketplaceIdentityConflictError,
)
from packages.core.services.marketplace_billing import MarketplacePaidPlanRequiredError
from packages.core.services.plan_gate import WorkspacePlanLimitError
from packages.core.services.sse_events import format_sse
from apps.api.deps import enforce_plan_resource, get_current_user, require_plan
from apps.api.streaming_concurrency import acquire_chat_stream_lease

logger = logging.getLogger(__name__)


_sse = format_sse


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
    suggested_blueprint: Optional[BlueprintSuggestionResponse] = None
    applied_blueprint_id: Optional[str] = None
    finalized_workspace_id: Optional[str] = None
    finalized_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DraftTurnResponse(BaseModel):
    reply: str
    draft: DraftResponse


class StartDraftRequest(BaseModel):
    initial_brief: Optional[str] = None
    draft_id: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$",
    )


class DraftMessageRequest(BaseModel):
    message: str


class ApplyBlueprintRequest(BaseModel):
    blueprint_id: str


class FinalizeDraftResponse(BaseModel):
    workspace_id: str
    draft: DraftResponse


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _hydrate_response(
    db: AsyncSession, draft: WorkspaceDraft,
) -> DraftResponse:
    draft_service.reconcile_draft_fields(draft)

    suggestion: Optional[BlueprintSuggestionResponse] = None
    if draft.suggested_blueprint_id and not draft.applied_blueprint_id:
        bp = await db.get(WorkspaceBlueprint, draft.suggested_blueprint_id)
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
            visible_messages.append({"role": "assistant", "content": stripped})
        # silently skip any system/tool messages persisted for context

    return DraftResponse(
        id=draft.id,
        entity_id=draft.entity_id,
        user_id=draft.user_id,
        status=draft.status,
        fields={
            key: value for key, value in (draft.fields or {}).items()
            if key != "_blueprint_source_payload"
        },
        messages=visible_messages,
        missing=list(draft.missing or []),
        ready=bool(draft.ready),
        suggested_blueprint=suggestion,
        applied_blueprint_id=draft.applied_blueprint_id,
        finalized_workspace_id=draft.finalized_workspace_id,
        finalized_at=draft.finalized_at,
        created_at=draft.created_at,
        updated_at=getattr(draft, "updated_at", None),
    )


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
    """
    reply, draft = await draft_service.start_draft(
        db,
        entity_id=user.entity_id,
        user_id=user.id,
        initial_brief=req.initial_brief,
    )
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
#   done     {reply, draft}       -- the full final reply + hydrated draft
#   error    {message}

async def _stream_turn(
    *,
    entity_id: str,
    user_id: str,
    draft_id: str,
    user_message: str,
    mode: Literal["create", "message"],
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
            await queue.put(("error", {"message": str(exc)}))
        except Exception as exc:  # noqa: BLE001
            logger.exception("workspace draft stream failed")
            await queue.put(("error", {"message": f"Internal error: {exc}"}))
        finally:
            await queue.put(DONE)

    # The shell is already committed before this generator is created. Emit
    # its exact recovery ID before acquiring another database connection, so
    # pool exhaustion cannot hide the resumable draft from the client.
    yield _sse("start", {"draft_id": draft_id, "mode": mode})
    task = asyncio.create_task(runner())

    try:
        while True:
            event_name, payload = await queue.get()
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
) -> StreamingResponse:
    """Release the request transaction before a long-lived SSE response."""
    try:
        await db.commit()
        await db.close()
        return StreamingResponse(
            lease.wrap(source),
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
    )


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
    return [await _hydrate_response(db, d) for d in drafts]


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
        )
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
        ),
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
    except MarketplacePaidPlanRequiredError as exc:
        raise HTTPException(402, detail=exc.detail) from exc
    except PermissionError as exc:
        # Paid blueprint without a completed purchase.
        raise HTTPException(402, str(exc))
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
    if "blueprint_channel_config_ids" in req:
        selections = req["blueprint_channel_config_ids"]
        if not isinstance(selections, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in selections.items()
        ):
            raise HTTPException(400, "Blueprint channel selections must map keys to account IDs")
    draft = await draft_service.get_draft(
        db, draft_id, user.entity_id, user.id, for_update=True,
    )
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
    except draft_service.WorkspaceDraftNotReadyError as exc:
        # Finalize has not started materialization. Persist only the verified
        # lint-derived readiness correction before the HTTP error rolls back.
        await db.commit()
        raise HTTPException(400, str(exc)) from exc
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
                event_name, payload = await queue.get()
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
    ok = await draft_service.abandon_draft(
        db,
        draft_id=draft_id,
        entity_id=user.entity_id,
        user_id=user.id,
    )
    if not ok:
        raise HTTPException(404, "Draft not found or already finalized")
    await db.commit()
