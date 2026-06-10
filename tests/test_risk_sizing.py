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
from trading_bot.risk import RiskManager, atr_based_quantity, confidence_multiplier


def test_confidence_multiplier_clamps():
    assert confidence_multiplier(0.5, min_conf=0.72, max_mult=2.5) == 1.0
    assert confidence_multiplier(0.72, min_conf=0.72, max_mult=2.5) == 1.0
    assert abs(confidence_multiplier(0.95, min_conf=0.72, max_mult=2.5) - 2.5) < 1e-9
    mid = confidence_multiplier(0.835, min_conf=0.72, max_mult=2.5)
    assert 1.7 < mid < 1.8


def test_confidence_scaling_flattened_in_config_default():
    """2026-05 adversarial review fix: the production ceiling is flattened to
    1.0 so uncalibrated LLM confidence cannot lever position size up. At
    max_mult=1.0 every confidence maps to a flat 1.0x until calibration is
    proven."""
    from trading_bot.config import Settings

    s = Settings(openai_api_key="test")
    assert s.risk_confidence_scaling_max == 1.0
    # With the flattened ceiling, even max confidence stays at 1.0x.
    assert confidence_multiplier(0.95, min_conf=0.72,
                                 max_mult=s.risk_confidence_scaling_max) == 1.0
    assert confidence_multiplier(0.80, min_conf=0.72,
                                 max_mult=s.risk_confidence_scaling_max) == 1.0


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


# --- Per review 2026-05-05-adhoc-1 #1: qty re-derivation + budget-breach guard ---


def _ctx(symbol: str, price: float, atr: float | None = None) -> SymbolContext:
    p = price
    a = atr if atr is not None else price * 0.02
    return SymbolContext(
        symbol=symbol,
        display_name=symbol,
        asset_class="energy",
        as_of="2026-05-05T12:00:00+00:00",
        indicators=IndicatorSnapshot(
            price=p,
            sma_20=p,
            sma_50=p,
            ema_12=p,
            ema_26=p,
            rsi_14=50.0,
            macd=0.0,
            macd_signal=0.0,
            atr_14=a,
            volatility_20d=0.1,
            return_1d=0.0,
            return_5d=0.0,
        ),
        chart_note="test",
    )


def _decision(symbol: str, action: str, confidence: float = 0.85) -> DecisionEnvelope:
    trade_kwargs: dict = dict(
        symbol=symbol,
        action=action,
        confidence=confidence,
        thesis="t",
        news_bias="neutral",
        invalidate_if="x",
    )
    if action in {"BUY", "SELL"}:
        trade_kwargs.update(stop_loss_pct=0.012, take_profit_pct=0.024)
    return DecisionEnvelope(
        market_regime="mixed",
        summary="t",
        trades=[TradeDecision(**trade_kwargs)],
    )


def _acct(equity: float = 8_023.11) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo",
        currency="GBP",
        total_funds=equity,
        free_funds=equity,
        open_positions=[],
    )


def _base_guardrail(symbol: str) -> dict:
    return {
        "block_new_entries": False,
        "block_reasons": [],
        "close_requests": [],
        "econ_events": [],
        "earnings_events": {},
        "session_open": {symbol: True},
    }


def test_phase15_buy_decision_valid_without_stop_pct():
    """Phase 1.5: a BUY/SELL decision is now valid WITHOUT stop_loss_pct /
    take_profit_pct — the LLM no longer sets them; the executor owns the math."""
    td = TradeDecision(
        symbol="WTI", action="BUY", confidence=0.8, thesis="t",
        news_bias="positive", invalidate_if="x", signal_horizon="swing",
    )
    assert td.stop_loss_pct is None
    assert td.take_profit_pct is None
    assert td.signal_horizon == "swing"


def test_phase15_take_profit_from_risk_reward_ratio():
    """Phase 1.5: take_profit_distance = stop_distance × risk_reward_ratio,
    independent of any LLM-supplied pct ratio."""
    s = Settings(risk_per_trade_pct_base=0.005, risk_reward_ratio=2.0)
    rm = RiskManager(s)
    # SPY, scale=1.0, so stop_distance == unscaled ATR-based stop.
    plans = rm.build_execution_plans(
        decision=_decision("SPY", "BUY"),
        symbol_contexts={"SPY": _ctx("SPY", 540.0, atr=10.0)},
        account_snapshot=_acct(equity=10_000.0),
        last_trade_times={},
        guardrail_context=_base_guardrail("SPY"),
    )
    assert len(plans) == 1
    plan = plans[0]
    # TP must be ~2.0x the stop distance (the RR ratio), NOT the old
    # 0.024/0.012 = 2.0 LLM-ratio coincidence — verify by changing the ratio.
    assert plan.take_profit_distance == pytest.approx(plan.stop_distance * 2.0, rel=1e-6)


