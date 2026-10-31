"""Company client contacts and private Workspace invitations."""

from fastapi import APIRouter, Depends, HTTPException
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from apps.api.deps import get_current_user
from packages.core.blueprints.exporter import ExportError
from packages.core.blueprints.installer import InstallError
from packages.core.blueprints.payload import RELEASE_NOTES_MAX_CHARS
from packages.core.constants.blueprints import BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS
from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services import client_delivery as service
from packages.core.services.client_delivery import ContactRequest, receipt, save_contact

router = APIRouter(prefix="/api/v1", tags=["client-delivery"])


def _document_ids(values: list[str]) -> list[str]:
    normalized = [value.strip() for value in values]
    if any(not value for value in normalized):
        raise ValueError("knowledge_document_ids cannot contain empty values")
    return list(dict.fromkeys(normalized))


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str
    workspace_id: str
    phase: Literal["trial", "delivered"] = "trial"
    knowledge_document_ids: list[str] = Field(default_factory=list, max_length=BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS)

    @field_validator("knowledge_document_ids")
    @classmethod
    def normalize_knowledge_document_ids(cls, values: list[str]) -> list[str]:
        return _document_ids(values)


class AcceptRequest(BaseModel):
    variable_values: dict = Field(default_factory=dict)
    channel_config_ids: dict[str, str] = Field(default_factory=dict)


@router.get("/clients/{client_id}/contacts")
async def contacts(client_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.contacts(client_id=client_id, user=user, db=db)


@router.post("/clients/{client_id}/contacts", status_code=201)
async def add_contact(
    client_id: str, req: ContactRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await save_contact(db, user, client_id, req)


@router.put("/clients/{client_id}/contacts/{contact_id}")
async def edit_contact(
    client_id: str,
    contact_id: str,
    req: ContactRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await save_contact(db, user, client_id, req, contact_id)


@router.delete("/clients/{client_id}/contacts/{contact_id}", status_code=204)
async def remove_contact(
    client_id: str, contact_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return await service.remove_contact(client_id=client_id, contact_id=contact_id, user=user, db=db)


@router.get("/clients/{client_id}/deliveries")
async def deliveries(client_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await service.deliveries(client_id=client_id, user=user, db=db)


@router.post("/clients/{client_id}/deliveries", status_code=201)
async def prepare(
    client_id: str, req: DeliveryRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    try:
        return await service.prepare_delivery(db, user=user, client_id=client_id, **req.model_dump())
    except (ExportError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


class PublishUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Omitted: the Knowledge documents the delivery already carries.
    knowledge_document_ids: list[str] | None = Field(default=None, max_length=BLUEPRINT_KNOWLEDGE_MAX_DOCUMENTS)
    release_notes: str | None = Field(default=None, max_length=RELEASE_NOTES_MAX_CHARS)

    @field_validator("knowledge_document_ids")
    @classmethod
    def normalize_knowledge_document_ids(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _document_ids(values)


# Declared before the invitation action route, which would otherwise match it.
@router.post("/clients/{client_id}/deliveries/{delivery_id}/publish-update")
async def publish_update(
    client_id: str,
    delivery_id: str,
    req: PublishUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send the source Workspace's current configuration as the delivery's next release."""
    try:
        return await service.publish_update(
            db, user=user, client_id=client_id, delivery_id=delivery_id, **req.model_dump(),
        )
    except (ExportError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/clients/{client_id}/deliveries/{delivery_id}/{action}")
async def change_invite(
    client_id: str,
    delivery_id: str,
    action: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await service.change_invite(client_id=client_id, delivery_id=delivery_id, action=action, user=user, db=db)


@router.get("/client-deliveries/invites/{token}")
async def invitation(token: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    row = await service.recipient_delivery(db, token, user)
    return receipt(row)


@router.post("/client-deliveries/invites/{token}/accept")
async def accept(
    token: str, req: AcceptRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    try:
        row = await service.accept_delivery(db, token=token, user=user, **req.model_dump())
    except (InstallError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return receipt(row)
