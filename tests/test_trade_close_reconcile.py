"""Mid-session `trade_close` reconcile (review 2026-04-29-adhoc-1 #5).

The 2026-04-29 incident: 3 WTI fills opened at 09:56–10:04 and were
closed (likely by IG-clamped 12pt stop) within minutes. None of them
produced a `trade_close` event, so the supervisor's consecutive_losses
and same_symbol_stop_streak rules were blind.

The previous fix (commit 362241f, review #2 of 2026-04-28-adhoc-1)
only emitted trade_close events during the startup reconcile path
(`_reconcile_on_startup`). It did not cover the much more common case:
a position vanishes mid-session because the broker stop fired.

This change extends `_refresh_runtime_state` (which runs every cycle)
to detect vanished positions and emit a synthesised trade_close event
the same way startup reconcile does, optionally enriched with broker
transaction-history data when available.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.bot import TradingBot
from trading_bot.config import Settings
from trading_bot.models import AccountSnapshot, OpenPosition


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        broker="paper",
        runtime_state_path=tmp_path / "runtime_state.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "paper_pnl.json",
        json_log_path=tmp_path / "events.jsonl",
        kill_switch_file=tmp_path / "kill_switch",
        openai_api_key="test",
    )


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _account(positions: list[OpenPosition]) -> AccountSnapshot:
    return AccountSnapshot(
        mode="demo", currency="GBP",
        total_funds=10_000.0, free_funds=10_000.0, open_positions=positions,
    )


def _make_bot(tmp_path: Path) -> TradingBot:
    """Build a TradingBot without invoking external services."""
    settings = _settings(tmp_path)
    bot = TradingBot.__new__(TradingBot)
    bot.settings = settings
    from trading_bot.json_log import JsonEventLogger
    bot._early_event_logger = JsonEventLogger(path=settings.json_log_path)
    bot.json_log = bot._early_event_logger
    bot.runtime_state = {"daily": {}, "positions": {}, "open_position_invalidations": {}}
    bot.broker = MagicMock()
    return bot


# --- mid-session vanishing → emits trade_close --------------------


def test_mid_session_vanish_emits_trade_close(tmp_path):
    bot = _make_bot(tmp_path)

    # Cycle 1: WTI opens at price 65.42 (Yahoo) → tracked
    pos = OpenPosition(symbol="WTI", position_id="DI001", quantity=1.0,
                       current_price=65.42, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))
    assert "DI001" in bot.runtime_state["positions"]
    assert _read_events(bot.settings.json_log_path) == []

    # Cycle 2: position no longer at broker → must emit trade_close
    bot.broker.get_transaction_history = MagicMock(return_value=[])
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    assert "DI001" not in bot.runtime_state["positions"]
    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    data = closes[0]["data"]
    assert data["position_id"] == "DI001"
    assert data["symbol"] == "WTI"
    assert data["direction"] == "buy"
    # Per review 2026-05-05-adhoc-1 #2: mid-session reconciles now emit
    # ``reconciled_mid_session`` to distinguish from real startup
    # reconciles. The previous literal ``reconciled_on_startup`` was a
    # mis-tag — the call originates from ``_refresh_runtime_state``,
    # not ``_reconcile_on_startup``.
    assert data["close_source"] == "reconciled_mid_session"  # fallback when no tx
    assert data["estimated"] is True
    assert data["reason"] == "stop"


def test_mid_session_vanish_uses_broker_tx_when_available(tmp_path):
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="WTI", position_id="DI002", quantity=1.0,
                       current_price=65.42, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    # Broker history uses the REAL IG /history/transactions shape: rows are
    # keyed by `reference` (NOT dealId), carry openLevel/closeLevel/
    # profitAndLoss/transactionType. Match is by openLevel≈entry_price.
    # entry_price was recorded as 65.42 (the first-sighting current_price).
    bot.broker.get_transaction_history = MagicMock(return_value=[
        {"reference": "N7F5MHAV", "transactionType": "DEAL",
         "instrumentName": "Oil - US Crude (£1)",
         "openLevel": "65.42", "closeLevel": "65.30",
         "profitAndLoss": "£-12.34", "size": "+1.0",
         "openDateUtc": datetime.now(timezone.utc).isoformat()},
    ])
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    data = closes[0]["data"]
    assert data["close_level"] == 65.30
    assert data["close_source"] == "ig_transaction_history"
    assert data["estimated"] is False
    assert data["pnl_cash"] == pytest.approx(-12.34, rel=1e-6)
    # pnl_pct is now derived from open/close levels (a true return):
    # (65.30 - 65.42) / 65.42 for a BUY.
    assert data["pnl_pct"] == pytest.approx((65.30 - 65.42) / 65.42, rel=1e-3)


def test_broker_tx_ignores_fee_rows_and_matches_deal(tmp_path):
    """Fee/interest rows (transactionType WITH) must be skipped; only the
    DEAL row with openLevel≈entry_price counts."""
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="WTI", position_id="DI_FEE", quantity=1.0,
                       current_price=90.639, direction="sell")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    bot.broker.get_transaction_history = MagicMock(return_value=[
        {"reference": "11728203", "transactionType": "WITH",
         "instrumentName": "Daily Admin Fee ... Oil - US Crude (£1)",
         "openLevel": "-", "closeLevel": "0", "profitAndLoss": "£-0.02",
         "openDateUtc": datetime.now(timezone.utc).isoformat()},
        {"reference": "N7F5MHAV", "transactionType": "DEAL",
         "instrumentName": "Oil - US Crude (£1)",
         "openLevel": "90.639", "closeLevel": "91.9975",
         "profitAndLoss": "£-4.08", "size": "-1.0",
         "openDateUtc": datetime.now(timezone.utc).isoformat()},
    ])
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    data = closes[0]["data"]
    # Must pick the DEAL row (£-4.08), not the -£0.02 fee row.
    assert data["pnl_cash"] == pytest.approx(-4.08, rel=1e-6)
    assert data["close_level"] == pytest.approx(91.9975, rel=1e-6)
    # SELL: close > open is a loss → negative return.
    assert data["pnl_pct"] < 0


def test_multiple_vanished_positions_each_emit_close(tmp_path):
    bot = _make_bot(tmp_path)

    p1 = OpenPosition(symbol="WTI", position_id="DI100", quantity=1.0,
                      current_price=65.0, direction="buy")
    p2 = OpenPosition(symbol="GOLD", position_id="DI101", quantity=0.5,
                      current_price=4700.0, direction="buy")
    bot._refresh_runtime_state(_account([p1, p2]), datetime.now(timezone.utc))

    bot.broker.get_transaction_history = MagicMock(return_value=[])
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 2
    pids = sorted([c["data"]["position_id"] for c in closes])
    assert pids == ["DI100", "DI101"]


def test_position_still_open_no_trade_close_emitted(tmp_path):
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="WTI", position_id="DI200", quantity=1.0,
                      current_price=65.0, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    # Cycle 2: position still there
    bot.broker.get_transaction_history = MagicMock(return_value=[])
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert closes == []


def test_first_seen_position_not_treated_as_vanished(tmp_path):
    """Critical regression: a fresh position (first sighting) must NOT
    immediately emit a trade_close."""
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="WTI", position_id="DI300", quantity=1.0,
                      current_price=65.0, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert closes == []


def test_broker_history_failure_falls_back_to_estimated(tmp_path):
    """If get_transaction_history raises, emit estimated trade_close."""
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="WTI", position_id="DI400", quantity=1.0,
                      current_price=65.42, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    bot.broker.get_transaction_history = MagicMock(side_effect=RuntimeError("API down"))
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    data = closes[0]["data"]
    assert data["estimated"] is True
    # Per review 2026-05-05-adhoc-1 #2: mid-session reconciles now use
    # ``reconciled_mid_session`` literal.
    assert data["close_source"] == "reconciled_mid_session"


def test_broker_without_history_helper_falls_back(tmp_path):
    """A broker that doesn't expose get_transaction_history is supported."""
    bot = _make_bot(tmp_path)
    # Strip the helper
    bot.broker = MagicMock(spec=[])

    pos = OpenPosition(symbol="WTI", position_id="DI500", quantity=1.0,
                      current_price=65.42, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    assert closes[0]["data"]["estimated"] is True


# --- Per review 2026-05-05-adhoc-1 #2: close_source caller-context split ---


def test_startup_reconcile_tags_close_source_as_reconciled_on_startup(tmp_path):
    """``_reconcile_on_startup`` must tag synthesised closes with the
    ``reconciled_on_startup`` literal so downstream consumers can
    distinguish post-restart catch-up from mid-session vanishes.
    """
    bot = _make_bot(tmp_path)
    # Seed a tracked position from a previous session
    bot.runtime_state["positions"]["DI_STARTUP_1"] = {
        "symbol": "WTI", "first_seen_at": datetime.now(timezone.utc).isoformat(),
        "entry_price": 65.0, "highest_price": 65.0, "lowest_price": 65.0,
        "direction": "buy",
    }
    # Broker reports no open positions (position closed while bot was down)
    bot.broker.get_account_snapshot = MagicMock(return_value=_account([]))
    bot.broker.get_transaction_history = MagicMock(return_value=[])

    bot._reconcile_on_startup()

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    data = closes[0]["data"]
    assert data["position_id"] == "DI_STARTUP_1"
    assert data["close_source"] == "reconciled_on_startup", (
        "startup-path closes must keep the reconciled_on_startup literal"
    )
    assert data["estimated"] is True


def test_mid_session_reconcile_tags_close_source_as_reconciled_mid_session(tmp_path):
    """``_refresh_runtime_state`` is called every cycle. When a tracked
    position vanishes mid-session, the close_source must be
    ``reconciled_mid_session``, NOT ``reconciled_on_startup``.

    This is the bug reported in 2026-05-05 13:01:23: a BRENT close was
    tagged ``reconciled_on_startup`` despite no service restart in the
    surrounding 8h window.
    """
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="BRENT", position_id="DI_MID_1", quantity=1.0,
                       current_price=113.0, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    bot.broker.get_transaction_history = MagicMock(return_value=[])
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    data = closes[0]["data"]
    assert data["position_id"] == "DI_MID_1"
    assert data["close_source"] == "reconciled_mid_session", (
        "mid-session reconciles must be tagged reconciled_mid_session, "
        f"got {data['close_source']!r}"
    )


def test_broker_tx_overrides_caller_context_tag(tmp_path):
    """When broker transaction history is available, ``ig_transaction_history``
    is the canonical tag regardless of caller_context — the broker
    confirmation is more authoritative than the reconcile path label.
    """
    bot = _make_bot(tmp_path)

    pos = OpenPosition(symbol="WTI", position_id="DI_TX_1", quantity=1.0,
                       current_price=65.0, direction="buy")
    bot._refresh_runtime_state(_account([pos]), datetime.now(timezone.utc))

    bot.broker.get_transaction_history = MagicMock(return_value=[
        {"reference": "NXYZ", "transactionType": "DEAL",
         "instrumentName": "Oil - US Crude (£1)",
         "openLevel": "65.0", "closeLevel": "64.80",
         "profitAndLoss": "£-15.00", "size": "+1.0",
         "openDateUtc": datetime.now(timezone.utc).isoformat()},
    ])
    bot._refresh_runtime_state(_account([]), datetime.now(timezone.utc))

    closes = [e for e in _read_events(bot.settings.json_log_path)
              if e.get("event") == "trade_close"]
    assert len(closes) == 1
    assert closes[0]["data"]["close_source"] == "ig_transaction_history"
    assert closes[0]["data"]["estimated"] is False
    assert closes[0]["data"]["pnl_cash"] == pytest.approx(-15.00, rel=1e-6)


# --- direct unit tests for the matcher (the core 0.3 fix) ----------


def test_matcher_returns_none_when_no_dealtype_match():
    from trading_bot.bot import match_transaction_to_position
    pos = {"entry_price": 100.0, "direction": "buy"}
    # Only a fee row, no DEAL → no match.
    txs = [{"transactionType": "WITH", "openLevel": "100.0",
            "profitAndLoss": "£-0.02"}]
    assert match_transaction_to_position(pos, txs) is None


def test_matcher_matches_on_open_level():
    from trading_bot.bot import match_transaction_to_position
    pos = {"entry_price": 9063.9, "direction": "buy"}
    txs = [
        {"transactionType": "DEAL", "openLevel": "9999.0",
         "profitAndLoss": "£1.00", "closeLevel": "9998.0"},
        {"transactionType": "DEAL", "openLevel": "9063.9",
         "profitAndLoss": "£-4.08", "closeLevel": "9199.7"},
    ]
    match = match_transaction_to_position(pos, txs)
    assert match is not None
    assert match["profitAndLoss"] == "£-4.08"


def test_matcher_open_level_tolerance():
    from trading_bot.bot import match_transaction_to_position
    pos = {"entry_price": 100.0, "direction": "buy"}
    # 100.04 is within 0.05% of 100.0 (0.04%); 100.10 is outside.
    txs = [{"transactionType": "DEAL", "openLevel": "100.04",
            "profitAndLoss": "£1.00"}]
    assert match_transaction_to_position(pos, txs) is not None
    txs2 = [{"transactionType": "DEAL", "openLevel": "100.10",
             "profitAndLoss": "£1.00"}]
    assert match_transaction_to_position(pos, txs2) is None


def test_matcher_uses_time_distance_tiebreak():
    from trading_bot.bot import match_transaction_to_position
    base = datetime(2026, 6, 4, 17, 32, 11, tzinfo=timezone.utc)
    pos = {"entry_price": 100.0, "direction": "buy",
           "opened_at": base.isoformat()}
    near = base.isoformat()
    far = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    txs = [
        {"transactionType": "DEAL", "openLevel": "100.0",
         "profitAndLoss": "£-9.99", "openDateUtc": far},
        {"transactionType": "DEAL", "openLevel": "100.0",
         "profitAndLoss": "£-1.11", "openDateUtc": near},
    ]
    match = match_transaction_to_position(pos, txs)
    assert match["profitAndLoss"] == "£-1.11"  # closest in time wins


def test_matcher_handles_missing_entry_price():
    from trading_bot.bot import match_transaction_to_position
    assert match_transaction_to_position({}, [{"transactionType": "DEAL",
                                               "openLevel": "100.0"}]) is None
