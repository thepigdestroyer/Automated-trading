"""Per-instrument stop_distance unit conversion (review 2026-04-29-adhoc-1 #1).

The Portfolio Manager + RiskManager produce stop_distance and
take_profit_distance in bot price-units (the data feed's decimal dollars),
but the executor compares them against IG dealing-rule minimums quoted in
IG points (1pt = 1¢ for WTI/NATGAS/BRENT). On 2026-04-29 the live bot
generated 8 WTI BUY plans with stop_distance ≈ 1.38 (= $1.38) and IG
rejected them all with `STOP_TOO_TIGHT_VS_SPREAD: 1.38 < 12.0`.

The fix scales stop_distance and take_profit_distance by
``InstrumentDefinition.ig_price_scale`` BEFORE the plan is handed to the
executor. WTI/NATGAS/BRENT scale = 100 (×100 cents per dollar);
FX/equity/index scale = 1.0 (no rescale).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.config import Settings
from trading_bot.instruments import get_instrument
from trading_bot.models import (
    AccountSnapshot,
    DecisionEnvelope,
    Headline,
    IndicatorSnapshot,
    SymbolContext,
    TradeDecision,
)
from trading_bot.risk import RiskManager


def _make_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        broker="paper",
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        json_log_path=tmp_path / "events.jsonl",
        **overrides,
    )


def _make_context(symbol: str, price: float, atr: float = 0.5) -> SymbolContext:
    instrument = get_instrument(symbol)
    indicators = IndicatorSnapshot(
        price=price,
        sma_20=price,
        sma_50=price,
        ema_12=price,
        ema_26=price,
        rsi_14=55.0,
        macd=0.0,
        macd_signal=0.0,
        atr_14=atr,
        volatility_20d=0.20,
        return_1d=0.001,
        return_5d=0.005,
    )
    return SymbolContext(
        symbol=instrument.symbol,
        display_name=instrument.display_name,
        asset_class=instrument.asset_class,
        as_of=datetime.now(timezone.utc).isoformat(),
        indicators=indicators,
        headlines=[],
        chart_note="test",
    )


def _make_decision(symbol: str, *, stop_loss_pct: float = 0.012,
                   take_profit_pct: float = 0.024) -> DecisionEnvelope:
    return DecisionEnvelope(
        market_regime="neutral-wait",
        summary="test",
        trades=[
            TradeDecision(
                symbol=symbol,
                action="BUY",
                confidence=0.80,
                thesis="test",
                news_bias="positive",
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                invalidate_if="thesis fails",
            )
        ],
    )


def _account(tmp_path) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=10_000.0, free_funds=10_000.0, open_positions=[],
    )


# --- ig_price_scale instrument metadata ----------------------------------


def test_wti_has_scale_100():
    inst = get_instrument("WTI")
    assert inst.ig_price_scale == 100.0


def test_brent_has_scale_100():
    inst = get_instrument("BRENT")
    assert inst.ig_price_scale == 100.0


def test_natgas_has_scale_100():
    inst = get_instrument("NATGAS")
    assert inst.ig_price_scale == 100.0


def test_fx_has_default_scale_1():
    """EURUSD must keep scale 1.0 — distances are already correct in IG pip units."""
    inst = get_instrument("EURUSD")
    assert inst.ig_price_scale == 1.0


def test_equity_has_default_scale_1():
    inst = get_instrument("AAPL")
    assert inst.ig_price_scale == 1.0


def test_index_has_default_scale_1():
    inst = get_instrument("SPX")
    assert inst.ig_price_scale == 1.0


def test_unknown_symbol_has_default_scale_1():
    """Per get_instrument fallback — synthetic instruments default to scale 1."""
    inst = get_instrument("DOESNOTEXIST")
    assert inst.ig_price_scale == 1.0


# --- risk plan emits scaled distances for energy commodities -----------


def test_wti_plan_distances_are_scaled_x100(tmp_path):
    settings = _make_settings(tmp_path)
    risk = RiskManager(settings)

    wti = _make_context("WTI", price=65.42, atr=0.50)
    decision = _make_decision("WTI")

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"WTI": wti},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert len(plans) == 1
    plan = plans[0]
    # Pre-scale stop_distance ≈ max(atr * mult, price * floor, 0.01)
    # = max(0.5 * 1.5, 65.42 * 0.005, 0.01) = max(0.75, 0.327, 0.01) = 0.75
    # Post-scale ×100 = 75.0
    assert plan.stop_distance == pytest.approx(75.0)
    # take_profit = stop * (tp_pct / sl_pct) = 0.75 * 2.0 = 1.5 → ×100 = 150.0
    assert plan.take_profit_distance == pytest.approx(150.0)


def test_natgas_plan_distances_are_scaled_x100(tmp_path):
    settings = _make_settings(tmp_path, allow_shorts=True)
    risk = RiskManager(settings)

    ng = _make_context("NATGAS", price=2.68, atr=0.05)
    decision = _make_decision("NATGAS", stop_loss_pct=0.010, take_profit_pct=0.020)

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"NATGAS": ng},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert len(plans) == 1
    plan = plans[0]
    # stop_distance pre-scale = max(0.05 * 1.5, 2.68 * 0.005, 0.01) = max(0.075, 0.0134, 0.01) = 0.075
    # Post-scale ×100 = 7.5
    assert plan.stop_distance == pytest.approx(7.5)


def test_aapl_plan_distance_unscaled(tmp_path):
    """Equity (scale=1.0) — stop_distance must remain in price units."""
    settings = _make_settings(tmp_path)
    risk = RiskManager(settings)

    aapl = _make_context("AAPL", price=180.0, atr=2.0)
    decision = _make_decision("AAPL")

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"AAPL": aapl},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert len(plans) == 1
    # stop_distance = max(2.0 * 1.5, 180 * 0.005, 0.01) = max(3.0, 0.9, 0.01) = 3.0
    # No scaling applied.
    assert plans[0].stop_distance == pytest.approx(3.0)


def test_eurusd_plan_distance_unscaled(tmp_path):
    """FX (scale=1.0) — distances stay in price units; do NOT auto-scale.

    Note: EURUSD is special because IG quotes it in 1/10000-units
    (1pt = 1 pip = 0.0001) but Yahoo quotes 1.083, so the executor
    is responsible for any final-stage normalisation. Risk just must
    not change pre-existing FX behaviour.
    """
    settings = _make_settings(tmp_path)
    risk = RiskManager(settings)

    eur = _make_context("EURUSD", price=1.0830, atr=0.0050)
    decision = _make_decision("EURUSD")

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"EURUSD": eur},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert len(plans) == 1
    # stop_distance = max(0.005 * 1.5, 1.083 * 0.005, 0.01) = max(0.0075, 0.00541, 0.01) = 0.01
    # No scaling applied (scale=1.0).
    assert plans[0].stop_distance == pytest.approx(0.01)


def test_3x_atr_check_runs_before_scaling(tmp_path):
    """Sanity check: the 3×ATR cap is computed in pre-scale price units,
    so a wide stop on WTI is still capped using ATR semantics, not
    after the ×100 explosion."""
    settings = _make_settings(tmp_path, atr_stop_multiplier=10.0)  # forces stop > 3*ATR
    risk = RiskManager(settings)

    wti = _make_context("WTI", price=65.42, atr=0.50)
    decision = _make_decision("WTI")

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"WTI": wti},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    # atr_stop_multiplier=10 → stop=5.0; 3*ATR=1.5; 5 > 1.5 → reject
    assert plans == []


# --- min_take_profit_distance gate (#2) --------------------------------


def test_min_tp_distance_gate_rejects_when_below_floor(tmp_path):
    """A plan whose post-scale TP is below min_take_profit_distance is
    rejected, so the executor doesn't see it and IG can't silently clamp."""
    settings = _make_settings(tmp_path, min_take_profit_distance=200.0)
    risk = RiskManager(settings)

    wti = _make_context("WTI", price=65.42, atr=0.50)
    # tp_pct=0.024, sl_pct=0.012 → tp_distance = 1.5 (×100 = 150) < 200
    decision = _make_decision("WTI", stop_loss_pct=0.012, take_profit_pct=0.024)

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"WTI": wti},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert plans == []


def test_min_tp_distance_gate_passes_when_above_floor(tmp_path):
    settings = _make_settings(tmp_path, min_take_profit_distance=100.0)
    risk = RiskManager(settings)

    wti = _make_context("WTI", price=65.42, atr=0.50)
    decision = _make_decision("WTI", stop_loss_pct=0.012, take_profit_pct=0.024)
    # tp_distance ×100 = 150 > 100 floor → passes

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"WTI": wti},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert len(plans) == 1


def test_min_tp_distance_default_disabled(tmp_path):
    """Default min_take_profit_distance=0.0 means gate disabled."""
    settings = _make_settings(tmp_path)
    assert settings.min_take_profit_distance == 0.0
    risk = RiskManager(settings)

    wti = _make_context("WTI", price=65.42, atr=0.50)
    decision = _make_decision("WTI", stop_loss_pct=0.012, take_profit_pct=0.013)
    # Tiny tp; gate disabled → still passes

    plans = risk.build_execution_plans(
        decision=decision,
        symbol_contexts={"WTI": wti},
        account_snapshot=_account(tmp_path),
        last_trade_times={},
    )
    assert len(plans) == 1
