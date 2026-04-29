"""Watcher rule `repeated_execution_errors` must count both `execution`
failures AND `risk_block` events (review 2026-04-29-adhoc-1 #3).

The 2026-04-29 live incident produced 8 same-symbol-same-reason WTI
``STOP_TOO_TIGHT_VS_SPREAD`` risk_blocks between 11:23 and 16:49 UTC.
The previous rule only matched on ``execution.error`` strings and
silently ignored the 8 risk_blocks, so the supervisor never received
the rule_trigger that would have surfaced the loop.

After the fix, risk_block events count under
``risk_block:<REASON>`` keys so they don't collide with execution.error
strings, and the rule fires when 3+ same-key events appear in the window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from supervisor.rules import repeated_execution_errors


def _risk_block(symbol: str, reason: str, *, age_minutes: float = 0.0) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "event": "risk_block",
        "timestamp": ts.isoformat(),
        "data": {"symbol": symbol, "reason": reason},
    }


def _exec_event(ts, results):
    return {"event": "execution", "timestamp": ts, "data": results}


def _failed_result(symbol, error):
    return {"plan": {"symbol": symbol, "action": "BUY"}, "status": "failed",
            "submitted_at": "2026-04-29T11:00:00+00:00", "error": error}


# --- pure risk_block stream (the 2026-04-29 incident shape) ------------


def test_eight_wti_stop_too_tight_blocks_fire_rule():
    """The actual incident: 8 same-symbol-same-reason risk_blocks → fire."""
    events = [
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD", age_minutes=i * 30)
        for i in range(8)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=6.0)
    assert ev is not None
    assert ev.rule == "repeated_execution_errors"
    assert ev.action == "alert_only"
    assert ev.detail["symbol"] == "WTI"
    assert ev.detail["error"] == "risk_block:STOP_TOO_TIGHT_VS_SPREAD"
    assert ev.detail["count"] == 8


def test_three_risk_blocks_at_threshold():
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(3)]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is not None
    assert ev.detail["count"] == 3


def test_two_risk_blocks_below_threshold():
    events = [_risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD") for _ in range(2)]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None


def test_risk_blocks_grouped_by_reason_not_just_symbol():
    """Different reasons on the same symbol should NOT combine."""
    events = [
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD"),
        _risk_block("WTI", "ATTACHED_ORDER_LEVEL_ERROR"),
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD"),
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None  # 2 of one reason, 1 of another → no key reaches 3


def test_risk_blocks_grouped_by_symbol_not_just_reason():
    """Different symbols, same reason → no single key reaches threshold."""
    events = [
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD"),
        _risk_block("NATGAS", "STOP_TOO_TIGHT_VS_SPREAD"),
        _risk_block("BRENT", "STOP_TOO_TIGHT_VS_SPREAD"),
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None


def test_natgas_attached_order_level_error_fires_at_threshold():
    """NATGAS shape from 2026-04-29 09:36/09:42 — only 2 in actual session,
    but verify rule mechanics with 3."""
    events = [_risk_block("NATGAS", "ATTACHED_ORDER_LEVEL_ERROR") for _ in range(3)]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is not None
    assert ev.detail["symbol"] == "NATGAS"
    assert ev.detail["error"] == "risk_block:ATTACHED_ORDER_LEVEL_ERROR"


def test_old_risk_blocks_outside_window_ignored():
    """Risk blocks older than window_hours must not count."""
    events = [
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD", age_minutes=60 * 10)
        for _ in range(5)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=4.0)
    assert ev is None


def test_recent_window_kept_old_dropped_mixed():
    """3 stale + 2 fresh — neither bucket reaches threshold."""
    events = (
        [_risk_block("WTI", "X", age_minutes=60 * 10) for _ in range(3)]
        + [_risk_block("WTI", "X", age_minutes=5) for _ in range(2)]
    )
    ev = repeated_execution_errors(recent_events=events, threshold=3, window_hours=4.0)
    assert ev is None


def test_risk_block_with_missing_reason_ignored():
    events = [
        {"event": "risk_block", "timestamp": datetime.now(timezone.utc).isoformat(),
         "data": {"symbol": "WTI"}}
        for _ in range(5)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None


def test_risk_block_with_missing_symbol_ignored():
    events = [
        {"event": "risk_block", "timestamp": datetime.now(timezone.utc).isoformat(),
         "data": {"reason": "STOP_TOO_TIGHT_VS_SPREAD"}}
        for _ in range(5)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None


def test_risk_block_with_non_dict_data_does_not_crash():
    events = [
        {"event": "risk_block", "timestamp": datetime.now(timezone.utc).isoformat(),
         "data": "garbage"}
    ]
    # Should silently skip
    ev = repeated_execution_errors(recent_events=events, threshold=1)
    assert ev is None


# --- mixed streams ----------------------------------------------------


def test_mixed_execution_failures_and_risk_blocks_combine_per_key():
    """Execution failures keyed by error-string and risk_block events keyed
    by 'risk_block:<reason>' use SEPARATE keys, so they don't combine."""
    now = datetime.now(timezone.utc)
    events = [
        _exec_event(now.isoformat(), [_failed_result("WTI", "SIZE_INCREMENT")]),
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD"),
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD"),
        _risk_block("WTI", "STOP_TOO_TIGHT_VS_SPREAD"),
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    # The 3 risk_blocks alone hit threshold
    assert ev is not None
    assert ev.detail["symbol"] == "WTI"
    assert "STOP_TOO_TIGHT_VS_SPREAD" in ev.detail["error"]


def test_execution_failures_alone_still_fire_unchanged():
    """Regression: pure execution.error stream still fires (existing behaviour)."""
    now = datetime.now(timezone.utc)
    events = [
        _exec_event(now.isoformat(), [_failed_result("WTI", "ATTACHED_ORDER_LEVEL_ERROR")])
        for _ in range(3)
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is not None
    assert ev.detail["symbol"] == "WTI"
    assert ev.detail["error"] == "ATTACHED_ORDER_LEVEL_ERROR"


def test_unrelated_event_types_ignored():
    """Events of type 'cycle_start', 'data_quality' etc. must not count."""
    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"event": "cycle_start", "timestamp": now, "data": {"symbol": "WTI"}},
        {"event": "data_quality", "timestamp": now, "data": {"symbol": "WTI"}},
        {"event": "decision", "timestamp": now, "data": {"symbol": "WTI"}},
    ]
    ev = repeated_execution_errors(recent_events=events, threshold=3)
    assert ev is None
