"""Backtest must reproduce the Track-A rule with costs and no lookahead."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from trading_bot.config import Settings


def _trending_up(n: int = 400) -> pd.DataFrame:
    # The task brief's fixture had High = Close * 1.01. Under the real Donchian
    # rule (close >= 0.999 x prior 20-bar high) a 0.25%/bar ramp whose highs
    # sit 1% above the close can never break out, so no trade is possible.
    # Highs at 1.001 x close keep the "clean uptrend" intent with breakouts.
    close = np.linspace(100.0, 200.0, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.001,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_uptrend_generates_long_trades():
    from trading_bot.backtest import simulate_symbol

    trades = simulate_symbol(
        _trending_up(), "WTI", _settings(), spread_bps=5.0, slippage_bps=5.0
    )
    assert trades, "a clean uptrend must produce at least one long entry"
    assert all(t.side == "buy" for t in trades)


def test_no_lookahead_entry_uses_next_bar_open():
    """A signal on bar t must fill on bar t+1, never on bar t's close."""
    from trading_bot.backtest import simulate_symbol

    daily = _trending_up()
    trades = simulate_symbol(
        daily, "WTI", _settings(), spread_bps=0.0, slippage_bps=0.0
    )
    first = trades[0]
    entry_idx = daily.index.get_loc(first.entry_at)
    assert entry_idx >= 1
    assert abs(first.entry - float(daily["Open"].iloc[entry_idx])) < 1e-6


def test_costs_reduce_expectancy():
    from trading_bot.backtest import simulate_symbol

    daily = _trending_up()
    free = simulate_symbol(daily, "WTI", _settings(), spread_bps=0.0, slippage_bps=0.0)
    costly = simulate_symbol(daily, "WTI", _settings(), spread_bps=50.0, slippage_bps=50.0)
    assert sum(t.pnl_pct for t in costly) < sum(t.pnl_pct for t in free)


def test_run_backtest_reports_wilson_ci_and_trade_count():
    from trading_bot.backtest import run_backtest

    report = run_backtest({"WTI": _trending_up()}, _settings())
    assert report["trades"] >= 1
    assert "win_rate_ci" in report
    lo, hi = report["win_rate_ci"]
    assert 0.0 <= lo <= hi <= 1.0


# --- controlled single-signal scenarios ----------------------------------
#
# A steady ramp whose highs sit 1% above the close is a daily uptrend (trend
# 'up', strength 100) that never breaks out. One bar then closes 3% higher:
# the only Donchian breakout, so the only signal, is on bar K. The bars after
# it are hand-built around the entry price E and stop distance D, so each
# exit rule can be exercised exactly.

_N_PREFIX = 260
K = _N_PREFIX  # index of the signal bar


def _base(short: bool = False) -> pd.DataFrame:
    sign = -1.0 if short else 1.0
    close = 100.0 + sign * np.linspace(0.0, 30.0, _N_PREFIX)
    high = close * (1.001 if short else 1.01)
    low = close * (0.99 if short else 0.999)
    last = close[-1]
    sig_close = last * (0.97 if short else 1.03)
    rows = list(zip(close, high, low, close))
    # The signal bar's extreme sits 0.5% beyond its close, so later bars
    # closing at E do not re-break the channel (the tolerance is 0.1%).
    if short:
        rows.append((last, last * 1.005, sig_close * 0.995, sig_close))
    else:
        rows.append((last, sig_close * 1.005, last * 0.995, sig_close))
    return _frame(rows)


def _frame(rows) -> pd.DataFrame:
    arr = np.asarray(rows, dtype=float)
    idx = pd.date_range("2023-01-01", periods=len(arr), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "Open": arr[:, 0],
            "High": arr[:, 1],
            "Low": arr[:, 2],
            "Close": arr[:, 3],
            "Volume": np.full(len(arr), 1000.0),
        },
        index=idx,
    )


def _atr_at_signal(base: pd.DataFrame) -> float:
    from trading_bot.indicators import _atr

    return float(_atr(base).iloc[K])


def _levels(short: bool = False, entry_open: float | None = None, ratio: float | None = None):
    """(base frame, entry mid E, stop distance D) under default settings."""
    base = _base(short)
    s = _settings()
    e = float(base["Close"].iloc[K]) if entry_open is None else entry_open
    atr = _atr_at_signal(base)
    if ratio is not None:
        atr = atr / ratio
    d = max(atr * s.atr_stop_multiplier, e * s.stop_loss_pct_floor, 0.01)
    return base, e, d


def _with_post(base: pd.DataFrame, post) -> pd.DataFrame:
    rows = list(base[["Open", "High", "Low", "Close"]].itertuples(index=False, name=None))
    return _frame(rows + list(post))


def _benign(e: float, d: float, n: int):
    return [(e, e + 0.1 * d, e - 0.1 * d, e)] * n


def _sim(daily, **kw):
    from trading_bot.backtest import simulate_symbol

    kw.setdefault("spread_bps", 0.0)
    kw.setdefault("slippage_bps", 0.0)
    kw.setdefault("swap_annualised_bps", 0.0)
    return simulate_symbol(daily, "WTI", _settings(), **kw)


def test_fixture_has_exactly_one_signal_on_bar_k():
    """Guard the scenarios below: if the fixture stops signalling only on K,
    every exit test would be testing something else."""
    from trading_bot.backtest import entry_signals
    from trading_bot.daily_trend import compute_trend_frame

    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 2))
    sig = entry_signals(compute_trend_frame(daily), _settings(), allow_shorts=True)
    assert list(np.flatnonzero(sig != 0)) == [K]
    assert sig[K] == 1


def test_signal_on_bar_t_fills_at_open_of_bar_t_plus_1():
    base, _, _ = _levels()
    gap_open = float(base["Close"].iloc[K]) * 0.998  # distinct from bar K's close
    _, e, d = _levels(entry_open=gap_open)
    daily = _with_post(base, [(e, e + 0.1 * d, e - 0.1 * d, e * 1.0005)] + _benign(e, d, 4))
    trades = _sim(daily)
    first = trades[0]
    assert first.entry_at == daily.index[K + 1]
    assert first.entry == pytest.approx(gap_open, abs=1e-9)
    assert first.entry != pytest.approx(float(daily["Close"].iloc[K]))


