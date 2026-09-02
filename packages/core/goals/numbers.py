"""Exact JSON projection for persisted Goal numbers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated

from pydantic import BeforeValidator

GoalNumberJSON = int | float | str

_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991
_MAX_ABS_GOAL_NUMBER = Decimal("10000000000000000")
_GOAL_NUMBER_QUANTUM = Decimal("0.0001")


def validate_goal_number(value: object) -> Decimal:
    """Return a finite Decimal that fits the persisted NUMERIC(20, 4)."""
    if isinstance(value, bool):
        raise ValueError("must be a finite number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError("must be a finite number") from None
    if not number.is_finite():
        raise ValueError("must be a finite number")
    if abs(number) >= _MAX_ABS_GOAL_NUMBER:
        raise ValueError("exceeds the supported numeric range")
    exponent = number.as_tuple().exponent
    if exponent < -4:
        excess_digits = -4 - exponent
        if any(number.as_tuple().digits[-excess_digits:]):
            raise ValueError("must have at most 4 decimal places")
    return number


def project_stat_value_to_goal_number(value: object) -> Decimal:
    """Project a NUMERIC(24,6) Stat value into Goal's NUMERIC(20,4)."""
    if isinstance(value, bool):
        raise ValueError("must be a finite number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        projected = number.quantize(_GOAL_NUMBER_QUANTUM, rounding=ROUND_HALF_UP)
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        raise ValueError("must be a finite number") from None
    return validate_goal_number(projected)


GoalNumberInput = Annotated[Decimal, BeforeValidator(validate_goal_number)]


def goal_number_to_json(value: Decimal | None) -> GoalNumberJSON | None:
    """Return a JSON primitive that round-trips to the same Decimal value."""
    if value is None:
        return None
    if value == value.to_integral_value():
        integer = int(value)
        if abs(integer) <= _MAX_SAFE_JSON_INTEGER:
            return integer
        return format(value, "f")
    candidate = float(value)
    if Decimal(str(candidate)) == value:
        return candidate
    return format(value, "f")
