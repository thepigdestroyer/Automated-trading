"""IG bridge close-path diagnostic dump (review 2026-05-05-adhoc-1 #4 β scope).

The 2026-05-03 fix #4 added a diagnostic WARN inside ig_bridge.execute()
gated on the synchronous-confirmation REJECTED status (lines 209-217 of
ig_bridge.py). But the production failures actually return HTTP 4xx in
``session_manager._handle`` (raised as RuntimeError) which bypasses that
branch entirely.

This test suite exercises the new try/except wrapper around the
``_request("DELETE", ...)`` call: when the wrapped RuntimeError contains
``validation.null-not-allowed`` (case-insensitive), a CLOSE-specific
diagnostic must be emitted with the dealId so operators can correlate
to a specific position. The error must then be re-raised unchanged.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.config import Settings
from trading_bot.ig_bridge import IGRestBridge
from trading_bot.models import ExecutionPlan


def _make_bridge() -> IGRestBridge:
    settings = Settings().model_copy(
        update={
            "broker": "ig",
            "ig_username": "u",
            "ig_password": "p",
            "ig_api_key": "k",
        }
    )
    bridge = IGRestBridge(settings)
    bridge.current_account_id = "DEMO123"  # bypass connect()
    # Pre-populate a position cache entry so _build_close_payload works.
    bridge._position_cache["DI_BRENT_001"] = {
        "direction": "BUY",
        "size": 1.0,
        "expiry": "-",
    }
    return bridge


def _close_plan() -> ExecutionPlan:
    return ExecutionPlan(
        symbol="BRENT",
        action="CLOSE",
        confidence=0.72,
        reason="test close",
        position_id="DI_BRENT_001",
        target_price=113.0,
    )


def test_close_position_null_violation_emits_diagnostic_and_reraises(caplog):
    """The exact 2026-04-30 / 2026-05-01 / 2026-05-05 production failure
    pattern: DELETE /positions/otc returns
    ``RuntimeError("IG API error: validation.null-not-allowed.request")``
    from session_manager._handle.

    The wrapper must:
      1. Emit a CLOSE-specific WARN with symbol, dealId, payload keys,
         field types, null fields.
      2. Re-raise the original RuntimeError so the outer execute()
         try/except converts it to ``ExecutionResult(status='failed',
         error='IG API error: ...')`` with the original message intact.
    """
    bridge = _make_bridge()
    plan = _close_plan()

    def fake_request(method, path, **kwargs):
        # Mirror the production failure from session_manager._handle
        raise RuntimeError("IG API error: validation.null-not-allowed.request")

    with patch.object(bridge, "_request", side_effect=fake_request):
        with caplog.at_level(logging.WARNING, logger="trading_bot.ig_bridge"):
            result = bridge.execute(plan, dry_run=False, cycle_id="c_test")

    # The outer execute() try/except converts our re-raised RuntimeError
    # into a status=failed ExecutionResult. The error message must be
    # preserved unchanged so callers that match on
    # ``validation.null-not-allowed.request`` keep working.
    assert result.status == "failed"
    assert "validation.null-not-allowed.request" in (result.error or "")

    # Close-specific diagnostic emitted exactly once.
    diag_logs = [
        rec for rec in caplog.records
        if "ig_bridge close validation.null diagnostic" in rec.getMessage()
    ]
    assert len(diag_logs) == 1, (
        f"expected exactly 1 close-specific diagnostic log; got {len(diag_logs)}"
    )
    msg = diag_logs[0].getMessage()
    assert "symbol=BRENT" in msg
    assert "dealId='DI_BRENT_001'" in msg
    # The payload built by _build_close_payload contains: dealId,
    # direction, orderType, size, expiry, timeInForce
    assert "'dealId'" in msg
    assert "'orderType'" in msg


def test_close_position_other_error_does_not_emit_diagnostic(caplog):
    """A non-null-violation RuntimeError (e.g. timeout, connection
    error, generic API failure) must NOT emit the close-specific
    diagnostic — that line is reserved for the validation.null pattern.
    """
    bridge = _make_bridge()
    plan = _close_plan()

    def fake_request(method, path, **kwargs):
        raise RuntimeError("IG API error: timeout")

    with patch.object(bridge, "_request", side_effect=fake_request):
        with caplog.at_level(logging.WARNING, logger="trading_bot.ig_bridge"):
            result = bridge.execute(plan, dry_run=False, cycle_id="c_test")

    assert result.status == "failed"
    assert "timeout" in (result.error or "")

    diag_logs = [
        rec for rec in caplog.records
        if "ig_bridge close validation.null diagnostic" in rec.getMessage()
    ]
    assert len(diag_logs) == 0, "diagnostic must not fire on non-null errors"


def test_close_position_null_violation_case_insensitive(caplog):
    """The match on 'validation.null-not-allowed' must be
    case-insensitive — IG might return upper/lower-case variants."""
    bridge = _make_bridge()
    plan = _close_plan()

    def fake_request(method, path, **kwargs):
        raise RuntimeError("IG API error: VALIDATION.NULL-NOT-ALLOWED.REQUEST")

    with patch.object(bridge, "_request", side_effect=fake_request):
        with caplog.at_level(logging.WARNING, logger="trading_bot.ig_bridge"):
            result = bridge.execute(plan, dry_run=False, cycle_id="c_test")

    assert result.status == "failed"
    diag_logs = [
        rec for rec in caplog.records
        if "ig_bridge close validation.null diagnostic" in rec.getMessage()
    ]
    assert len(diag_logs) == 1


def test_close_position_diagnostic_failure_does_not_mask_original_error(caplog):
    """If the diagnostic dump itself raises (e.g. because of a malformed
    payload), the wrapper must still re-raise the ORIGINAL RuntimeError
    — operators care about the broker error, not the diagnostic.
    The outer execute() then converts to status=failed with the
    original message.
    """
    bridge = _make_bridge()
    plan = _close_plan()

    def fake_request(method, path, **kwargs):
        raise RuntimeError("IG API error: validation.null-not-allowed.request")

    # Force the diagnostic logger.warning to raise — simulates a
    # logging-handler misconfiguration. The wrapper's inner try/except
    # must swallow this so the original RuntimeError is the visible one
    # (caught by execute()'s outer try/except).
    with patch.object(bridge, "_request", side_effect=fake_request):
        with patch(
            "trading_bot.ig_bridge.LOGGER.warning",
            side_effect=RuntimeError("logger broken"),
        ):
            result = bridge.execute(plan, dry_run=False, cycle_id="c_test")

    # The original error wins, not the logger-failure error
    assert result.status == "failed"
    assert "validation.null-not-allowed.request" in (result.error or "")
    assert "logger broken" not in (result.error or "")