def test_stop_and_tp_follow_track_a_formula_from_daily_atr():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 5))
    first = _sim(daily)[0]
    s = _settings()
    assert first.stop == pytest.approx(e - d, rel=1e-12)
    assert first.tp == pytest.approx(e + d * s.risk_reward_ratio, rel=1e-12)


def test_bar_touching_both_stop_and_target_records_the_stop():
    base, e, d = _levels()
    daily = _with_post(base, [(e, e + 2 * d + 1.0, e - d - 1.0, e)])
    first = _sim(daily)[0]
    assert first.reason == "stop"
    assert first.exit == pytest.approx(e - d, rel=1e-12)
    assert first.exit_at == daily.index[K + 1]
    assert first.r_multiple == pytest.approx(-1.0, abs=1e-9)


def test_intrabar_stop_fills_at_the_stop_level():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 1) + [(e, e + 0.2 * d, e - 1.5 * d, e - 1.2 * d)])
    first = _sim(daily)[0]
    assert first.reason == "stop"
    assert first.exit == pytest.approx(e - d, rel=1e-12)
    assert first.exit_at == daily.index[K + 2]
    assert first.gap is False


def test_intrabar_target_fills_at_the_target_level():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 1) + [(e, e + 2.5 * d, e - 0.2 * d, e + 2.2 * d)])
    first = _sim(daily)[0]
    assert first.reason == "tp"
    assert first.exit == pytest.approx(e + 2 * d, rel=1e-12)


def test_gap_through_stop_fills_at_the_open_not_the_stop():
    base, e, d = _levels()
    gap = e - 3.0 * d
    daily = _with_post(base, _benign(e, d, 1) + [(gap, gap + 0.1 * d, gap - 0.1 * d, gap)])
    first = _sim(daily)[0]
    assert first.reason == "stop"
    assert first.exit == pytest.approx(gap, rel=1e-12)
    assert first.exit < first.stop
    assert first.gap is True
    assert first.r_multiple == pytest.approx(-3.0, abs=1e-9)


def test_gap_through_target_fills_at_the_open():
    base, e, d = _levels()
    gap = e + 3.0 * d
    daily = _with_post(base, _benign(e, d, 1) + [(gap, gap + 0.1 * d, gap - 0.1 * d, gap)])
    first = _sim(daily)[0]
    assert first.reason == "tp"
    assert first.exit == pytest.approx(gap, rel=1e-12)
    assert first.gap is True


def test_short_gap_through_stop_fills_at_the_open():
    base, e, d = _levels(short=True)
    gap = e + 3.0 * d
    daily = _with_post(base, _benign(e, d, 1) + [(gap, gap + 0.1 * d, gap - 0.1 * d, gap)])
    first = _sim(daily, allow_shorts=True)[0]
    assert first.side == "sell"
    assert first.stop == pytest.approx(e + d, rel=1e-12)
    assert first.reason == "stop"
    assert first.exit == pytest.approx(gap, rel=1e-12)
    assert first.r_multiple == pytest.approx(-3.0, abs=1e-9)


def test_short_bar_touching_both_records_the_stop():
    base, e, d = _levels(short=True)
    daily = _with_post(base, [(e, e + d + 1.0, e - 2 * d - 1.0, e)])
    first = _sim(daily, allow_shorts=True)[0]
    assert first.reason == "stop"
    assert first.exit == pytest.approx(e + d, rel=1e-12)


def test_time_stop_closes_at_first_open_at_or_after_max_hold():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 6))
    first = _sim(daily)[0]
    assert _settings().default_max_holding_hours == 72
    # Daily bars: entry at bar K+1's open, 72h later is bar K+4's open.
    assert first.reason == "time"
    assert first.exit_at == daily.index[K + 4]
    assert first.exit == pytest.approx(float(daily["Open"].iloc[K + 4]))
    assert first.hold_hours == pytest.approx(72.0)


def test_end_of_data_closes_at_last_close():
    base, e, d = _levels()
    daily = _with_post(base, [(e, e + 0.1 * d, e - 0.1 * d, e + 0.05 * d)])
    first = _sim(daily)[0]
    assert first.reason == "end_of_data"
    assert first.exit == pytest.approx(e + 0.05 * d)
    assert first.exit_at == daily.index[-1]


def test_costs_mirror_paper_adverse_fill_on_both_legs_plus_swap():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 6))
    t = _sim(daily, spread_bps=10.0, slippage_bps=5.0, swap_annualised_bps=200.0)[0]
    adj = (10.0 / 2.0 + 5.0) / 10_000.0
    fill_entry = e * (1 + adj)
    fill_exit = e * (1 - adj)  # time exit at an open equal to E
    financing = fill_entry * (200.0 / 10_000.0) * (72.0 / 8766.0)
    pnl_units = (fill_exit - fill_entry) - financing
    assert t.entry == pytest.approx(fill_entry, rel=1e-12)
    assert t.exit == pytest.approx(fill_exit, rel=1e-12)
    assert t.pnl_pct == pytest.approx(pnl_units / fill_entry, rel=1e-9)
    assert t.r_multiple == pytest.approx(pnl_units / d, rel=1e-9)
    # Stop/TP levels sit around the MID entry, as in the live executor.
    assert t.stop == pytest.approx(e - d, rel=1e-12)


def test_default_swap_comes_from_settings():
    from trading_bot.backtest import simulate_symbol

    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 6))
    implicit = simulate_symbol(daily, "WTI", _settings(), spread_bps=0.0, slippage_bps=0.0)[0]
    explicit = _sim(daily, swap_annualised_bps=float(_settings().swap_annualised_bps_default))[0]
    free = _sim(daily, swap_annualised_bps=0.0)[0]
    assert implicit.pnl_pct == pytest.approx(explicit.pnl_pct, rel=1e-12)
    assert implicit.pnl_pct < free.pnl_pct


def test_live_hourly_equiv_sizes_stop_from_daily_atr_over_ratio():
    base, e, d_daily = _levels()
    _, _, d_live = _levels(ratio=2.0)
    assert d_live < d_daily  # the scenario must actually distinguish the modes
    daily = _with_post(base, _benign(e, d_daily, 6))
    daily_trade = _sim(daily, stop_mode="daily_atr")[0]
    live_trade = _sim(daily, stop_mode="live_hourly_equiv", hourly_ratio={"WTI": 2.0})[0]
    assert daily_trade.stop == pytest.approx(e - d_daily, rel=1e-12)
    assert live_trade.stop == pytest.approx(e - d_live, rel=1e-12)
    assert live_trade.tp == pytest.approx(e + 2 * d_live, rel=1e-12)


