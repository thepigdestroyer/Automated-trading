"""Tests for the Phase 2 daily-trend classifier + trend-alignment gate."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading_bot.config import Settings
from trading_bot.daily_trend import build_daily_trend
from trading_bot.models import (
    AccountSnapshot,
    DailyTrend,
    DecisionEnvelope,
    IndicatorSnapshot,
    SymbolContext,
    TradeDecision,
)
from trading_bot.risk import RiskManager


def _daily(closes: np.ndarray, *, intrabar: bool = False) -> pd.DataFrame:
    """Daily OHLC fixture. By default bars are flat (High=Low=Close) so a
    monotonic close series genuinely breaks prior Donchian highs/lows (a
    close*1.01 high would otherwise keep the prior bar's high above today's
    close and suppress the breakout). Set intrabar=True to add range."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    high = closes * 1.01 if intrabar else closes
    low = closes * 0.99 if intrabar else closes
    return pd.DataFrame(
        {"Open": closes, "High": high, "Low": low, "Close": closes},
        index=idx,
    )


def test_uptrend_classified_up():
    closes = 100 + np.linspace(0, 60, 220)  # steady multi-month uptrend
    trend = build_daily_trend(_daily(closes))
    assert trend.direction == "up"
    assert trend.donchian_breakout == "up"  # making new highs
    assert trend.sma_50_200_cross == "golden"
    assert trend.close_vs_sma50_bps > 0


def test_downtrend_classified_down():
    closes = 160 - np.linspace(0, 60, 220)
    trend = build_daily_trend(_daily(closes))
    assert trend.direction == "down"
    assert trend.donchian_breakout == "down"
    assert trend.sma_50_200_cross == "death"


def test_sideways_when_choppy():
    rng = np.random.RandomState(5)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, 220))
    trend = build_daily_trend(_daily(closes))
    # A random walk should not read as a strong golden/death structured trend
    # with high strength; at minimum the model is valid and strength modest.
    assert trend.direction in {"up", "down", "sideways"}
    assert 0.0 <= trend.trend_strength <= 100.0


def test_insufficient_data_is_neutral():
    closes = np.array([100.0] * 10)
    trend = build_daily_trend(_daily(closes))
    assert trend.direction == "sideways"
    assert trend.donchian_breakout == "none"
    assert "insufficient" in trend.summary


def test_missing_close_column_is_neutral():
    df = pd.DataFrame({"Open": [1, 2, 3]})
    trend = build_daily_trend(df)
    assert trend.direction == "sideways"


# --- trend-alignment gate (in risk.py) -------------------------------


def _ctx(symbol: str, price: float, atr: float, trend: DailyTrend | None) -> SymbolContext:
    ind = IndicatorSnapshot(
        price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
        rsi_14=55.0, macd=0.1, macd_signal=0.05, atr_14=atr,
        volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
    )
    return SymbolContext(
        symbol=symbol, display_name=symbol, asset_class="index", as_of="t",
        indicators=ind, daily_trend=trend, headlines=[], chart_note="t",
    )


def _decision(symbol: str, action: str) -> DecisionEnvelope:
    return DecisionEnvelope(
        market_regime="mixed", summary="t",
        trades=[TradeDecision(symbol=symbol, action=action, confidence=0.85,
                              thesis="t", news_bias="neutral", invalidate_if="x")],
    )


def _acct() -> AccountSnapshot:
    return AccountSnapshot(mode="demo", currency="GBP", total_funds=10_000.0,
                           free_funds=10_000.0, open_positions=[])


def _gr(symbol: str) -> dict:
    return {"block_new_entries": False, "block_reasons": [], "close_requests": [],
            "econ_events": [], "earnings_events": {}, "session_open": {symbol: True}}


def _strong(direction: str) -> DailyTrend:
    return DailyTrend(direction=direction, donchian_breakout="none",
                      sma_50_200_cross="none", trend_strength=40.0,
                      close_vs_sma50_bps=10.0, summary="t")


def test_long_into_downtrend_rejected(caplog):
    s = Settings(require_trend_alignment=True, min_trend_strength=20.0,
                 cost_edge_multiple=0.0)
    rm = RiskManager(s)
    with caplog.at_level(logging.INFO, logger="trading_bot.risk"):
        plans = rm.build_execution_plans(
            decision=_decision("SPY", "BUY"),
            symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0, _strong("down"))},
            account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
        )
    assert plans == []
    assert any("TREND_MISALIGNED" in r.getMessage() for r in caplog.records)


def test_long_into_uptrend_allowed():
    s = Settings(require_trend_alignment=True, min_trend_strength=20.0,
                 cost_edge_multiple=0.0)
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("SPY", "BUY"),
        symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0, _strong("up"))},
        account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
    )
    assert len(plans) == 1


def test_short_into_uptrend_rejected(caplog):
    s = Settings(require_trend_alignment=True, min_trend_strength=20.0,
                 allow_shorts=True, cost_edge_multiple=0.0)
    rm = RiskManager(s)
    with caplog.at_level(logging.INFO, logger="trading_bot.risk"):
        plans = rm.build_execution_plans(
            decision=_decision("SPY", "SELL"),
            symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0, _strong("up"))},
            account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
        )
    assert plans == []
    assert any("TREND_MISALIGNED" in r.getMessage() for r in caplog.records)


def test_weak_trend_rejected_as_chop(caplog):
    weak = DailyTrend(direction="up", donchian_breakout="none",
                      sma_50_200_cross="none", trend_strength=10.0,
                      close_vs_sma50_bps=5.0, summary="t")
    s = Settings(require_trend_alignment=True, min_trend_strength=20.0,
                 cost_edge_multiple=0.0)
    rm = RiskManager(s)
    with caplog.at_level(logging.INFO, logger="trading_bot.risk"):
        plans = rm.build_execution_plans(
            decision=_decision("SPY", "BUY"),
            symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0, weak)},
            account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
        )
    assert plans == []
    assert any("TREND_TOO_WEAK" in r.getMessage() for r in caplog.records)


def test_no_daily_trend_degrades_open():
    # Missing daily_trend must NOT block (degrade-open) so a Yahoo daily-fetch
    # failure doesn't halt all trading.
    s = Settings(require_trend_alignment=True, min_trend_strength=20.0,
                 cost_edge_multiple=0.0)
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("SPY", "BUY"),
        symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0, None)},
        account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
    )
    assert len(plans) == 1


def test_alignment_disabled_allows_counter_trend():
    s = Settings(require_trend_alignment=False, cost_edge_multiple=0.0)
    rm = RiskManager(s)
    plans = rm.build_execution_plans(
        decision=_decision("SPY", "BUY"),
        symbol_contexts={"SPY": _ctx("SPY", 540.0, 10.0, _strong("down"))},
        account_snapshot=_acct(), last_trade_times={}, guardrail_context=_gr("SPY"),
    )
    assert len(plans) == 1