def test_phase15_risk_reward_ratio_is_honoured():
    """Changing risk_reward_ratio changes the TP distance proportionally,
    proving the executor (not the LLM) owns it."""
    ctx = {"SPY": _ctx("SPY", 540.0, atr=10.0)}
    acct = _acct(equity=10_000.0)
    gr = _base_guardrail("SPY")

    rm2 = RiskManager(Settings(risk_per_trade_pct_base=0.005, risk_reward_ratio=2.0))
    rm3 = RiskManager(Settings(risk_per_trade_pct_base=0.005, risk_reward_ratio=3.0))
    p2 = rm2.build_execution_plans(decision=_decision("SPY", "BUY"),
                                   symbol_contexts=ctx, account_snapshot=acct,
                                   last_trade_times={}, guardrail_context=gr)[0]
    p3 = rm3.build_execution_plans(decision=_decision("SPY", "BUY"),
                                   symbol_contexts=ctx, account_snapshot=acct,
                                   last_trade_times={}, guardrail_context=gr)[0]
    # Same stop, TP scales 2.0 -> 3.0.
    assert p2.stop_distance == pytest.approx(p3.stop_distance, rel=1e-6)
    assert p3.take_profit_distance == pytest.approx(p2.take_profit_distance * 1.5, rel=1e-6)


def test_qty_rederived_from_scaled_stop_brent():
    """BRENT (ig_price_scale=100): qty must be divided by scale.

    Replays the 2026-05-05 BRENT BUY conditions: equity ~£8 023, target
    cash risk ~£40 (0.5% of equity), price ~113, atr ~1.13, stop ~1.77%
    of price ≈ $2.01 unscaled → 201 IG points scaled.

    Pre-fix qty would be target_cash_risk / unscaled_stop = 40/2.01 ≈ 19.9,
    then capped by free funds / max_notional / round → previously
    materialised as 2.6367 in production. The cash-risk-at-stop on a
    scale=100 instrument was qty × scaled_stop × £1/pt = 2.6367 × 201 ≈ £530.

    Post-fix: qty must be divided by 100 → ≤ 0.05 (rounded to 4dp).
    Cash-risk-at-stop = 0.026 × 201 ≈ £5.30 (well under the £40 budget).
    """
    s = Settings(risk_per_trade_pct_base=0.005)  # 0.5% per-trade
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("BRENT", "BUY"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0, atr=1.13)},
        account_snapshot=_acct(equity=8_023.11),
        last_trade_times={},
        guardrail_context=_base_guardrail("BRENT"),
    )
    assert len(plans) == 1, "BRENT BUY should pass after re-derivation"
    plan = plans[0]
    # Stop should be IG-points-scaled (>100 because 1.77% of $113 × 100)
    assert plan.stop_distance > 100, (
        f"stop_distance should be in IG points (>100), got {plan.stop_distance}"
    )
    # Qty must be small (sub-unit) on a scale=100 instrument
    assert abs(plan.quantity) < 1.0, (
        f"qty must be re-derived to sub-unit on scale=100; got {plan.quantity}"
    )
    # Cash-risk-at-stop must be within tolerance of target
    cash_risk = abs(plan.quantity) * plan.stop_distance * 1.0  # point_value=1
    target_cash_risk = 8_023.11 * 0.005  # ≈ £40.12
    budget_ceiling = target_cash_risk * (1.0 + 0.25)  # default tolerance
    assert cash_risk <= budget_ceiling, (
        f"cash_risk={cash_risk:.2f} must be <= budget_ceiling={budget_ceiling:.2f} "
        f"(qty={plan.quantity}, scaled_stop={plan.stop_distance})"
    )


def test_qty_unchanged_on_scale_one_instruments():
    """Equity / FX paths with ig_price_scale=1.0 must be unaffected.

    SPY has scale=1.0 by default (not in the scaled instrument list).
    Confirms the re-derivation block is a no-op when scale==1.0.
    """
    s = Settings(risk_per_trade_pct_base=0.005)
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("SPY", "BUY"),
        symbol_contexts={"SPY": _ctx("SPY", 540.0, atr=10.0)},
        account_snapshot=_acct(equity=10_000.0),
        last_trade_times={},
        guardrail_context=_base_guardrail("SPY"),
    )
    # SPY has scale=1.0 → no re-derivation, qty stays as originally sized.
    # We don't assert exact qty (depends on internal floors) — just that a
    # plan emerged and qty is sensible (positive, non-trivial).
    assert len(plans) == 1
    assert plans[0].quantity > 0