def test_live_hourly_equiv_still_honours_pct_floor():
    base, e, _ = _levels()
    daily = _with_post(base, _benign(e, 1.0, 6))
    t = _sim(daily, stop_mode="live_hourly_equiv", hourly_ratio={"WTI": 1000.0})[0]
    assert t.stop == pytest.approx(e - e * _settings().stop_loss_pct_floor, rel=1e-12)


def test_live_hourly_equiv_requires_a_ratio_for_the_symbol():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 3))
    with pytest.raises(ValueError):
        _sim(daily, stop_mode="live_hourly_equiv", hourly_ratio={"GOLD": 2.0})
    with pytest.raises(ValueError):
        _sim(daily, stop_mode="live_hourly_equiv")
    with pytest.raises(ValueError):
        _sim(daily, stop_mode="nonsense")


def test_shorts_off_by_default_and_on_when_allowed():
    base, e, d = _levels(short=True)
    daily = _with_post(base, _benign(e, d, 6))
    assert _sim(daily) == []
    shorts = _sim(daily, allow_shorts=True)
    assert shorts and shorts[0].side == "sell"


def test_shorts_follow_paper_allow_shorts_setting():
    from trading_bot.backtest import simulate_symbol

    base, e, d = _levels(short=True)
    daily = _with_post(base, _benign(e, d, 6))
    # The setting lands in a later task; model_copy(update=) attaches it now.
    settings = _settings().model_copy(update={"paper_allow_shorts": True})
    assert getattr(settings, "paper_allow_shorts", False) is True
    trades = simulate_symbol(daily, "WTI", settings, spread_bps=0.0, slippage_bps=0.0)
    assert trades and trades[0].side == "sell"


def test_one_position_at_a_time_and_cooldown_between_entries():
    from trading_bot.backtest import simulate_symbol

    daily = _trending_up()
    trades = simulate_symbol(daily, "WTI", _settings(), spread_bps=0.0, slippage_bps=0.0)
    assert len(trades) >= 5
    for prev, nxt in zip(trades, trades[1:]):
        assert nxt.entry_at > prev.exit_at

    slow = _settings().model_copy(update={"track_a_cooldown_hours": 24.0 * 10})
    spaced = simulate_symbol(daily, "WTI", slow, spread_bps=0.0, slippage_bps=0.0)
    assert spaced and len(spaced) < len(trades)
    for prev, nxt in zip(spaced, spaced[1:]):
        assert (nxt.entry_at - prev.entry_at) >= timedelta(hours=240)


def test_start_ignores_signals_before_the_window():
    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 6))
    assert _sim(daily, start=daily.index[K])
    assert _sim(daily, start=daily.index[K + 1]) == []


# --- still-forming bar ---------------------------------------------------


def test_drop_unsettled_bar_removes_todays_bar_only():
    from trading_bot.backtest import drop_unsettled_bar

    daily = _trending_up(60)
    last = daily.index[-1]
    assert len(drop_unsettled_bar(daily, now=last + timedelta(hours=5))) == len(daily) - 1
    assert len(drop_unsettled_bar(daily, now=last + timedelta(days=1, minutes=1))) == len(daily)


def test_drop_unsettled_bar_uses_the_exchange_date():
    from trading_bot.backtest import drop_unsettled_bar

    daily = _trending_up(60)
    daily.index = daily.index.tz_localize(None).tz_localize("America/New_York")
    last = daily.index[-1]
    # 01:00 UTC next calendar day is still 21:00 New York on the bar's date.
    now_utc = (last + timedelta(hours=21)).tz_convert("UTC")
    assert now_utc.date() > last.date()
    assert len(drop_unsettled_bar(daily, now=now_utc)) == len(daily) - 1


def test_simulation_never_uses_a_still_forming_bar():
    base, e, d = _levels()
    # Bar K+2 is "today": its partial range has already crossed the stop.
    daily = _with_post(base, _benign(e, d, 1) + [(e, e + 0.1 * d, e - 2 * d, e - 1.5 * d)])
    today = daily.index[-1]
    settled = _sim(daily, now=today + timedelta(days=2))[0]
    assert settled.reason == "stop" and settled.exit_at == today
    forming = _sim(daily, now=today + timedelta(hours=10))[0]
    assert forming.reason == "end_of_data"
    assert forming.exit_at == daily.index[-2]


# --- rule parity with the live Track-A decision --------------------------


def _random_walk(seed: int, n: int = 420) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, 0.012, n) + 0.004 * np.sin(np.arange(n) / 40.0)
    close = 100.0 * np.exp(np.cumsum(rets))
    opens = np.r_[close[0], close[:-1]]
    spread = np.abs(rng.normal(0.0, 0.008, n)) + 0.002
    high = np.maximum(opens, close) * (1 + spread)
    low = np.minimum(opens, close) * (1 - spread)
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"Open": opens, "High": high, "Low": low, "Close": close, "Volume": 1000.0}, index=idx
    )


def test_entry_signals_match_decide_track_a_on_every_prefix():
    from trading_bot.backtest import entry_signals
    from trading_bot.daily_trend import build_daily_trend, compute_trend_frame
    from trading_bot.models import IndicatorSnapshot, SymbolContext
    from trading_bot.track_a import decide_track_a

    live_settings = Settings(_env_file=None, allow_shorts=True)
    seen = {1: 0, -1: 0}
    for seed in (3, 11):
        daily = _random_walk(seed)
        sig = entry_signals(compute_trend_frame(daily), live_settings, allow_shorts=True)
        for t in range(len(daily)):
            price = float(daily["Close"].iloc[t])
            ind = IndicatorSnapshot(
                price=price, sma_20=price, sma_50=price, ema_12=price, ema_26=price,
                rsi_14=50.0, macd=0.0, macd_signal=0.0, atr_14=1.0,
                volatility_20d=0.2, return_1d=0.0, return_5d=0.0,
            )
            ctx = SymbolContext(
                symbol="WTI", display_name="WTI", asset_class="energy", as_of="t",
                indicators=ind, daily_trend=build_daily_trend(daily.iloc[: t + 1]),
                headlines=[], chart_note="t",
            )
            live = decide_track_a(ctx, live_settings)
            expected = 0 if live is None else (1 if live.side == "buy" else -1)
            assert sig[t] == expected, (seed, t)
            if expected:
                seen[expected] += 1
    assert seen[1] > 0 and seen[-1] > 0, seen


