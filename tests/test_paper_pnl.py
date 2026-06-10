"""Tests for the upgraded cost-aware paper-fill simulator (Phase 3 pivot).

The forward-walk eval is only valid if the simulator does NOT flatter the
strategy: adverse fills (half-spread + slippage on both legs), intrabar
High/Low stop/TP detection with a stop-first tie-break, max-holding expiry,
and financing accrual. Bars are injected (no network).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from trading_bot.paper_pnl import PaperPnL


def _bars(rows: list[tuple[float, float, float]], start: str = "2026-06-01") -> pd.DataFrame:
    """rows = [(high, low, close), ...] hourly bars."""
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "High": [r[0] for r in rows],
            "Low": [r[1] for r in rows],
            "Close": [r[2] for r in rows],
        },
        index=idx,
    )


def _pnl(tmp_path, bars: pd.DataFrame | None) -> PaperPnL:
    return PaperPnL(tmp_path / "paper.json", bar_fetcher=lambda sym, opened_at: bars)


def _open(p: PaperPnL, *, side="buy", entry=100.0, stop_dist=2.0, tp_dist=4.0, **kw):
    stop = entry - stop_dist if side == "buy" else entry + stop_dist
    tp = entry + tp_dist if side == "buy" else entry - tp_dist
    p.record_entry(
        symbol="GOLD", side=side, entry=entry, stop=stop, tp=tp,
        opened_at=kw.pop("opened_at", "2026-06-01T00:00:00+00:00"),
        data_symbol="GC=F",
        decision_id=kw.pop("decision_id", "c1:GOLD"),
        confidence=kw.pop("confidence", 0.8),
        spread_bps=kw.pop("spread_bps", 0.0),
        slippage_bps=kw.pop("slippage_bps", 0.0),
        swap_annualised_bps=kw.pop("swap_annualised_bps", 0.0),
        max_holding_hours=kw.pop("max_holding_hours", 48.0),
    )


def test_adverse_entry_fill_buy(tmp_path):
    p = _pnl(tmp_path, None)
    _open(p, side="buy", entry=100.0, spread_bps=10.0, slippage_bps=5.0)
    item = p.state["open"][0]
    # buy fills ABOVE mid: +(spread/2 + slippage) = +(5+5)bps = 100.10
    assert abs(item["fill_entry"] - 100.10) < 1e-6
    assert item["decision_id"] == "c1:GOLD"
    assert item["confidence"] == 0.8


def test_adverse_entry_fill_sell(tmp_path):
    p = _pnl(tmp_path, None)
    _open(p, side="sell", entry=100.0, spread_bps=10.0, slippage_bps=5.0)
    item = p.state["open"][0]
    # sell fills BELOW mid: -(5+5)bps = 99.90
    assert abs(item["fill_entry"] - 99.90) < 1e-6


def test_tp_hit_via_intrabar_high(tmp_path):
    # Close never reaches TP but the bar HIGH does -> must close as tp.
    bars = _bars([(103.0, 99.5, 101.0), (104.5, 100.5, 102.0)])
    p = _pnl(tmp_path, bars)
    _open(p, side="buy", entry=100.0, stop_dist=2.0, tp_dist=4.0)
    p.evaluate()
    assert p.state["open"] == []
    closed = p.state["closed"][0]
    assert closed["reason"] == "tp"
    assert closed["win"] is True
    assert closed["r_multiple"] > 0


def test_stop_hit_via_intrabar_low(tmp_path):
    # Close stays above stop but the bar LOW touches it -> stopped out.
    bars = _bars([(101.0, 97.5, 100.5)])
    p = _pnl(tmp_path, bars)
    _open(p, side="buy", entry=100.0, stop_dist=2.0, tp_dist=4.0)
    p.evaluate()
    closed = p.state["closed"][0]
    assert closed["reason"] == "stop"
    assert closed["win"] is False
    assert closed["r_multiple"] < 0


def test_stop_first_tie_break_when_bar_touches_both(tmp_path):
    # One bar spans both stop (98) and TP (104): conservative = stop wins.
    bars = _bars([(105.0, 97.0, 102.0)])
    p = _pnl(tmp_path, bars)
    _open(p, side="buy", entry=100.0, stop_dist=2.0, tp_dist=4.0)
    p.evaluate()
    assert p.state["closed"][0]["reason"] == "stop"


def test_max_holding_expiry_closes_at_close_price(tmp_path):
    # Neither level ever touched; after max_holding_hours the position must
    # time-expire at the last bar close.
    rows = [(100.6, 99.4, 100.2)] * 30  # 30 hourly bars, range never hits 98/104
    bars = _bars(rows)
    p = _pnl(tmp_path, bars)
    _open(p, side="buy", entry=100.0, stop_dist=2.0, tp_dist=4.0,
          max_holding_hours=24.0)
    p.evaluate(now=datetime(2026, 6, 2, 6, 0, tzinfo=timezone.utc))  # 30h later
    assert p.state["open"] == []
    closed = p.state["closed"][0]
    assert closed["reason"] == "expiry"


def test_financing_cost_reduces_pnl(tmp_path):
    rows = [(100.6, 99.4, 100.2)] * 30
    bars = _bars(rows)
    costs = {}
    for swap in (0.0, 500.0):
        p = PaperPnL(tmp_path / f"paper_{swap}.json",
                     bar_fetcher=lambda sym, opened_at: bars)
        _open(p, side="buy", entry=100.0, stop_dist=2.0, tp_dist=4.0,
              max_holding_hours=24.0, swap_annualised_bps=swap)
        p.evaluate(now=datetime(2026, 6, 2, 6, 0, tzinfo=timezone.utc))
        costs[swap] = p.state["closed"][0]["pnl_price_units"]
    assert costs[500.0] < costs[0.0]  # financing must hurt


def test_sell_side_tp(tmp_path):
    bars = _bars([(100.5, 95.5, 99.0)])  # low touches 96 TP for a short at 100
    p = _pnl(tmp_path, bars)
    _open(p, side="sell", entry=100.0, stop_dist=2.0, tp_dist=4.0)
    p.evaluate()
    closed = p.state["closed"][0]
    assert closed["reason"] == "tp"
    assert closed["win"] is True


def test_legacy_rows_without_new_fields_still_evaluate(tmp_path):
    # Backwards compat: rows written by the v1 simulator lack fill_entry /
    # cost fields. They must still close without raising.
    path = tmp_path / "paper.json"
    bars = _bars([(105.0, 99.0, 104.5)])
    p = PaperPnL(path, bar_fetcher=lambda sym, opened_at: bars)
    p.state["open"] = [{
        "symbol": "GOLD", "data_symbol": "GC=F", "side": "buy",
        "entry": 100.0, "stop": 98.0, "tp": 104.0,
        "opened_at": "2026-06-01T00:00:00+00:00",
    }]
    p.evaluate()
    assert len(p.state["closed"]) == 1
    assert p.state["closed"][0]["reason"] == "tp"


def test_no_bars_keeps_position_open(tmp_path):
    p = _pnl(tmp_path, None)
    _open(p)
    p.evaluate()
    assert len(p.state["open"]) == 1
    assert p.state["closed"] == []


def test_state_round_trip_persists(tmp_path):
    path = tmp_path / "paper.json"
    p = PaperPnL(path, bar_fetcher=lambda s, o: None)
    _open(p)
    p2 = PaperPnL(path, bar_fetcher=lambda s, o: None)
    assert len(p2.state["open"]) == 1
    assert p2.state["open"][0]["decision_id"] == "c1:GOLD"
