from supervisor.journal import append_event
from supervisor.rollups import compute_daily_rollup


def test_rollup_computes_win_rate(tmp_path):
    p = tmp_path / "j.jsonl"
    # 1 win, 2 losses
    append_event(p, {"type": "trade_close", "symbol": "NVDA", "exit": 100.0, "pnl_pct": 1.5, "reason": "tp", "held_minutes": 30, "ts": "2026-04-27T12:00:00+00:00"})
    append_event(p, {"type": "trade_close", "symbol": "WTI", "exit": 50.0, "pnl_pct": -0.8, "reason": "stop", "held_minutes": 60, "ts": "2026-04-27T13:00:00+00:00"})
    append_event(p, {"type": "trade_close", "symbol": "WTI", "exit": 48.0, "pnl_pct": -1.2, "reason": "stop", "held_minutes": 45, "ts": "2026-04-27T15:00:00+00:00"})
    rollup = compute_daily_rollup(p, date="2026-04-27")
    assert rollup["trades"] == 3
    assert rollup["wins"] == 1
    assert rollup["losses"] == 2
    assert abs(rollup["win_rate"] - (1/3)) < 1e-6
    assert abs(rollup["net_pnl_pct"] - (1.5 - 0.8 - 1.2)) < 1e-6
    assert rollup["by_symbol"]["WTI"]["n"] == 2
    assert abs(rollup["by_symbol"]["WTI"]["pnl"] - (-2.0)) < 1e-6


def test_rollup_empty_day(tmp_path):
    p = tmp_path / "j.jsonl"
    rollup = compute_daily_rollup(p, date="2026-04-27")
    assert rollup["trades"] == 0
    assert rollup["wins"] == 0
    assert rollup["win_rate"] == 0.0
