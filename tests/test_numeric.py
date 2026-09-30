from __future__ import annotations

from decimal import Decimal

import pytest

from another_trade.numeric import ScaledIntCodec, ceil_to_step, decimal_value, floor_to_step


def test_binary_float_is_rejected() -> None:
    with pytest.raises(TypeError):
        decimal_value(0.1)  # type: ignore[arg-type]


def test_directional_step_rounding() -> None:
    value = Decimal("100.057")
    step = Decimal("0.01")
    assert floor_to_step(value, step) == Decimal("100.05")
    assert ceil_to_step(value, step) == Decimal("100.06")


def test_scaled_int_codec_is_exact_and_does_not_round() -> None:
    codec = ScaledIntCodec(scale=10)
    encoded = codec.encode("0.1234567890")
    assert encoded == 1_234_567_890
    assert codec.decode(encoded) == Decimal("0.123456789")
    with pytest.raises(ValueError):
        codec.encode("0.12345678901")
