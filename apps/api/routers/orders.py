"""Order / Commerce endpoints — CRUD, status changes, items, statistics."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.database import get_db
from packages.core.models.user import User
from packages.core.services.order_service import (
    list_orders, get_order, create_order, update_order, delete_order,
    update_order_status, list_order_items, add_order_item,
    update_order_item, remove_order_item, get_order_stats,
)
from apps.api.deps import get_current_user

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


# ── Schemas ──

class OrderResponse(BaseModel):
    id: str
    entity_id: str
    order_number: str
    title: str
    description: str | None = None
    client_id: str | None = None
    assignee_id: str | None = None
    creator_id: str | None = None
    status: str
    order_type: str
    amount: float
    currency: str
    paid_amount: float
    payment_status: str
    details: dict = {}
    notes: str | None = None
    due_date: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class OrderListResponse(BaseModel):
    items: list[OrderResponse]
    total: int


class OrderCreateRequest(BaseModel):
    title: str
    description: str | None = None
    client_id: str | None = None
    assignee_id: str | None = None
    order_type: str = "service"
    amount: float = 0
    currency: str = "USD"
    details: dict = {}
    notes: str | None = None
    due_date: str | None = None


class OrderUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    client_id: str | None = None
    assignee_id: str | None = None
    order_type: str | None = None
    amount: float | None = None
    currency: str | None = None
    paid_amount: float | None = None
    payment_status: str | None = None
    details: dict | None = None
    notes: str | None = None
    due_date: str | None = None


class OrderStatusRequest(BaseModel):
    status: str


class OrderItemResponse(BaseModel):
    id: str
    order_id: str
    name: str
    description: str | None = None
    quantity: int
    unit_price: float
    total_price: float
    details: dict = {}
    created_at: str | None = None


class OrderItemCreateRequest(BaseModel):
    name: str
    description: str | None = None
    quantity: int = 1
    unit_price: float = 0
    details: dict = {}


class OrderItemUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    quantity: int | None = None
    unit_price: float | None = None
    details: dict | None = None


class OrderStatsResponse(BaseModel):
    total_orders: int
    counts_by_status: dict[str, int]
    total_revenue: float


# ── Helpers ──

def _to_order_response(o) -> OrderResponse:
    return OrderResponse(
        id=o.id, entity_id=o.entity_id, order_number=o.order_number,
        title=o.title, description=o.description,
        client_id=o.client_id, assignee_id=o.assignee_id,
        creator_id=o.creator_id, status=o.status, order_type=o.order_type,
        amount=o.amount, currency=o.currency,
        paid_amount=o.paid_amount, payment_status=o.payment_status,
        details=o.details or {}, notes=o.notes,
        due_date=o.due_date.isoformat() if o.due_date else None,
        completed_at=o.completed_at.isoformat() if o.completed_at else None,
        created_at=o.created_at.isoformat() if o.created_at else None,
        updated_at=o.updated_at.isoformat() if o.updated_at else None,
    )


def _to_item_response(i) -> OrderItemResponse:
    return OrderItemResponse(
        id=i.id, order_id=i.order_id, name=i.name,
        description=i.description, quantity=i.quantity,
        unit_price=i.unit_price, total_price=i.total_price,
        details=i.details or {},
        created_at=i.created_at.isoformat() if i.created_at else None,
    )


# ── Order Endpoints ──

@router.get("", response_model=OrderListResponse)
async def list_entity_orders(
    status: str | None = Query(None),
    client_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    orders, total = await list_orders(
        db, user.entity_id,
        status=status, client_id=client_id,
        limit=limit, offset=offset,
    )
    return OrderListResponse(items=[_to_order_response(o) for o in orders], total=total)


@router.post("", response_model=OrderResponse, status_code=201)
async def create_new_order(
    req: OrderCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await create_order(
        db, user.entity_id, user.id,
        title=req.title, description=req.description,
        client_id=req.client_id, assignee_id=req.assignee_id,
        order_type=req.order_type, amount=req.amount,
        currency=req.currency, details=req.details,
        notes=req.notes, due_date=req.due_date,
    )
    return _to_order_response(order)


@router.get("/stats", response_model=OrderStatsResponse)
async def order_statistics(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stats = await get_order_stats(db, user.entity_id)
    return OrderStatsResponse(**stats)


@router.get("/{order_id}", response_model=OrderResponse)
async def get_one_order(
    order_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await get_order(db, user.entity_id, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return _to_order_response(order)


@router.put("/{order_id}", response_model=OrderResponse)
async def update_one_order(
    order_id: str,
    req: OrderUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await update_order(db, user.entity_id, order_id, **req.model_dump(exclude_none=True))
    if not order:
        raise HTTPException(404, "Order not found")
    return _to_order_response(order)


@router.delete("/{order_id}", status_code=204)
async def delete_one_order(
    order_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ok = await delete_order(db, user.entity_id, order_id)
    if not ok:
        raise HTTPException(404, "Order not found")


@router.put("/{order_id}/status", response_model=OrderResponse)
async def change_order_status(
    order_id: str,
    req: OrderStatusRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await update_order_status(db, user.entity_id, order_id, req.status)
    if not order:
        raise HTTPException(404, "Order not found")
    return _to_order_response(order)


# ── Order Item Endpoints ──

@router.get("/{order_id}/items", response_model=list[OrderItemResponse])
async def list_items(
    order_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify order belongs to entity
    order = await get_order(db, user.entity_id, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    items = await list_order_items(db, order_id)
    return [_to_item_response(i) for i in items]


@router.post("/{order_id}/items", response_model=OrderItemResponse, status_code=201)
async def add_item(
    order_id: str,
    req: OrderItemCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await get_order(db, user.entity_id, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    item = await add_order_item(
        db, order_id,
        name=req.name, description=req.description,
        quantity=req.quantity, unit_price=req.unit_price,
        details=req.details,
    )
    return _to_item_response(item)


@router.put("/{order_id}/items/{item_id}", response_model=OrderItemResponse)
async def update_item(
    order_id: str,
    item_id: str,
    req: OrderItemUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify order belongs to entity
    order = await get_order(db, user.entity_id, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    item = await update_order_item(db, item_id, **req.model_dump(exclude_none=True))
    if not item:
        raise HTTPException(404, "Order item not found")
    return _to_item_response(item)


@router.delete("/{order_id}/items/{item_id}", status_code=204)
async def delete_item(
    order_id: str,
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    order = await get_order(db, user.entity_id, order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    ok = await remove_order_item(db, item_id)
    if not ok:
        raise HTTPException(404, "Order item not found")
