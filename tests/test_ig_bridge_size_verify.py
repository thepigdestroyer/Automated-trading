"""Per review 2026-05-15-adhoc-1 #6.1 — STRUCTURAL_MIN_DEAL_SIZE_BREACH.

When IG's minDealSize is LARGER than the budget-correct raw quantity (e.g.
BRENT min=1.0 vs raw=0.0275 at current equity), `_normalize_size` rounds
UP to min_deal_size, inflating the cash-risk by min_deal_size / raw_size.
The downstream SIZE_VS_BUDGET_BREACH guard then rejects the trade.

These tests verify:
  1. `_normalize_size` emits a MIN_DEAL_SIZE_INFLATION warning when
     raw_size < min_deal_size.
  2. The SIZE_VS_BUDGET_BREACH error message uses the distinct
     STRUCTURAL_MIN_DEAL_SIZE_BREACH code when the cause was
     upward inflation by the broker minimum.
  3. The same path still uses the legacy SIZE_VS_BUDGET_BREACH code
     when raw_size >= min_deal_size (no inflation).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

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
    bridge.current_account_id = "DEMO123"
    return bridge


# ---------------------------------------------------------------------------
# _normalize_size — MIN_DEAL_SIZE_INFLATION warning
# ---------------------------------------------------------------------------


def test_normalize_size_logs_inflation_when_raw_below_min(caplog):
    """BRENT-like case: raw=0.0275, min=1.0 → forced to 1.0 (36.4x)."""
    bridge = _make_bridge()
    dealing_rules = {"minDealSize": {"value": 1.0}}

    with caplog.at_level(logging.WARNING, logger="trading_bot.ig_bridge"):
        result = bridge._normalize_size(0.0275, dealing_rules)

    assert result == 1.0
    inflation_logs = [
        rec for rec in caplog.records
        if "MIN_DEAL_SIZE_INFLATION" in rec.getMessage()
    ]
    assert len(inflation_logs) == 1, (
        f"expected exactly 1 MIN_DEAL_SIZE_INFLATION log; got {len(inflation_logs)}"
    )
    msg = inflation_logs[0].getMessage()
    assert "symbol_min=1.0" in msg
    assert "raw=0.027500" in msg
    assert "forced_to=1.0" in msg
    # 1.0 / 0.0275 = 36.36...
    assert "inflation_ratio=36" in msg


def test_normalize_size_no_inflation_log_when_raw_above_min(caplog):
    """WTI-like case: raw=0.0295, min=0.01 → 0.02, no inflation."""
    bridge = _make_bridge()
    dealing_rules = {"minDealSize": {"value": 0.01}}

    with caplog.at_level(logging.WARNING, logger="trading_bot.ig_bridge"):
        result = bridge._normalize_size(0.0295, dealing_rules)

    assert result == pytest.approx(0.02)
    inflation_logs = [
        rec for rec in caplog.records
        if "MIN_DEAL_SIZE_INFLATION" in rec.getMessage()
    ]
    assert len(inflation_logs) == 0, "no inflation log expected when raw >= min"


def test_normalize_size_no_inflation_log_when_min_is_zero(caplog):
    """If dealing-rules min is unset/zero, the function early-returns and
    must not log MIN_DEAL_SIZE_INFLATION."""
    bridge = _make_bridge()
    dealing_rules = {"minDealSize": {"value": 0.0}}

    with caplog.at_level(logging.WARNING, logger="trading_bot.ig_bridge"):
        result = bridge._normalize_size(0.0275, dealing_rules)

    assert result == pytest.approx(0.0275)
    inflation_logs = [
        rec for rec in caplog.records
        if "MIN_DEAL_SIZE_INFLATION" in rec.getMessage()
    ]
    assert len(inflation_logs) == 0


# ---------------------------------------------------------------------------
# SIZE_VS_BUDGET_BREACH vs STRUCTURAL_MIN_DEAL_SIZE_BREACH error codes
# ---------------------------------------------------------------------------


def _market_with_min(min_deal: float, price: float = 64.0) -> dict:
    """Build a minimal IG-market dict shaped for _build_open_payload."""
    return {
        "snapshot": {"marketStatus": "TRADEABLE", "offer": price, "bid": price},
        "instrument": {"expiry": "-", "currencies": [{"code": "GBP", "isDefault": True}]},
        "dealingRules": {
            "minDealSize": {"value": min_deal},
            "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": 1.0},
        },
    }


def _patch_market(bridge: IGRestBridge, epic: str, market: dict):
    """Patch _resolve_epic and the inner _request to return a canned market dict."""
    return (
        patch.object(bridge, "_resolve_epic", return_value=epic),
        patch.object(bridge, "_request", return_value=market),
    )


def test_size_vs_budget_breach_uses_structural_code_when_min_inflates_up():
    """BRENT-like: raw=0.0275, min=1.0, stop=97.93 → cash_risk £97.93
    on size 1.0 vs budget ceiling ~£3.37 → STRUCTURAL_MIN_DEAL_SIZE_BREACH."""
    bridge = _make_bridge()
    market = _market_with_min(min_deal=1.0, price=64.0)
    plan = ExecutionPlan(
        symbol="BRENT",
        action="BUY",
        confidence=0.72,
        reason="test",
        quantity=0.0275,
        stop_distance=97.93,
    )

    epic_patch, req_patch = _patch_market(bridge, "CC.D.LCO.UMP.IP", market)
    with epic_patch, req_patch:
        with pytest.raises(RuntimeError) as excinfo:
            bridge._build_open_payload(plan, cycle_id="c_test")

    msg = str(excinfo.value)
    assert "STRUCTURAL_MIN_DEAL_SIZE_BREACH" in msg
    # Distinct from the legacy code — must not double-name
    assert "SIZE_VS_BUDGET_BREACH:" not in msg
    assert "raw_qty=0.0275" in msg


def test_size_vs_budget_breach_passes_when_size_equals_raw():
    """When raw_size >= min_deal_size, normalization rounds DOWN so
    size <= raw_size. cash_risk_at_stop <= target_cash_risk < budget_ceiling.
    No breach should fire; trade succeeds normally. This verifies the new
    `size > raw_size` discriminator doesn't accidentally break the
    no-inflation path."""
    bridge = _make_bridge()
    market = _market_with_min(min_deal=0.5, price=64.0)
    plan = ExecutionPlan(
        symbol="GOLD",
        action="BUY",
        confidence=0.7,
        reason="test",
        quantity=2.5,
        stop_distance=1.0,
    )

    epic_patch, req_patch = _patch_market(bridge, "CS.D.GOLD.IP", market)
    with epic_patch, req_patch:
        payload = bridge._build_open_payload(plan, cycle_id="c_test")

    # raw=2.5, min=0.5 → units=5, size=2.5; size == raw so no breach.
    assert payload["size"] == pytest.approx(2.5)


def test_structural_breach_error_message_contains_diagnostic_fields():
    """The error message must carry enough context (raw_qty, stop,
    point_value) for forensic reconstruction and LLM pattern-matching."""
    bridge = _make_bridge()
    market = _market_with_min(min_deal=1.0, price=64.0)
    plan = ExecutionPlan(
        symbol="BRENT",
        action="BUY",
        confidence=0.72,
        reason="test",
        quantity=0.0275,
        stop_distance=97.93,
    )

    epic_patch, req_patch = _patch_market(bridge, "CC.D.LCO.UMP.IP", market)
    with epic_patch, req_patch:
        with pytest.raises(RuntimeError) as excinfo:
            bridge._build_open_payload(plan, cycle_id="c_test")

    msg = str(excinfo.value)
    # Forensic fields required by the supervisor watcher and the LLM bullet
    assert "STRUCTURAL_MIN_DEAL_SIZE_BREACH" in msg
    assert "raw_qty=" in msg
    assert "stop=" in msg
    assert "point_value=" in msg
    assert "Refusing trade" in msg
