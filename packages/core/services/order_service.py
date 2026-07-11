"""Order service — CRUD, status changes, items, statistics."""
from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.base import generate_ulid
from packages.core.models.order import BusinessOrder, BusinessOrderItem


def _generate_order_number() -> str:
    """Generate a unique order number like ORD-20260421-A3F7."""
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y%m%d")
    random_part = "".join(random.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=4))
    return f"ORD-{date_part}-{random_part}"


# ── Orders ──

async def list_orders(
    db: AsyncSession, entity_id: str, *,
    status: str | None = None,
    client_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[BusinessOrder], int]:
    q = select(BusinessOrder).where(BusinessOrder.entity_id == entity_id)
    count_q = select(func.count()).select_from(BusinessOrder).where(BusinessOrder.entity_id == entity_id)

    if status:
        q = q.where(BusinessOrder.status == status)
        count_q = count_q.where(BusinessOrder.status == status)
    if client_id:
        q = q.where(BusinessOrder.client_id == client_id)
        count_q = count_q.where(BusinessOrder.client_id == client_id)

    q = q.order_by(BusinessOrder.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(q)
    count_result = await db.execute(count_q)
    return list(result.scalars().all()), count_result.scalar_one()


async def get_order(db: AsyncSession, entity_id: str, order_id: str) -> Optional[BusinessOrder]:
    result = await db.execute(
        select(BusinessOrder).where(BusinessOrder.id == order_id, BusinessOrder.entity_id == entity_id)
    )
    return result.scalar_one_or_none()


async def create_order(
    db: AsyncSession, entity_id: str, user_id: str, *,
    title: str,
    description: str | None = None,
    client_id: str | None = None,
    assignee_id: str | None = None,
    order_type: str = "service",
    amount: float = 0,
    currency: str = "USD",
    details: dict | None = None,
    notes: str | None = None,
    due_date: str | None = None,
) -> BusinessOrder:
    order = BusinessOrder(
        id=generate_ulid(),
        entity_id=entity_id,
        order_number=_generate_order_number(),
        title=title,
        description=description,
        client_id=client_id,
        assignee_id=assignee_id,
        creator_id=user_id,
        order_type=order_type,
        amount=amount,
        currency=currency,
        details=details or {},
        notes=notes,
        due_date=datetime.fromisoformat(due_date) if due_date else None,
    )
    db.add(order)
    await db.flush()

    from packages.core.services.event_emitter import emit
    emit(entity_id, "order.created", source="order_service", payload={
        "order_id": order.id, "order_number": order.order_number, "title": title,
    })

    return order


async def update_order(
    db: AsyncSession, entity_id: str, order_id: str, **fields,
) -> Optional[BusinessOrder]:
    order = await get_order(db, entity_id, order_id)
    if not order:
        return None

    # Handle due_date string → datetime conversion
    if "due_date" in fields and fields["due_date"] is not None:
        fields["due_date"] = datetime.fromisoformat(fields["due_date"])

    for k, v in fields.items():
        if hasattr(order, k) and v is not None:
            setattr(order, k, v)

    await db.flush()
    return order


async def delete_order(db: AsyncSession, entity_id: str, order_id: str) -> bool:
    order = await get_order(db, entity_id, order_id)
    if not order:
        return False
    # Remove items first
    await db.execute(
        delete(BusinessOrderItem).where(BusinessOrderItem.order_id == order_id)
    )
    await db.delete(order)
    await db.flush()
    return True


async def update_order_status(
    db: AsyncSession, entity_id: str, order_id: str, status: str,
) -> Optional[BusinessOrder]:
    order = await get_order(db, entity_id, order_id)
    if not order:
        return None

    old_status = order.status
    order.status = status

    now = datetime.now(timezone.utc)
    if status in ("completed",):
        order.completed_at = now
    if status == "cancelled":
        order.completed_at = now

    await db.flush()

    from packages.core.services.event_emitter import emit
    emit(entity_id, "order.status_changed", source="order_service", payload={
        "order_id": order_id, "order_number": order.order_number,
        "old_status": old_status, "new_status": status,
    })

    return order


# ── Order Items ──

async def list_order_items(db: AsyncSession, order_id: str) -> list[BusinessOrderItem]:
    result = await db.execute(
        select(BusinessOrderItem)
        .where(BusinessOrderItem.order_id == order_id)
        .order_by(BusinessOrderItem.created_at.asc())
    )
    return list(result.scalars().all())


async def add_order_item(
    db: AsyncSession, order_id: str, *,
    name: str,
    description: str | None = None,
    quantity: int = 1,
    unit_price: float = 0,
    details: dict | None = None,
) -> BusinessOrderItem:
    total_price = quantity * unit_price
    item = BusinessOrderItem(
        id=generate_ulid(),
        order_id=order_id,
        name=name,
        description=description,
        quantity=quantity,
        unit_price=unit_price,
        total_price=total_price,
        details=details or {},
    )
    db.add(item)
    await db.flush()
    return item


async def update_order_item(
    db: AsyncSession, item_id: str, **fields,
) -> Optional[BusinessOrderItem]:
    result = await db.execute(
        select(BusinessOrderItem).where(BusinessOrderItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return None

    for k, v in fields.items():
        if hasattr(item, k) and v is not None:
            setattr(item, k, v)

    # Recalculate total_price if quantity or unit_price changed
    if "quantity" in fields or "unit_price" in fields:
        item.total_price = item.quantity * item.unit_price

    await db.flush()
    return item


async def remove_order_item(db: AsyncSession, item_id: str) -> bool:
    result = await db.execute(
        select(BusinessOrderItem).where(BusinessOrderItem.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return False
    await db.delete(item)
    await db.flush()
    return True


# ── Statistics ──

async def get_order_stats(db: AsyncSession, entity_id: str) -> dict:
    """Return order counts by status and total revenue."""
    # Counts by status
    status_q = (
        select(BusinessOrder.status, func.count())
        .where(BusinessOrder.entity_id == entity_id)
        .group_by(BusinessOrder.status)
    )
    status_result = await db.execute(status_q)
    counts_by_status = {row[0]: row[1] for row in status_result.all()}

    # Total revenue (paid_amount for completed orders)
    revenue_q = (
        select(func.coalesce(func.sum(BusinessOrder.paid_amount), 0))
        .where(
            BusinessOrder.entity_id == entity_id,
            BusinessOrder.status == "completed",
        )
    )
    revenue_result = await db.execute(revenue_q)
    total_revenue = float(revenue_result.scalar_one())

    # Total orders
    total_q = select(func.count()).select_from(BusinessOrder).where(BusinessOrder.entity_id == entity_id)
    total_result = await db.execute(total_q)
    total_orders = total_result.scalar_one()

    return {
        "total_orders": total_orders,
        "counts_by_status": counts_by_status,
        "total_revenue": total_revenue,
    }
