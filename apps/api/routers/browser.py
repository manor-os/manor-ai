"""Browser automation endpoints — session management and page interaction."""
from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import browser_service
from apps.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/browser", tags=["browser"])


# ── Pydantic models ──

class CreateSessionRequest(BaseModel):
    headless: bool = True


class SessionResponse(BaseModel):
    session_id: str
    entity_id: str
    status: str
    current_url: str | None = None
    created_at: str


class NavigateRequest(BaseModel):
    url: str


class NavigateResponse(BaseModel):
    url: str
    title: str | None = None
    status: int | None = None


class ScreenshotRequest(BaseModel):
    full_page: bool = False


class ActionRequest(BaseModel):
    action: str  # click | fill | evaluate | get_content
    selector: str | None = None
    value: str | None = None
    script: str | None = None


class ActionResponse(BaseModel):
    action: str
    result: str | None = None


# ── Helpers ──

async def _get_session_or_404(session_id: str, entity_id: str):
    session = await browser_service.get_session(session_id)
    if not session or session.entity_id != entity_id:
        raise HTTPException(404, "Browser session not found")
    return session


# ── Endpoints ──

@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    req: CreateSessionRequest,
    user: User = Depends(get_current_user),
):
    """Create and start a new browser automation session."""
    try:
        session = await browser_service.create_session(
            entity_id=user.entity_id,
            headless=req.headless,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    return SessionResponse(**session.to_dict())


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    user: User = Depends(get_current_user),
):
    """List active browser sessions for current entity."""
    sessions = await browser_service.list_sessions(user.entity_id)
    return [SessionResponse(**s) for s in sessions]


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str,
    user: User = Depends(get_current_user),
):
    """Get browser session info."""
    session = await _get_session_or_404(session_id, user.entity_id)
    return SessionResponse(**session.to_dict())


@router.post("/sessions/{session_id}/navigate", response_model=NavigateResponse)
async def navigate(
    session_id: str,
    req: NavigateRequest,
    user: User = Depends(get_current_user),
):
    """Navigate the browser to a URL."""
    session = await _get_session_or_404(session_id, user.entity_id)
    try:
        result = await session.navigate(req.url)
    except Exception as e:
        raise HTTPException(400, f"Navigation failed: {e}")
    return NavigateResponse(**result)


@router.post("/sessions/{session_id}/screenshot")
async def screenshot(
    session_id: str,
    req: ScreenshotRequest | None = None,
    user: User = Depends(get_current_user),
):
    """Take a screenshot of the current page. Returns PNG image."""
    session = await _get_session_or_404(session_id, user.entity_id)
    full_page = req.full_page if req else False
    try:
        png_bytes = await session.screenshot(full_page=full_page)
    except Exception as e:
        raise HTTPException(400, f"Screenshot failed: {e}")
    return Response(content=png_bytes, media_type="image/png")


@router.post("/sessions/{session_id}/action", response_model=ActionResponse)
async def perform_action(
    session_id: str,
    req: ActionRequest,
    user: User = Depends(get_current_user),
):
    """Perform a browser action: click, fill, evaluate, or get_content."""
    session = await _get_session_or_404(session_id, user.entity_id)

    try:
        if req.action == "click":
            if not req.selector:
                raise HTTPException(400, "selector is required for click action")
            await session.click(req.selector)
            return ActionResponse(action="click", result="ok")

        elif req.action == "fill":
            if not req.selector or req.value is None:
                raise HTTPException(400, "selector and value are required for fill action")
            await session.fill(req.selector, req.value)
            return ActionResponse(action="fill", result="ok")

        elif req.action == "evaluate":
            if not req.script:
                raise HTTPException(400, "script is required for evaluate action")
            result = await session.evaluate(req.script)
            return ActionResponse(action="evaluate", result=str(result) if result is not None else None)

        elif req.action == "get_content":
            content = await session.get_content()
            # Truncate to avoid massive responses
            if len(content) > 50_000:
                content = content[:50_000] + "\n... [truncated]"
            return ActionResponse(action="get_content", result=content)

        elif req.action == "pdf":
            pdf_bytes = await session.pdf()
            return Response(content=pdf_bytes, media_type="application/pdf")

        else:
            raise HTTPException(400, f"Unknown action: {req.action}. Supported: click, fill, evaluate, get_content, pdf")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Action '{req.action}' failed: {e}")


@router.delete("/sessions/{session_id}", status_code=204)
async def close_session(
    session_id: str,
    user: User = Depends(get_current_user),
):
    """Close and cleanup a browser session."""
    # Verify ownership first
    session = await browser_service.get_session(session_id)
    if not session or session.entity_id != user.entity_id:
        raise HTTPException(404, "Browser session not found")

    await browser_service.close_session(session_id)
