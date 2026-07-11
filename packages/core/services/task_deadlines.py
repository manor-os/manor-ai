"""Shared deadline helpers for task queries."""
from __future__ import annotations

from sqlalchemy import Date, and_, cast, func, or_


def task_deadline_overdue_expr(deadline_col, *, now_expr=None, current_date_expr=None):
    """Return SQL for overdue task deadlines.

    Date-only deadlines are stored as midnight timestamps by the task UI.
    Treat those as due through the end of that calendar date, while keeping
    explicit date-time deadlines precise.
    """
    now_value = now_expr if now_expr is not None else func.now()
    current_date_value = current_date_expr if current_date_expr is not None else func.current_date()
    is_midnight_deadline = func.date_trunc("day", deadline_col) == deadline_col
    return or_(
        and_(is_midnight_deadline, cast(deadline_col, Date) < current_date_value),
        and_(~is_midnight_deadline, deadline_col < now_value),
    )
