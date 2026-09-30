from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext

CANONICAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


def decimal_value(value: str | int | Decimal) -> Decimal:
    """Create canonical Decimal values without ever accepting binary float."""
    if isinstance(value, float):
        raise TypeError("binary float is forbidden in canonical numeric code")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"unsupported decimal input type: {type(value)!r}")


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    with localcontext(CANONICAL_CONTEXT):
        units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
        return units * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    with localcontext(CANONICAL_CONTEXT):
        units = (value / step).to_integral_value(rounding=ROUND_CEILING)
        return units * step


@dataclass(frozen=True, slots=True)
class ScaledIntCodec:
    """Exact fixed-scale storage codec for vectorized bulk-data layers."""

    scale: int = 10

    @property
    def factor(self) -> Decimal:
        return Decimal(10) ** self.scale

    def encode(self, value: str | int | Decimal) -> int:
        dec = decimal_value(value)
        with localcontext(CANONICAL_CONTEXT):
            scaled = dec * self.factor
            integral = scaled.to_integral_value()
        if scaled != integral:
            raise ValueError(f"{dec} is not exactly representable at scale {self.scale}")
        return int(integral)

    def decode(self, value: int) -> Decimal:
        with localcontext(CANONICAL_CONTEXT):
            return Decimal(value) / self.factor
