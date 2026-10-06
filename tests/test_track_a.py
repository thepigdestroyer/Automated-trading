"""Tests for Track-A: the deterministic signals-only twin strategy.

Track-A trades the mechanical daily-trend rules (Donchian breakout in the
direction of a strong daily trend) through the SAME cost-aware paper
simulator as the LLM track. Comparing the two forward-walk reports answers:
does the LLM regime-veto add value over the mechanical strategy, subtract
value, or do nothing?
"""

from __future__ import annotations

from datetime import datetime, timezone

from trading_bot.config import Settings
from trading_bot.models import DailyTrend, IndicatorSnapshot, SymbolContext
from trading_bot.paper_pnl import PaperPnL
from trading_bot.track_a import decide_track_a, run_track_a_cycle


def _trend(direction="up", breakout="up", strength=35.0) -> DailyTrend:
    return DailyTrend(
        direction=direction, donchian_breakout=breakout,
        sma_50_200_cross="golden" if direction == "up" else "death",
        trend_strength=strength, close_vs_sma50_bps=50.0, summary="t",
    )


def _ctx(symbol="GOLD", price=100.0, atr=2.0, trend: DailyTrend | None = None) -> SymbolContext:
    ind = IndicatorSnapshot(
        price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
        rsi_14=55.0, macd=0.1, macd_signal=0.05, atr_14=atr,
        volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
    )
    return SymbolContext(
        symbol=symbol, display_name=symbol, asset_class="commodity", as_of="t",
        indicators=ind, daily_trend=trend, headlines=[], chart_note="t",
    )


def _settings(**kw) -> Settings:
    defaults = dict(
        track_a_enabled=True, min_trend_strength=20.0, allow_shorts=True,
        atr_stop_multiplier=1.5, stop_loss_pct_floor=0.004,
        risk_reward_ratio=2.0, track_a_cooldown_hours=24.0,
    )
    defaults.update(kw)
    return Settings(**defaults)


# --- decide_track_a -----------------------------------------------------


def test_uptrend_breakout_long():
    sig = decide_track_a(_ctx(trend=_trend("up", "up")), _settings())
    assert sig is not None
    assert sig.side == "buy"
    # stop = max(1.5*2.0, 100*0.004, 0.01) = 3.0 ; tp = 6.0
    assert abs(sig.stop - 97.0) < 1e-9
    assert abs(sig.tp - 106.0) < 1e-9


def test_downtrend_breakdown_short():
    sig = decide_track_a(_ctx(trend=_trend("down", "down")), _settings())
    assert sig is not None
    assert sig.side == "sell"
    assert abs(sig.stop - 103.0) < 1e-9
    assert abs(sig.tp - 94.0) < 1e-9


def test_no_breakout_no_trade():
    assert decide_track_a(_ctx(trend=_trend("up", "none")), _settings()) is None


def test_breakout_against_trend_no_trade():
    assert decide_track_a(_ctx(trend=_trend("up", "down")), _settings()) is None


def test_weak_trend_no_trade():
    assert decide_track_a(_ctx(trend=_trend("up", "up", strength=10.0)), _settings()) is None


def test_sideways_no_trade():
    assert decide_track_a(_ctx(trend=_trend("sideways", "up")), _settings()) is None


def test_missing_daily_trend_no_trade():
    assert decide_track_a(_ctx(trend=None), _settings()) is None


def test_shorts_disabled_blocks_sell():
    s = _settings(allow_shorts=False)
    assert decide_track_a(_ctx(trend=_trend("down", "down")), s) is None


def test_zero_price_no_trade():
    assert decide_track_a(_ctx(price=0.0, trend=_trend("up", "up")), _settings()) is None


# --- run_track_a_cycle --------------------------------------------------


def _paper(tmp_path) -> PaperPnL:
    return PaperPnL(tmp_path / "track_a.json", bar_fetcher=lambda s, o: None)


def test_cycle_records_entry_with_costs_and_id(tmp_path):
    p = _paper(tmp_path)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    opened = run_track_a_cycle(
        {"GOLD": _ctx(trend=_trend("up", "up"))}, p, _settings(), now=now,
    )
    assert len(opened) == 1
    item = p.state["open"][0]
    assert item["decision_id"].startswith("trackA:")
    assert item["symbol"] == "GOLD"
    assert item["spread_bps"] > 0  # instrument costs carried through
    assert item["max_holding_hours"] > 0


def test_cycle_skips_symbol_with_open_position(tmp_path):
    p = _paper(tmp_path)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    ctxs = {"GOLD": _ctx(trend=_trend("up", "up"))}
    run_track_a_cycle(ctxs, p, _settings(), now=now)
    opened2 = run_track_a_cycle(ctxs, p, _settings(), now=now)
    assert opened2 == []
    assert len(p.state["open"]) == 1


def test_cooldown_blocks_immediate_reentry_after_close(tmp_path):
    p = _paper(tmp_path)
    s = _settings(track_a_cooldown_hours=24.0)
    t0 = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    ctxs = {"GOLD": _ctx(trend=_trend("up", "up"))}
    run_track_a_cycle(ctxs, p, s, now=t0)
    p.state["open"] = []  # simulate the position closing
    # 1h later: cooldown active -> no re-entry
    opened = run_track_a_cycle(ctxs, p, s, now=datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc))
    assert opened == []
    # 25h later: cooldown expired -> re-entry allowed
    opened = run_track_a_cycle(ctxs, p, s, now=datetime(2026, 6, 11, 13, 30, tzinfo=timezone.utc))
    assert len(opened) == 1


def test_cycle_no_signal_records_nothing(tmp_path):
    p = _paper(tmp_path)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    opened = run_track_a_cycle(
        {"GOLD": _ctx(trend=_trend("up", "none"))}, p, _settings(), now=now,
    )
    assert opened == [] and p.state["open"] == []


def test_cycle_survives_bad_context(tmp_path):
    # A context that raises inside decide must not kill the cycle.
    p = _paper(tmp_path)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    opened = run_track_a_cycle(
        {"BAD": object(), "GOLD": _ctx(trend=_trend("up", "up"))},  # type: ignore[dict-item]
        p, _settings(), now=now,
    )
    assert len(opened) == 1  # GOLD still trades


def test_entries_are_tagged_with_the_llm_universe(tmp_path):
    """Track-A trades the unfiltered universe; the LLM track does not. Each
    entry records whether the LLM track could also trade that symbol this
    cycle, so forward_eval can compare like with like."""
    p = _paper(tmp_path)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    ctxs = {
        "GOLD": _ctx("GOLD", trend=_trend("up", "up")),
        "BRENT": _ctx("BRENT", trend=_trend("up", "up")),
    }

    run_track_a_cycle(ctxs, p, _settings(), now=now, llm_universe={"GOLD"}, cycle_id="c_x")

    rows = {row["symbol"]: row for row in p.state["open"]}
    assert rows["GOLD"]["in_llm_universe"] is True
    assert rows["BRENT"]["in_llm_universe"] is False
    assert rows["GOLD"]["cycle_id"] == "c_x"


def test_entries_are_untagged_when_the_llm_universe_is_unknown(tmp_path):
    p = _paper(tmp_path)
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

    run_track_a_cycle({"GOLD": _ctx(trend=_trend("up", "up"))}, p, _settings(), now=now)

    assert "in_llm_universe" not in p.state["open"][0]