# --- hourly ratio --------------------------------------------------------


def test_measure_hourly_ratio_recovers_known_ratio():
    from trading_bot.backtest import measure_hourly_ratio

    days = pd.date_range("2025-01-01", periods=60, freq="D", tz="UTC")
    daily = pd.DataFrame(
        {"Open": 100.0, "High": 104.0, "Low": 96.0, "Close": 100.0}, index=days
    )
    hours = pd.date_range("2025-01-01", periods=60 * 24, freq="h", tz="UTC")
    hourly = pd.DataFrame(
        {"Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0}, index=hours
    )
    assert measure_hourly_ratio(daily, hourly) == pytest.approx(8.0)


def test_measure_hourly_ratio_none_without_overlap():
    from trading_bot.backtest import measure_hourly_ratio

    days = pd.date_range("2020-01-01", periods=60, freq="D", tz="UTC")
    daily = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.0}, index=days)
    hours = pd.date_range("2025-01-01", periods=200, freq="h", tz="UTC")
    hourly = pd.DataFrame({"Open": 1.0, "High": 1.1, "Low": 0.9, "Close": 1.0}, index=hours)
    assert measure_hourly_ratio(daily, hourly) is None


# --- aggregation -----------------------------------------------------------


def test_run_backtest_uses_instrument_spread_and_settings_slippage():
    from trading_bot.backtest import run_backtest, simulate_symbol
    from trading_bot.instruments import get_instrument

    daily = _trending_up()
    s = _settings()
    report = run_backtest({"WTI": daily}, s)
    trades = simulate_symbol(
        daily, "WTI", s,
        spread_bps=float(get_instrument("WTI").max_spread_bps),
        slippage_bps=float(s.slippage_buffer_bps),
    )
    assert report["trades"] == len(trades)
    assert report["expectancy_pct"] == pytest.approx(
        sum(t.pnl_pct for t in trades) / len(trades), rel=1e-9
    )
    assert report["expectancy_r"] == pytest.approx(
        sum(t.r_multiple for t in trades) / len(trades), rel=1e-9
    )


def test_run_backtest_report_shape_and_stats():
    from trading_bot.backtest import run_backtest
    from trading_bot.forward_eval import bootstrap_expectancy_ci, wilson_ci

    sink: list = []
    report = run_backtest(
        {"WTI": _trending_up(), "GOLD": _trending_up()}, _settings(), trades_out=sink
    )
    for key in ("trades", "wins", "win_rate", "win_rate_ci", "expectancy_pct",
                "expectancy_ci", "expectancy_r", "expectancy_r_ci", "by_symbol",
                "stop_mode", "skipped", "exit_reasons"):
        assert key in report
    assert set(report["by_symbol"]) == {"WTI", "GOLD"}
    n, wins = report["trades"], report["wins"]
    assert report["win_rate_ci"] == pytest.approx(list(wilson_ci(wins, n)), abs=1e-4)
    assert sum(v["trades"] for v in report["by_symbol"].values()) == n
    assert sum(report["exit_reasons"].values()) == n
    assert len(sink) == n
    # Same estimators as the forward-walk report, on the same trades.
    _, r_lo, r_hi = bootstrap_expectancy_ci([t.r_multiple for t in sink])
    _, p_lo, p_hi = bootstrap_expectancy_ci([t.pnl_pct for t in sink])
    assert report["expectancy_r_ci"] == pytest.approx([r_lo, r_hi])
    assert report["expectancy_ci"] == pytest.approx([p_lo, p_hi])
    assert r_lo <= report["expectancy_r"] <= r_hi


def test_run_backtest_live_mode_skips_symbols_without_ratio():
    from trading_bot.backtest import run_backtest

    report = run_backtest(
        {"WTI": _trending_up(), "GOLD": _trending_up()},
        _settings(),
        stop_mode="live_hourly_equiv",
        hourly_ratio={"WTI": 4.0},
    )
    assert report["stop_mode"] == "live_hourly_equiv"
    assert "GOLD" in report["skipped"]
    assert set(report["by_symbol"]) == {"WTI"}


def test_run_backtest_stop_mode_changes_the_result():
    from trading_bot.backtest import run_backtest

    frames = {"WTI": _trending_up()}
    a = run_backtest(frames, _settings(), stop_mode="daily_atr")
    b = run_backtest(frames, _settings(), stop_mode="live_hourly_equiv", hourly_ratio={"WTI": 8.0})
    assert a["avg_stop_pct"] > b["avg_stop_pct"]


# --- reporting honesty: cost scenarios, clustered CI, stop-first bound ----


def test_cost_inputs_per_scenario():
    from trading_bot.backtest import TYPICAL_SLIPPAGE_BPS, TYPICAL_SPREAD_BPS, cost_inputs
    from trading_bot.instruments import get_instrument, list_supported_instruments

    s = _settings()
    swap = float(s.swap_annualised_bps_default)
    assert cost_inputs("WTI", s, "max") == (
        float(get_instrument("WTI").max_spread_bps), float(s.slippage_buffer_bps), swap
    )
    assert cost_inputs("WTI", s, "typical") == (
        TYPICAL_SPREAD_BPS["WTI"], TYPICAL_SLIPPAGE_BPS, swap
    )
    assert cost_inputs("WTI", s, "zero") == (0.0, 0.0, 0.0)
    # Every instrument has a typical spread, and it sits below live's cap.
    for inst in list_supported_instruments():
        spread, _, _ = cost_inputs(inst.symbol, s, "typical")
        assert 0.0 < spread <= inst.max_spread_bps, inst.symbol
    with pytest.raises(ValueError):
        cost_inputs("WTI", s, "cheap")


