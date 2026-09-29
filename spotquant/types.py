"""Shared outcomes and decimal helpers. No exchange I/O."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

D = Decimal
ZERO = D(0)


class Blocked(RuntimeError):
    """A known prerequisite is unsatisfied. No new exposure is allowed."""


class Unknown(RuntimeError):
    """An observation is incomplete. Do not treat it as success or as an empty account."""


def number(value: Any, name: str = 'number', *, positive: bool = False, nonnegative: bool = False) -> D:
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise Blocked(f'invalid {name}') from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise Blocked(f'invalid {name}')
    return result


def floor_step(value: D, step: D) -> D:
    if step <= 0 or value < 0:
        raise Blocked('invalid quantity or step')
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def serial(value: Any) -> Any:
    if isinstance(value, D):
        return format(value, 'f')
    if hasattr(value, '__dataclass_fields__'):
        return serial(asdict(value))
    if isinstance(value, dict):
        return {str(key): serial(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [serial(item) for item in value]
    return value
