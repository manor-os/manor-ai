"""Document generation endpoints — create Word, PDF, and PowerPoint files."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from apps.api.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/docgen", tags=["docgen"])


# ── Request / response models ──

class GenerateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    format: str = Field(..., pattern="^(docx|pdf|pptx)$")
    template: str | None = None
    options: dict | None = None


class AIGenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=10_000)
    format: str = Field(..., pattern="^(docx|pdf|pptx)$")
    options: dict | None = None


class PreviewRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)


class FormatInfo(BaseModel):
    format: str
    name: str
    mime_type: str
    extension: str
    available: bool
    install: str | None = None


class PreviewResponse(BaseModel):
    html: str


# ── Endpoints ──

@router.post("/generate")
async def generate_document(
    req: GenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a document from provided content and return as a file download."""
    from packages.core.services.docgen_service import generate_document as gen_doc

    try:
        file_bytes, filename, mime_type = await gen_doc(
            entity_id=user.entity_id,
            user_id=user.id,
            title=req.title,
            content=req.content,
            format=req.format,
            template=req.template,
            options=req.options,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(422, str(e))

    import io
    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/ai-generate")
async def ai_generate_document(
    req: AIGenerateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Use AI to generate content, then render to the requested document format."""
    from packages.core.services.docgen_service import ai_generate_document as ai_gen

    try:
        file_bytes, filename, mime_type = await ai_gen(
            entity_id=user.entity_id,
            user_id=user.id,
            prompt=req.prompt,
            format=req.format,
            options=req.options,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(422, str(e))

    import io
    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type=mime_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/formats", response_model=list[FormatInfo])
async def list_formats(user: User = Depends(get_current_user)):
    """List supported document formats and their availability."""
    from packages.core.services.docgen_service import get_available_formats
    return get_available_formats()


@router.post("/preview", response_model=PreviewResponse)
async def preview_document(
    req: PreviewRequest,
    user: User = Depends(get_current_user),
):
    """Generate an HTML preview of the document content."""
    from packages.core.services.docgen_service import content_to_html
    html = content_to_html(req.title, req.content)
    return PreviewResponse(html=html)