def test_zero_costs_make_net_equal_gross():
    from trading_bot.backtest import run_backtest

    sink: list = []
    report = run_backtest({"WTI": _trending_up()}, _settings(), costs="zero", trades_out=sink)
    assert report["costs"] == "zero"
    assert sink
    for t in sink:
        assert t.r_multiple == pytest.approx(t.gross_r, abs=1e-12)
        assert t.entry == t.entry_mid and t.exit == t.exit_mid
    assert report["expectancy_r"] == pytest.approx(report["expectancy_r_gross"], abs=1e-12)


def test_cost_scenarios_change_only_costs():
    """Same trades, same gross R; net R falls as costs rise: zero > typical > max."""
    from trading_bot.backtest import cost_inputs, run_backtest, simulate_symbol

    daily = _trending_up()
    s = _settings()
    runs = {}
    for costs in ("zero", "typical", "max"):
        sink: list = []
        runs[costs] = (run_backtest({"WTI": daily}, s, costs=costs, trades_out=sink), sink)
    trades = {k: v[1] for k, v in runs.items()}
    keys = [(t.entry_at, t.exit_at, t.reason, t.gross_r) for t in trades["zero"]]
    assert [(t.entry_at, t.exit_at, t.reason, t.gross_r) for t in trades["typical"]] == keys
    assert [(t.entry_at, t.exit_at, t.reason, t.gross_r) for t in trades["max"]] == keys
    net = {k: v[0]["expectancy_r"] for k, v in runs.items()}
    assert net["zero"] > net["typical"] > net["max"]
    gross = {k: v[0]["expectancy_r_gross"] for k, v in runs.items()}
    assert gross["zero"] == pytest.approx(gross["typical"]) == pytest.approx(gross["max"])

    spread, slippage, swap = cost_inputs("WTI", s, "typical")
    direct = simulate_symbol(
        daily, "WTI", s, spread_bps=spread, slippage_bps=slippage, swap_annualised_bps=swap
    )
    assert [t.r_multiple for t in trades["typical"]] == pytest.approx([t.r_multiple for t in direct])
    assert all(t.spread_bps == spread and t.slippage_bps == slippage for t in trades["typical"])


def test_trade_carries_mid_prices_and_gross_r_for_audit():
    from trading_bot.paper_pnl import _adverse_fill

    base, e, d = _levels()
    daily = _with_post(base, _benign(e, d, 6))
    t = _sim(daily, spread_bps=10.0, slippage_bps=5.0, swap_annualised_bps=200.0)[0]
    assert t.entry_mid == pytest.approx(e, rel=1e-12)
    assert t.exit_mid == pytest.approx(float(daily["Open"].iloc[K + 4]), rel=1e-12)
    assert t.entry == pytest.approx(
        _adverse_fill(t.entry_mid, "buy", leg="entry", spread_bps=10.0, slippage_bps=5.0)
    )
    assert t.exit == pytest.approx(
        _adverse_fill(t.exit_mid, "buy", leg="exit", spread_bps=10.0, slippage_bps=5.0)
    )
    assert t.gross_r == pytest.approx((t.exit_mid - t.entry_mid) / d, abs=1e-12)
    assert t.r_multiple < t.gross_r
    assert (t.spread_bps, t.slippage_bps, t.swap_bps) == (10.0, 5.0, 200.0)


def test_both_hit_bar_is_counted_and_target_first_bound_reported():
    from trading_bot.backtest import run_backtest

    base, e, d = _levels()
    daily = _with_post(base, [(e, e + 2 * d + 1.0, e - d - 1.0, e)])
    sink: list = []
    report = run_backtest({"WTI": daily}, _settings(), costs="zero", trades_out=sink)
    (t,) = sink
    assert t.reason == "stop" and t.both_hit is True
    assert t.r_multiple == pytest.approx(-1.0, abs=1e-9)          # the booked (stop) outcome
    assert t.r_target_first == pytest.approx(2.0, abs=1e-9)       # the same bar at the target
    assert report["both_hit_bars"] == 1
    assert report["expectancy_r"] == pytest.approx(-1.0, abs=1e-9)
    assert report["expectancy_r_target_first"] == pytest.approx(2.0, abs=1e-9)


def test_target_first_bound_is_net_of_costs_and_equals_net_without_both_hits():
    from trading_bot.paper_pnl import _adverse_fill

    base, e, d = _levels()
    daily = _with_post(base, [(e, e + 2 * d + 1.0, e - d - 1.0, e)])
    t = _sim(daily, spread_bps=10.0, slippage_bps=5.0, swap_annualised_bps=200.0)[0]
    fin = t.entry * (200.0 / 10_000.0) * (t.hold_hours / 8766.0)
    alt = _adverse_fill(e + 2 * d, "buy", leg="exit", spread_bps=10.0, slippage_bps=5.0)
    assert t.r_target_first == pytest.approx((alt - t.entry - fin) / d, rel=1e-9)

    # Stop hit alone (target not touched): no both-hit, bound == booked R.
    plain = _with_post(base, _benign(e, d, 1) + [(e, e + 0.2 * d, e - 1.5 * d, e - 1.2 * d)])
    p = _sim(plain, spread_bps=10.0, slippage_bps=5.0)[0]
    assert p.reason == "stop" and p.both_hit is False
    assert p.r_target_first == p.r_multiple
    # A gap through the stop is not a both-hit bar either.
    gap = e - 3.0 * d
    gapped = _with_post(base, _benign(e, d, 1) + [(gap, e + 3 * d, gap - 0.1 * d, gap)])
    g = _sim(gapped)[0]
    assert g.gap is True and g.both_hit is False


def _clustered_trades(n_weeks: int = 60, per_week: int = 5):
    """`per_week` identical trades entered on the same Monday each week:
    perfectly correlated within a week, mean 0.2R, sd 1R across weeks."""
    from trading_bot.backtest import BacktestTrade

    trades = []
    mondays = pd.date_range("2022-01-03", periods=n_weeks, freq="W-MON", tz="UTC")
    for k, day in enumerate(mondays):
        r = 0.2 + (1.0 if k % 2 == 0 else -1.0)
        for j in range(per_week):
            trades.append(BacktestTrade(
                symbol=f"S{j}", side="buy", entry_at=day, entry=100.0, stop=99.0, tp=102.0,
                exit_at=day + pd.Timedelta(days=1), exit=100.0 + r, reason="time",
                r_multiple=r, pnl_pct=r / 100.0, gross_r=r, r_target_first=r,
            ))
    return trades


