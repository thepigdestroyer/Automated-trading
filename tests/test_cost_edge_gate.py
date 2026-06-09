"""Tests for the cost-vs-edge gate (2026-05 adversarial review).

The gate refuses entries whose targeted reward (take-profit distance) does not
clear the estimated round-trip cost (spread + 2x slippage + financing over the
holding horizon) by at least cost_edge_multiple.
"""

from __future__ import annotations

import logging

import pytest

from trading_bot.config import Settings
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    IndicatorSnapshot,
    SymbolContext,
    TradeDecision,
)
from trading_bot.risk import RiskManager, _round_trip_cost_price_units


# --- pure cost helper -------------------------------------------------


def test_round_trip_cost_components():
    # price 100, spread 20bps, slippage 5bps (x2), swap 200bps over half a year.
    cost = _round_trip_cost_price_units(
        price=100.0, spread_bps=20, slippage_bps=5,
        swap_annualised_bps=200, hold_hours=365.25 * 24 / 2,
    )
    # spread 0.0020 + slippage 0.0010 + financing 0.0100 (half of 200bps) = 0.013
    # × price 100 = 1.30
    assert cost == pytest.approx(100.0 * (0.0020 + 0.0010 + 0.0100), rel=1e-6)


def test_round_trip_cost_zero_price():
    assert _round_trip_cost_price_units(
        price=0.0, spread_bps=20, slippage_bps=5,
        swap_annualised_bps=200, hold_hours=24,
    ) == 0.0


def test_financing_scales_with_hold_time():
    short = _round_trip_cost_price_units(price=100, spread_bps=10, slippage_bps=5,
                                         swap_annualised_bps=200, hold_hours=1)
    long = _round_trip_cost_price_units(price=100, spread_bps=10, slippage_bps=5,
                                        swap_annualised_bps=200, hold_hours=240)
    assert long > short  # more financing over a longer hold


# --- gate integration -------------------------------------------------


def _ctx(symbol: str, price: float, atr: float) -> SymbolContext:
    ind = IndicatorSnapshot(
        price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
        rsi_14=55.0, macd=0.1, macd_signal=0.05, atr_14=atr,
        volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
    )
    return SymbolContext(
        symbol=symbol, display_name=symbol, asset_class="index",
        as_of="t", indicators=ind, headlines=[], chart_note="t",
    )


def _decision(symbol: str) -> DecisionEnvelope:
    return DecisionEnvelope(
        market_regime="mixed", summary="t",
        trades=[TradeDecision(symbol=symbol, action="BUY", confidence=0.85,
                              thesis="t", news_bias="neutral", invalidate_if="x")],
    )


def _acct() -> AccountSnapshot:
    return AccountSnapshot(mode="demo", currency="GBP", total_funds=10_000.0,
                           free_funds=10_000.0, open_positions=[])


def _gr(symbol: str) -> dict:
    return {"block_new_entries": False, "block_reasons": [], "close_requests": [],
            "econ_events": [], "earnings_events": {}, "session_open": {symbol: True}}


def test_healthy_reward_passes_cost_gate():
    # SPY scale=1.0, price 540, atr 10 -> stop ~15, TP ~30 (RR2). Cost is tiny
    # vs a 30-point reward, so the plan passes.
    s = Settings(cost_edge_multiple=2.0, risk_reward_ratio=2.0)
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("SPY"), symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0)},
        account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
    )
    assert len(plans) == 1


def test_marginal_reward_rejected_by_cost_gate(caplog):
    # Healthy ATR setup that passes all OTHER gates (stop within 3xATR, budget
    # fine), but an absurdly high cost_edge_multiple forces the COST gate to
    # fire specifically. This isolates the cost gate from the stop/spread gate.
    s = Settings(cost_edge_multiple=1000.0, risk_reward_ratio=2.0)
    rm = RiskManager(s)
    with caplog.at_level(logging.INFO, logger="trading_bot.risk"):
        plans = rm.build_execution_plans(
            decision=_decision("SPY"),
            symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0)},
            account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
        )
    assert plans == []
    assert any("COST_VS_EDGE_BREACH" in r.getMessage() for r in caplog.records)


def test_cost_gate_disabled_when_multiple_zero():
    # Same setup, but with the gate disabled (multiple=0) the plan passes —
    # proving it was the COST gate (not another guard) that rejected above.
    s = Settings(cost_edge_multiple=0.0, risk_reward_ratio=2.0)
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("SPY"),
        symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0)},
        account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
    )
    assert len(plans) == 1