def test_size_vs_budget_breach_rejection_when_pretrade_far_over_budget(monkeypatch, caplog):
    """Defence-in-depth: even if a future scaling/config bug slips qty
    past the re-derivation, the SIZE_VS_BUDGET_BREACH guard must reject
    plans whose cash_risk_at_stop exceeds budget × (1+tolerance).

    Force the breach by setting tolerance to 0 (any breach rejected) and
    point_value to a very high value so cash_risk is artificially
    inflated past target_cash_risk.
    """
    s = Settings(
        risk_per_trade_pct_base=0.005,
        risk_size_vs_budget_tolerance=0.0,  # zero tolerance
        point_value_per_unit=8575.0,         # Forces quantity=0.000051 -> rounds to 0.0001 -> ~2x cash risk
        # This test exercises the SIZE_VS_BUDGET_BREACH guard mechanism, which
        # is independent of the sizing policy. Pin the (legacy) graded ceiling
        # so the scenario stays calibrated after the production default was
        # flattened to 1.0 (2026-05 adversarial review).
        risk_confidence_scaling_max=2.5,
    )
    rm = RiskManager(s)
    with caplog.at_level(logging.WARNING, logger="trading_bot.risk"):
        plans = rm.build_execution_plans(
            decision=_decision("BRENT", "BUY"),
            symbol_contexts={"BRENT": _ctx("BRENT", 113.0, atr=1.13)},
            account_snapshot=_acct(equity=8_023.11),
            last_trade_times={},
            guardrail_context=_base_guardrail("BRENT"),
        )
    assert plans == [], "plan must be rejected by SIZE_VS_BUDGET_BREACH guard"
    assert any("SIZE_VS_BUDGET_BREACH" in rec.getMessage() for rec in caplog.records), (
        "expected SIZE_VS_BUDGET_BREACH WARN log; got: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_size_vs_budget_breach_silent_when_within_tolerance():
    """Plans within budget × (1 + tolerance) must NOT be rejected by the
    guard (only by other gates if applicable)."""
    s = Settings(
        risk_per_trade_pct_base=0.005,
        risk_size_vs_budget_tolerance=0.25,
        point_value_per_unit=1.0,
    )
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("BRENT", "BUY"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0, atr=1.13)},
        account_snapshot=_acct(equity=8_023.11),
        last_trade_times={},
        guardrail_context=_base_guardrail("BRENT"),
    )
    # Should pass (re-derivation already brings cash_risk well under budget)
    assert len(plans) == 1


# --- Per review 2026-05-05-adhoc-1 #1 follow-up: budget raised 0.5% -> 2.5% ---


def _default_settings(monkeypatch, **overrides) -> Settings:
    """Construct Settings asserting CODE defaults, isolated from any
    deployment .env and OS environ. `Settings` is a pydantic BaseSettings
    that loads `.env` (model_config env_file=".env") and the process
    environ, so a bare `Settings()` reads whatever the host .env declares
    (e.g. OCI sets RISK_PER_TRADE_PCT=0.005) rather than the code default.
    Tests that assert defaults must opt out of both sources.
    """
    for var in ("RISK_PER_TRADE_PCT", "RISK_PER_TRADE_PCT_BASE"):
        monkeypatch.delenv(var, raising=False)
    return Settings(_env_file=None, **overrides)


def test_default_risk_per_trade_budget_is_two_and_a_half_percent(monkeypatch):
    """Per the #1 follow-up: risk_per_trade_pct[_base] defaults are now
    0.025 (was 0.005). At ×100-scaled futures and ~£8k equity, the
    previous 0.5% budget produced sub-min_deal_size qty after the
    re-derivation, which the broker clamped to 1.0 and the new
    SIZE_VS_BUDGET_BREACH guard rejected. 2.5% restores tradability.

    Uses _default_settings so it asserts the CODE default and not the
    deployment .env (OCI .env sets RISK_PER_TRADE_PCT=0.005).
    """
    s = _default_settings(monkeypatch)
    assert s.risk_per_trade_pct == 0.025
    assert s.risk_per_trade_pct_base == 0.025


def test_brent_at_25pct_budget_produces_tradable_qty(monkeypatch):
    """At the new 2.5% default and ~£8k equity, BRENT (×100 scaled)
    should size to a qty that, after broker min-deal-size clamping,
    has cash_risk_at_stop within the 25% tolerance ceiling.

    Pre-fix (commit before d250617): qty=2.6549, cash_risk=£450 (~5.6%).
    Post-fix at 0.5% budget: qty=0.0265, sub-min_deal_size, broker
    clamps to 1.0, cash_risk=£169 → BREACH-rejected against £50 ceiling.
    Post-fix at 2.5% budget: qty=0.1325, still sub-min_deal_size, but
    the 1.0 clamp produces cash_risk=£169 against £250 ceiling → passes.
    """
    s = _default_settings(monkeypatch)  # default 2.5%, isolated from host .env
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("BRENT", "BUY"),
        symbol_contexts={"BRENT": _ctx("BRENT", 113.0, atr=1.13)},
        account_snapshot=_acct(equity=8_023.11),
        last_trade_times={},
        guardrail_context=_base_guardrail("BRENT"),
    )
    assert len(plans) == 1, "BRENT BUY should pass guard at 2.5% budget"
    plan = plans[0]
    target_cash_risk = 8_023.11 * 0.025  # £200.58
    budget_ceiling = target_cash_risk * 1.25  # £250.72
    cash_risk = abs(plan.quantity) * plan.stop_distance * 1.0
    assert cash_risk <= budget_ceiling, (
        f"cash_risk={cash_risk:.2f} must be <= ceiling={budget_ceiling:.2f}"
    )
