from __future__ import annotations

from datetime import datetime, timezone

from t212_cfd_bot.portfolio import ConsecutiveLossTracker, CorrelationCap, TradeJournal


def test_trade_journal_records_and_reads():
    state = {"trade_journal": []}
    journal = TradeJournal(state=state)
    journal.record(
        symbol="WTI",
        direction="sell",
        entry=95.0,
        exit=93.0,
        entry_at=datetime.now(timezone.utc).isoformat(),
        exit_at=datetime.now(timezone.utc).isoformat(),
        reason="tp",
    )
    assert len(journal.entries()) == 1
    assert journal.entries()[0].pnl_pct > 0


def test_consecutive_loss_tracker_triggers_pause():
    state = {
        "trade_journal": [
            {"symbol": "X", "direction": "buy", "entry": 100, "exit": 99, "pnl_pct": -0.01, "r": -1.0, "reason": "stop", "entry_at": "2026-04-23T00:00:00+00:00", "exit_at": "2026-04-23T01:00:00+00:00"},
            {"symbol": "Y", "direction": "buy", "entry": 100, "exit": 98, "pnl_pct": -0.02, "r": -1.0, "reason": "stop", "entry_at": "2026-04-23T02:00:00+00:00", "exit_at": "2026-04-23T03:00:00+00:00"},
            {"symbol": "Z", "direction": "buy", "entry": 100, "exit": 97, "pnl_pct": -0.03, "r": -1.0, "reason": "stop", "entry_at": "2026-04-23T04:00:00+00:00", "exit_at": "2026-04-23T05:00:00+00:00"},
        ],
        "consecutive_loss_pause_until": None,
    }
    tracker = ConsecutiveLossTracker(state=state, limit=3, pause_hours=24)
    ok, reason = tracker.check(now=datetime(2026, 4, 23, 6, tzinfo=timezone.utc))
    assert not ok
    assert "consecutive" in reason.lower()


def test_correlation_cap_blocks_same_group():
    class _Inst:
        def __init__(self, group):
            self.correlation_group = group

    instruments = {"SPY": _Inst("us_equity"), "QQQ": _Inst("us_equity"), "WTI": _Inst("energy_oil")}
    cap = CorrelationCap(cap=0.7)
    assert cap.would_breach("QQQ", ["SPY"], instruments=instruments) is True
    assert cap.would_breach("WTI", ["SPY"], instruments=instruments) is False
