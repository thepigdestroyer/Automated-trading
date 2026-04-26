from __future__ import annotations

from t212_cfd_bot.risk import atr_based_quantity, confidence_multiplier


def test_confidence_multiplier_clamps():
    assert confidence_multiplier(0.5, min_conf=0.72, max_mult=2.5) == 1.0
    assert confidence_multiplier(0.72, min_conf=0.72, max_mult=2.5) == 1.0
    assert abs(confidence_multiplier(0.95, min_conf=0.72, max_mult=2.5) - 2.5) < 1e-9
    mid = confidence_multiplier(0.835, min_conf=0.72, max_mult=2.5)
    assert 1.7 < mid < 1.8


def test_atr_based_quantity():
    qty = atr_based_quantity(target_cash_risk=50.0, price=2900.0, atr14=15.0, atr_mult=1.5, stop_pct_floor=0.005)
    stop_distance = 22.5  # 15 * 1.5
    assert abs(qty - (50.0 / stop_distance)) < 1e-6


def test_atr_based_quantity_uses_floor_when_atr_tiny():
    # When ATR stop is smaller than price * floor, floor wins
    qty = atr_based_quantity(target_cash_risk=10.0, price=100.0, atr14=0.01, atr_mult=1.5, stop_pct_floor=0.02)
    floor_stop = 100.0 * 0.02  # = 2.0
    assert abs(qty - (10.0 / floor_stop)) < 1e-6


def test_atr_based_quantity_zero_price():
    assert atr_based_quantity(target_cash_risk=50.0, price=0.0, atr14=1.0, atr_mult=1.5, stop_pct_floor=0.01) == 0.0
