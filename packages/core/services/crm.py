"""Validated company CRM operations shared by HTTP and Agent tools."""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from packages.core.models.crm import ClientActivity, ClientFollowup, ClientOpportunity
from packages.core.models.people import Client
from packages.core.models.user import User, UserMembership
from packages.core.services.people_service import create_client, get_client, update_client

Stage = Literal["qualified", "discovery", "proposal", "negotiation", "won", "lost"]
Lifecycle = Literal["lead", "prospect", "customer", "partner"]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Profile(Request):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(default="", max_length=255, pattern="^$|^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")
    phone: str = Field(default="", max_length=50)
    address: str = Field(default="", max_length=2000)
    industry: str = Field(default="", max_length=255)
    website: str = Field(default="", max_length=1000)
    source: str = Field(default="", max_length=255)
    company_size: str = Field(default="", max_length=100)
    lifecycle: Lifecycle = "lead"
    owner_id: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=20000)
    status: Literal["active", "inactive"] = "active"

    @field_validator("tags")
    @classmethod
    def tags_valid(cls, value):
        if any((not x.strip() or len(x.strip()) > 60 for x in value)):
            raise ValueError("Tags must contain 1–60 characters")
        return list(dict.fromkeys((x.strip() for x in value)))


class Activity(Request):
    kind: Literal["note", "call", "email", "meeting"] = "note"
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(default="", max_length=20000)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("A timezone is required")
        return value


class Opportunity(Request):
    title: str = Field(min_length=1, max_length=255)
    stage: Stage = "qualified"
    amount: Decimal = Field(default=Decimal(0), ge=0, max_digits=16, decimal_places=2)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    probability: int = Field(default=25, ge=0, le=100)
    expected_close: date | None = None
    owner_id: str | None = None
    description: str = Field(default="", max_length=20000)
    loss_reason: str = Field(default="", max_length=1000)