def test_week_cluster_bootstrap_is_at_least_as_wide_as_iid_on_same_day_trades():
    from trading_bot.backtest import cluster_bootstrap_ci, entry_week
    from trading_bot.forward_eval import bootstrap_expectancy_ci

    trades = _clustered_trades()
    rs = [t.r_multiple for t in trades]
    weeks = [entry_week(t.entry_at) for t in trades]
    assert len(set(weeks)) == 60
    mean, c_lo, c_hi = cluster_bootstrap_ci(rs, weeks)
    _, i_lo, i_hi = bootstrap_expectancy_ci(rs)
    assert mean == pytest.approx(0.2)
    assert c_hi - c_lo >= i_hi - i_lo
    # 5 perfectly correlated trades per cluster: ~sqrt(5) wider, not marginally.
    assert (c_hi - c_lo) > 1.8 * (i_hi - i_lo)
    # Singleton clusters reduce to (approximately) the i.i.d. bootstrap.
    _, s_lo, s_hi = cluster_bootstrap_ci(rs, list(range(len(rs))))
    assert (s_hi - s_lo) == pytest.approx(i_hi - i_lo, rel=0.25)


def test_verdict_uses_the_clustered_ci_not_the_iid_one():
    from trading_bot.backtest import _summarise

    out = _summarise(_clustered_trades())
    assert out["entry_weeks"] == 60
    assert out["expectancy_r_ci"][0] > 0          # i.i.d. would call it an edge...
    assert out["verdict_iid"] == "positive_expectancy"
    lo, hi = out["expectancy_r_ci_week"]
    assert lo < 0 < hi                            # ...the week-clustered CI does not
    assert out["verdict"] == "indeterminate"


def test_entry_week_uses_the_bar_exchange_date():
    from trading_bot.backtest import entry_week

    # Monday 00:00 New York is Monday 04:00 UTC; Sunday 23:00 London stays Sunday.
    ny = pd.Timestamp("2024-03-04", tz="America/New_York")
    assert entry_week(ny) == entry_week(pd.Timestamp("2024-03-08"))
    assert entry_week(pd.Timestamp("2024-03-03 23:00", tz="Europe/London")) == "2024-W09"
    assert entry_week(ny) == "2024-W10"


def test_unknown_symbol_is_skipped_not_given_a_default_spread():
    from trading_bot.backtest import run_backtest

    for costs in ("max", "typical", "zero"):
        sink: list = []
        report = run_backtest(
            {"WTI": _trending_up(), "FOOBAR": _trending_up()}, _settings(),
            costs=costs, trades_out=sink,
        )
        assert "FOOBAR" in report["skipped"], costs
        assert "unknown symbol" in report["skipped"]["FOOBAR"]
        assert set(report["by_symbol"]) == {"WTI"}
        assert {t.symbol for t in sink} == {"WTI"}


def test_format_comparison_shows_gross_clustered_ci_and_stop_first_bound():
    from trading_bot.backtest import format_comparison, run_backtest

    frames = {"WTI": _trending_up()}
    reports = {
        "daily_atr": run_backtest(frames, _settings(), costs="typical"),
        "live_hourly_equiv": run_backtest(
            frames, _settings(), stop_mode="live_hourly_equiv", hourly_ratio={"WTI": 4.0},
            costs="typical",
        ),
    }
    text = format_comparison(reports, {"WTI": 4.0})
    for label in ("gross E[R] (no costs)", "net E[R] 95% CI week", "net E[R] 95% CI iid*",
                  "stop+target same bar", "net E[R] if target-first", "verdict (week CI",
                  "typical"):
        assert label in text, label
    assert "channel exits" not in text  # default output unchanged


# --- default exit mode is pinned (byte-identical to the Task 7 code) -------
#
# Digests of every trade field from the pre-change code (HEAD 3120e9f) on
# synthetic walks with gaps. Together they hit stop, tp, time, gap-through
# and both-hit exits, longs and shorts, in both stop regimes. If one of these
# changes, the default (live) exit rules changed.


def _gappy(seed: int, n: int = 900) -> pd.DataFrame:
    daily = _random_walk(seed, n)
    rng = np.random.default_rng(seed + 1000)
    jump = 1.0 + rng.normal(0.0, 0.02, n) * (rng.random(n) < 0.15)
    daily["Open"] = daily["Open"] * jump
    daily["High"] = daily[["Open", "High"]].max(axis=1)
    daily["Low"] = daily[["Open", "Low"]].min(axis=1)
    return daily


def _trade_digest(trades, daily) -> str:
    import hashlib

    idx = daily.index
    rows = [
        f"{idx.get_loc(t.entry_at)}|{idx.get_loc(t.exit_at)}|{t.side}|{t.reason}|{t.gap}|{t.both_hit}|"
        f"{t.r_multiple:.12e}|{t.gross_r:.12e}|{t.hold_hours:.6f}|{t.r_target_first:.12e}|"
        f"{t.pnl_pct:.12e}|{t.stop:.12e}|{t.tp:.12e}"
        for t in trades
    ]
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


_DEFAULT_MODE_DIGESTS = {
    (3, "daily_atr"): (30, "f089df5aa47fdb875b07cc818e914bed396e538f4b59beaadebbf92eac2f0cac"),
    (3, "live_hourly_equiv"): (45, "05d3c42cacd9a67b8f4bc0cb67bf2aa1bdd9810275500b91ce09df7484c28a6e"),
    (11, "daily_atr"): (30, "562a43ab6eef65a406d9adffa834846050c7999d7a2cd8bb8f26c454caee8b6d"),
    (11, "live_hourly_equiv"): (47, "35a46c3cd6d30e78aac8b0f7444f3ed0521f4792b9b5540cc237a17088cd9f7f"),
    (29, "daily_atr"): (30, "4bb1dd040c2cc6def75783616b723a0c091c3f358cb612a67eba0c6b619ed91c"),
    (29, "live_hourly_equiv"): (47, "f5252222c36f457769d80bbeaabb7c5187906431d32350d7ff3cbc10849dbe91"),
}