class Followup(Request):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(default="", max_length=20000)
    due_at: datetime
    owner_id: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    status: Literal["open", "done"] = "open"

    @field_validator("due_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("A timezone is required")
        return value


async def customer(db, user, client_id, *, write=False):
    row = (
        await db.scalar(
            select(Client)
            .where(Client.id == client_id, Client.entity_id == user.entity_id, Client.deleted_at.is_(None))
            .with_for_update()
        )
        if write
        else await get_client(db, client_id, user.entity_id)
    )
    if row is None:
        raise HTTPException(404, "Client not found")
    if write and row.status != "active":
        raise HTTPException(409, "Restore the client before editing CRM records")
    return row


async def owner(db, user, owner_id):
    if owner_id is None:
        return
    found = await db.scalar(
        select(User.id)
        .join(UserMembership, UserMembership.user_id == User.id)
        .where(
            User.id == owner_id,
            User.status == "active",
            User.deleted_at.is_(None),
            UserMembership.entity_id == user.entity_id,
            UserMembership.status == "active",
            UserMembership.deleted_at.is_(None),
        )
    )
    if not found:
        raise HTTPException(422, "Choose an active member of this company")


def client_json(row):
    return {
        "id": row.id,
        "entity_id": row.entity_id,
        "name": row.name,
        "email": row.email,
        "phone": row.phone,
        "address": row.address,
        "status": row.status,
        "metadata": row.meta or {},
    }


def record_json(row):
    return {
        column.name: format(value, "f") if isinstance((value := getattr(row, column.name)), Decimal) else value
        for column in row.__table__.columns
        if column.name != "deleted_at"
    }


async def clients(
    search: str = "",
    lifecycle: Lifecycle | None = None,
    owner_id: str = "",
    status: Literal["active", "inactive"] = "active",
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
    user: User = None,
    db: AsyncSession = None,
):
    q = select(Client).where(Client.entity_id == user.entity_id, Client.deleted_at.is_(None))
    if status in ("active", "inactive"):
        q = q.where(Client.status == status)
    if search.strip():
        like = f"%{search.strip()}%"
        q = q.where(or_(Client.name.ilike(like), Client.email.ilike(like), Client.phone.ilike(like)))
    if lifecycle:
        q = q.where(func.coalesce(Client.meta["crm"]["lifecycle"].astext, "lead") == lifecycle)
    if owner_id:
        q = q.where(Client.meta["crm"]["owner_id"].astext == owner_id)
    if tag:
        q = q.where(Client.meta["crm"]["tags"].contains([tag]))
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = (
        (await db.execute(q.order_by(Client.created_at.desc(), Client.id).limit(limit).offset(offset))).scalars().all()
    )
    return {"items": [client_json(r) for r in rows], "total": total}


async def create(req: Profile, user: User = None, db: AsyncSession = None):
    await owner(db, user, req.owner_id)
    fields = req.model_dump()
    basics = {key: fields.pop(key) for key in ("name", "email", "phone", "address", "status")}
    row = await create_client(db, user.entity_id, **basics, meta={"crm": fields})
    return client_json(row)


async def profile(client_id: str, req: Profile, user: User = None, db: AsyncSession = None):
    row = await db.scalar(
        select(Client)
        .where(Client.id == client_id, Client.entity_id == user.entity_id, Client.deleted_at.is_(None))
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Client not found")
    if req.owner_id != (row.meta or {}).get("crm", {}).get("owner_id"):
        await owner(db, user, req.owner_id)
    fields = req.model_dump()
    basics = {key: fields.pop(key) for key in ("name", "email", "phone", "address", "status")}
    basics["meta"] = {**(row.meta or {}), "crm": fields}
    row = await update_client(db, client_id, user.entity_id, **basics)
    return client_json(row)


async def overview(user: User = None, db: AsyncSession = None):
    scope = [Client.entity_id == user.entity_id, Client.deleted_at.is_(None), Client.status == "active"]
    lifecycle = func.coalesce(Client.meta["crm"]["lifecycle"].astext, "lead").label("lifecycle")
    counts = (await db.execute(select(lifecycle, func.count()).where(*scope).group_by("lifecycle"))).all()
    stages = (
        await db.execute(
            select(
                ClientOpportunity.stage,
                ClientOpportunity.currency,
                func.count(),
                func.sum(ClientOpportunity.amount),
                func.sum(ClientOpportunity.amount * ClientOpportunity.probability / 100),
            )
            .join(Client, Client.id == ClientOpportunity.client_id)
            .where(*scope, ClientOpportunity.entity_id == user.entity_id, ClientOpportunity.deleted_at.is_(None))
            .group_by(ClientOpportunity.stage, ClientOpportunity.currency)
        )
    ).all()
    now = datetime.now(timezone.utc)
    due = (
        select(ClientFollowup, Client.name)
        .join(Client, Client.id == ClientFollowup.client_id)
        .where(
            *scope,
            ClientFollowup.entity_id == user.entity_id,
            ClientFollowup.deleted_at.is_(None),
            ClientFollowup.status == "open",
        )
    )
    overdue = await db.scalar(select(func.count()).select_from(due.where(ClientFollowup.due_at < now).subquery()))
    upcoming = (await db.execute(due.order_by(ClientFollowup.due_at, ClientFollowup.id).limit(12))).all()
    return {
        "clients": dict(counts),
        "overdue": overdue,
        "pipeline": [
            {"stage": s, "currency": c, "count": n, "amount": format(a, "f"), "weighted": format(w, "f")}
            for s, c, n, a, w in stages
        ],
        "followups": [{**record_json(r), "client_name": name} for r, name in upcoming],
    }


MODELS = {"activities": ClientActivity, "opportunities": ClientOpportunity, "followups": ClientFollowup}


async def rows(db, user, client_id, kind, limit, offset):
    await customer(db, user, client_id)
    model = MODELS[kind]
    q = select(model).where(model.entity_id == user.entity_id, model.client_id == client_id, model.deleted_at.is_(None))
    order = (
        model.occurred_at.desc()
        if kind == "activities"
        else model.due_at
        if kind == "followups"
        else model.created_at.desc()
    )
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    items = (await db.execute(q.order_by(order, model.id).limit(limit).offset(offset))).scalars().all()
    return {"items": [record_json(r) for r in items], "total": total}


async def save(db, user, client_id, kind, req, record_id=None):
    await customer(db, user, client_id, write=True)
    model = MODELS[kind]
    data = req.model_dump()
    old_stage = None
    if record_id:
        row = await db.scalar(
            select(model)
            .where(
                model.id == record_id,
                model.entity_id == user.entity_id,
                model.client_id == client_id,
                model.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(404, "CRM record not found")
        if kind == "activities" and row.kind == "deal":
            raise HTTPException(409, "Sales stage history cannot be edited")
        old_stage = getattr(row, "stage", None)
    else:
        row = model(entity_id=user.entity_id, client_id=client_id, created_by=user.id)
    if "owner_id" in data and data["owner_id"] != getattr(row, "owner_id", None):
        await owner(db, user, data["owner_id"])
    if not record_id:
        db.add(row)
    if kind == "opportunities":
        if data["stage"] in ("won", "lost"):
            data["probability"] = 100 if data["stage"] == "won" else 0
        if data["stage"] != "lost":
            data["loss_reason"] = ""
    if kind == "followups":
        data["completed_at"] = (
            getattr(row, "completed_at", None) or datetime.now(timezone.utc) if data["status"] == "done" else None
        )
    for key, value in data.items():
        setattr(row, key, value)
    if kind == "opportunities" and old_stage != data["stage"]:
        db.add(
            ClientActivity(
                entity_id=user.entity_id,
                client_id=client_id,
                created_by=user.id,
                kind="deal",
                subject=data["title"],
                body=f"{old_stage or 'created'} → {data['stage']}",
                occurred_at=datetime.now(timezone.utc),
            )
        )
    await db.flush()
    await db.refresh(row)
    return record_json(row)


async def remove(db, user, client_id, kind, record_id):
    await customer(db, user, client_id, write=True)
    model = MODELS[kind]
    row = await db.scalar(
        select(model)
        .where(
            model.id == record_id,
            model.entity_id == user.entity_id,
            model.client_id == client_id,
            model.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "CRM record not found")
    if kind == "activities" and row.kind == "deal":
        raise HTTPException(409, "Sales stage history cannot be removed")
    row.deleted_at = datetime.now(timezone.utc)
    await db.flush()


async def activities(client_id: str, limit: int = 50, offset: int = 0, user: User = None, db: AsyncSession = None):
    return await rows(db, user, client_id, "activities", limit, offset)


async def activity(
    client_id: str, req: Activity, record_id: str | None = None, user: User = None, db: AsyncSession = None
):
    return await save(db, user, client_id, "activities", req, record_id)


async def opportunities(client_id: str, limit: int = 50, offset: int = 0, user: User = None, db: AsyncSession = None):
    return await rows(db, user, client_id, "opportunities", limit, offset)


async def opportunity(
    client_id: str, req: Opportunity, record_id: str | None = None, user: User = None, db: AsyncSession = None
):
    return await save(db, user, client_id, "opportunities", req, record_id)


async def followups(client_id: str, limit: int = 50, offset: int = 0, user: User = None, db: AsyncSession = None):
    return await rows(db, user, client_id, "followups", limit, offset)


async def followup(
    client_id: str, req: Followup, record_id: str | None = None, user: User = None, db: AsyncSession = None
):
    return await save(db, user, client_id, "followups", req, record_id)


async def delete_record(
    client_id: str,
    kind: Literal["activities", "opportunities", "followups"],
    record_id: str,
    user: User = None,
    db: AsyncSession = None,
):
    await remove(db, user, client_id, kind, record_id)


async def pipeline(
    stage: Stage | None = None,
    owner_id: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    user: User = None,
    db: AsyncSession = None,
):
    q = (
        select(ClientOpportunity, Client.name)
        .join(Client, Client.id == ClientOpportunity.client_id)
        .where(
            Client.entity_id == user.entity_id,
            Client.deleted_at.is_(None),
            Client.status == "active",
            ClientOpportunity.entity_id == user.entity_id,
            ClientOpportunity.deleted_at.is_(None),
        )
    )
    if stage:
        q = q.where(ClientOpportunity.stage == stage)
    if owner_id:
        q = q.where(ClientOpportunity.owner_id == owner_id)
    if search.strip():
        q = q.where(or_(Client.name.ilike(f"%{search.strip()}%"), ClientOpportunity.title.ilike(f"%{search.strip()}%")))
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    items = (
        await db.execute(
            q.order_by(ClientOpportunity.created_at.desc(), ClientOpportunity.id).limit(limit).offset(offset)
        )
    ).all()
    return {"items": [{**record_json(r), "client_name": name} for r, name in items], "total": total}


async def followup_queue(
    status: Literal["open", "done"] = "open",
    owner_id: str = "",
    overdue: bool = False,
    limit: int = 50,
    offset: int = 0,
    user: User = None,
    db: AsyncSession = None,
):
    q = (
        select(ClientFollowup, Client.name)
        .join(Client, Client.id == ClientFollowup.client_id)
        .where(
            Client.entity_id == user.entity_id,
            Client.deleted_at.is_(None),
            Client.status == "active",
            ClientFollowup.entity_id == user.entity_id,
            ClientFollowup.deleted_at.is_(None),
            ClientFollowup.status == status,
        )
    )
    if owner_id:
        q = q.where(ClientFollowup.owner_id == owner_id)
    if overdue:
        q = q.where(ClientFollowup.due_at < datetime.now(timezone.utc), ClientFollowup.status == "open")
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    items = (await db.execute(q.order_by(ClientFollowup.due_at, ClientFollowup.id).limit(limit).offset(offset))).all()
    return {"items": [{**record_json(r), "client_name": name} for r, name in items], "total": total}