@pytest.mark.parametrize("seed,stop_mode", sorted(_DEFAULT_MODE_DIGESTS))
def test_default_exit_mode_trades_are_unchanged(seed, stop_mode):
    from trading_bot.backtest import simulate_symbol

    daily = _gappy(seed)
    kw = {"hourly_ratio": {"WTI": 4.0}} if stop_mode == "live_hourly_equiv" else {}
    trades = simulate_symbol(
        daily, "WTI", _settings(), spread_bps=4.0, slippage_bps=1.0, swap_annualised_bps=200.0,
        allow_shorts=True, stop_mode=stop_mode, **kw,
    )
    explicit = simulate_symbol(
        daily, "WTI", _settings(), spread_bps=4.0, slippage_bps=1.0, swap_annualised_bps=200.0,
        allow_shorts=True, stop_mode=stop_mode, exit_mode="track_a", **kw,
    )
    n, digest = _DEFAULT_MODE_DIGESTS[(seed, stop_mode)]
    assert len(trades) == n
    assert _trade_digest(trades, daily) == digest
    assert explicit == trades


def test_default_exit_mode_max_cost_report_is_unchanged():
    from trading_bot.backtest import run_backtest

    frames = {"WTI": _gappy(3), "GOLD": _gappy(11), "SPX": _gappy(29)}
    report = run_backtest(frames, _settings(), costs="max", allow_shorts=True)
    assert report["exit_mode"] == "track_a"
    assert report["trades"] == 90
    assert report["exit_reasons"] == {"time": 84, "stop": 6}
    assert report["expectancy_r"] == pytest.approx(0.012458283906270684, rel=1e-12)
    assert report["expectancy_r_gross"] == pytest.approx(0.08212809226213577, rel=1e-12)
    assert report["expectancy_r_ci_week"] == pytest.approx(
        [-0.10726832099016567, 0.1292902649223064], rel=1e-12
    )
    assert report["avg_hold_hours"] == pytest.approx(83.2, rel=1e-12)


# --- exit_mode="channel_trail": pre-registered Turtle System 1 exit ---------
#
# docs/research/2026-10-06-preregistration-trend-exit.md. Same single-signal
# fixture as above (signal on bar K, entry at K+1's open E). The stop is
# 2.0 x ATR (D). Bars K+1..K+11 ramp up 0.2D a bar, so the 10-bar channel of
# the bars before T = K+12 is bar K+2's low, E + 0.15D. Bar T's own low sits
# at E + 0.05D: a channel that wrongly included bar T could never be broken
# by T's close.

T = K + 12


def _channel_levels(short: bool = False):
    base = _base(short)
    s = _settings()
    e = float(base["Close"].iloc[K])
    d = max(_atr_at_signal(base) * 2.0, e * s.stop_loss_pct_floor, 0.01)
    return base, e, d


def _channel_post(e: float, d: float, close_t: float, short: bool = False, tail: int = 4):
    sign = -1.0 if short else 1.0
    rows = []
    for j in range(1, 12):  # bars K+1..K+11
        op, cl = e + sign * 0.2 * d * (j - 1), e + sign * 0.2 * d * j
        rows.append((op, cl + 0.05 * d, op - 0.05 * d, cl) if not short
                    else (op, op + 0.05 * d, cl - 0.05 * d, cl))
    op = e + sign * 2.2 * d  # bar T
    rows.append((op, op + 0.05 * d, e + 0.05 * d, close_t) if not short
                else (op, e - 0.05 * d, op - 0.05 * d, close_t))
    nxt = e + sign * 0.3 * d  # bar T+1 opens away from T's close
    rows += [(nxt, nxt + 0.05 * d, nxt - 0.05 * d, nxt)] * tail
    return rows


def _csim(daily, **kw):
    kw.setdefault("exit_mode", "channel_trail")
    kw.setdefault("allow_shorts", True)
    return _sim(daily, **kw)


def test_channel_fixture_guard():
    """The scenario must put bar T's close below the prior-10 channel but not
    below its own low, and the stop must be far away."""
    base, e, d = _channel_levels()
    daily = _with_post(base, _channel_post(e, d, e + 0.1 * d))
    lows = daily["Low"].to_numpy()
    assert lows[T - 10:T].min() == pytest.approx(e + 0.15 * d)
    assert lows[T - 11:T].min() < e + 0.1 * d < lows[T - 10:T].min()
    assert lows[T] <= e + 0.1 * d
    assert lows[K + 1:].min() > e - d


def test_channel_exit_on_close_below_prior_10_bar_low_fills_at_next_open():
    base, e, d = _channel_levels()
    daily = _with_post(base, _channel_post(e, d, e + 0.1 * d))
    t = _csim(daily)[0]
    assert t.side == "buy"
    assert t.entry_at == daily.index[K + 1]
    assert t.reason == "channel"
    assert t.exit_at == daily.index[T + 1]
    assert t.exit_mid == pytest.approx(e + 0.3 * d, rel=1e-12)  # bar T+1's open...
    assert t.exit_mid != pytest.approx(float(daily["Close"].iloc[T]))  # ...not T's close
    assert t.gross_r == pytest.approx(0.3, abs=1e-9)
    assert t.gap is False
    assert t.hold_hours == pytest.approx(_hours(daily.index[T + 1] - daily.index[K + 1]))


def _hours(delta) -> float:
    return pd.Timedelta(delta).total_seconds() / 3600.0


def test_channel_exit_short_mirror_uses_prior_10_bar_high():
    base, e, d = _channel_levels(short=True)
    daily = _with_post(base, _channel_post(e, d, e - 0.1 * d, short=True))
    highs = daily["High"].to_numpy()
    assert highs[T - 10:T].max() == pytest.approx(e - 0.15 * d)
    assert highs[T] >= e - 0.1 * d
    t = _csim(daily)[0]
    assert t.side == "sell"
    assert t.stop == pytest.approx(e + d, rel=1e-12)
    assert t.reason == "channel"
    assert t.exit_at == daily.index[T + 1]
    assert t.exit_mid == pytest.approx(e - 0.3 * d, rel=1e-12)
    assert t.gross_r == pytest.approx(0.3, abs=1e-9)


@pytest.mark.parametrize("close_t", ["at_channel", "between_bar_t_minus_10_and_minus_9"])
def test_channel_is_exactly_the_10_prior_bars_and_strict(close_t):
    """A close AT the 10-bar low is not a break (strict <); a close above bar
    T-10's low but below bar T-9's would break a 9-bar channel, not 10."""
    base, e, d = _channel_levels()
    probe = _with_post(base, _channel_post(e, d, e + 0.1 * d))
    lows = probe["Low"].to_numpy()
    level = float(lows[T - 10:T].min()) if close_t == "at_channel" else e + 0.2 * d
    assert lows[T - 10] <= level < lows[T - 9]
    daily = _with_post(base, _channel_post(e, d, level))
    t = _csim(daily)[0]
    assert t.reason == "end_of_data"
    assert t.exit_at == daily.index[-1]


def test_channel_exit_decision_does_not_read_future_bars():
    base, e, d = _channel_levels()
    full = _with_post(base, _channel_post(e, d, e + 0.1 * d, tail=8))
    cut = full.iloc[: T + 2]
    a, b = _csim(full)[0], _csim(cut)[0]
    assert (a.exit_at, a.reason, a.exit_mid) == (b.exit_at, b.reason, b.exit_mid)


def test_channel_signal_on_the_last_bar_closes_at_end_of_data():
    base, e, d = _channel_levels()
    daily = _with_post(base, _channel_post(e, d, e + 0.1 * d, tail=0))
    t = _csim(daily)[0]
    assert t.reason == "end_of_data"
    assert t.exit_at == daily.index[T]
    assert t.exit_mid == pytest.approx(e + 0.1 * d)


def test_channel_mode_stop_is_two_atr_regardless_of_settings():
    from trading_bot.backtest import CHANNEL_STOP_ATR_MULT, simulate_symbol

    assert CHANNEL_STOP_ATR_MULT == 2.0
    base, e, d = _channel_levels()
    atr = _atr_at_signal(base)
    assert d == pytest.approx(2.0 * atr)  # the ATR term binds, not the floor
    daily = _with_post(base, _channel_post(e, d, e + 0.1 * d))
    t = _csim(daily)[0]
    assert t.stop == pytest.approx(e - 2.0 * atr, rel=1e-12)
    assert t.stop_pct == pytest.approx(2.0 * atr / e, rel=1e-12)
    wide = _settings().model_copy(update={"atr_stop_multiplier": 3.0})
    w = simulate_symbol(daily, "WTI", wide, spread_bps=0.0, slippage_bps=0.0,
                        swap_annualised_bps=0.0, exit_mode="channel_trail")[0]
    assert w.stop == pytest.approx(e - 2.0 * atr, rel=1e-12)
    # The default mode still follows the setting.
    assert _sim(daily)[0].stop == pytest.approx(e - 1.5 * atr, rel=1e-12)


def test_channel_mode_has_no_target_and_no_time_cap():
    """A trend that runs 60 bars stays open: no 72h time stop, no 2R target."""
    base, e, d = _channel_levels()
    post = []
    for j in range(1, 61):
        op, cl = e + 0.1 * d * (j - 1), e + 0.1 * d * j
        post.append((op, cl + 0.05 * d, op - 0.02 * d, cl))
    daily = _with_post(base, post)
    assert _sim(daily)[0].reason in ("time", "tp")  # the default mode would close it
    t = _csim(daily)[0]
    assert t.reason == "end_of_data"
    assert t.exit_at == daily.index[-1]
    assert t.exit_at - t.entry_at == pd.Timedelta(days=59)
    assert t.gross_r == pytest.approx(6.0, abs=1e-9)
    assert np.isnan(t.tp)
    assert t.both_hit is False and t.r_target_first == t.r_multiple


def test_channel_mode_bar_touching_stop_books_the_stop_even_if_close_breaks_channel():
    base, e, d = _channel_levels()
    post = _channel_post(e, d, e + 0.1 * d)
    op = post[T - K - 1][0]
    post[T - K - 1] = (op, op + 0.05 * d, e - 1.5 * d, e + 0.1 * d)  # bar T also touches the stop
    daily = _with_post(base, post)
    t = _csim(daily)[0]
    assert t.reason == "stop"
    assert t.exit_at == daily.index[T]
    assert t.exit_mid == pytest.approx(e - d, rel=1e-12)
    assert t.r_multiple == pytest.approx(-1.0, abs=1e-9)


def test_channel_mode_gap_through_stop_fills_at_the_open():
    base, e, d = _channel_levels()
    gap = e - 3.0 * d
    daily = _with_post(base, _benign(e, d, 1) + [(gap, gap + 0.1 * d, gap - 0.1 * d, gap)])
    t = _csim(daily)[0]
    assert t.reason == "stop" and t.gap is True
    assert t.exit_mid == pytest.approx(gap, rel=1e-12)
    assert t.r_multiple == pytest.approx(-3.0, abs=1e-9)


def test_channel_mode_reuses_entry_costs_and_r():
    from trading_bot.paper_pnl import _adverse_fill

    base, e, d = _channel_levels()
    daily = _with_post(base, _channel_post(e, d, e + 0.1 * d))
    t = _csim(daily, spread_bps=10.0, slippage_bps=5.0, swap_annualised_bps=200.0)[0]
    assert t.entry_mid == pytest.approx(e, rel=1e-12)
    assert t.entry == pytest.approx(_adverse_fill(e, "buy", leg="entry", spread_bps=10.0, slippage_bps=5.0))
    assert t.exit == pytest.approx(
        _adverse_fill(e + 0.3 * d, "buy", leg="exit", spread_bps=10.0, slippage_bps=5.0)
    )
    fin = t.entry * (200.0 / 10_000.0) * (t.hold_hours / 8766.0)
    assert t.r_multiple == pytest.approx((t.exit - t.entry - fin) / d, rel=1e-9)


def test_channel_mode_report_and_validation():
    from trading_bot.backtest import format_comparison, run_backtest

    base, e, d = _channel_levels()
    daily = _with_post(base, _channel_post(e, d, e + 0.1 * d))
    report = run_backtest({"WTI": daily}, _settings(), costs="zero", allow_shorts=True,
                          exit_mode="channel_trail")
    assert report["exit_mode"] == "channel_trail"
    assert report["exit_reasons"].get("channel", 0) >= 1
    text = format_comparison({"daily_atr": report}, {})
    assert "channel exits" in text and "channel_trail" in text
    with pytest.raises(ValueError):
        run_backtest({"WTI": daily}, _settings(), exit_mode="turtle")
    with pytest.raises(ValueError):
        _sim(daily, exit_mode="turtle")
