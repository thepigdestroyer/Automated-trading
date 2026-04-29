"""Pre-submit stop-distance vs spread sanity gate (incident 2026-04-29).

Three WTI BUYs were submitted with stop_distance=12 on a market with
roughly 4–8 unit spread. Every position got chewed up by entry slippage
and was stopped out within ~70s, for a net loss of about £35 plus
commission.  This new gate rejects such plans BEFORE the IG submit so
the bot never gives those trades to the broker.
"""
from __future__ import annotations

import json

import pytest

from trading_bot.config import Settings
from trading_bot.ig_bridge import IGRestBridge
from trading_bot.models import ExecutionPlan


def _make_bridge(tmp_path, **overrides) -> IGRestBridge:
    json_log = tmp_path / "events.jsonl"
    settings = Settings(
        broker="paper",
        json_log_path=json_log,
        runtime_state_path=tmp_path / "rs.json",
        decision_log_dir=tmp_path / "logs",
        alerts_dir=tmp_path / "alerts",
        paper_pnl_path=tmp_path / "pp.json",
        **overrides,
    )
    bridge = IGRestBridge(settings)
    bridge.current_account_id = "DEMO123"  # bypass connect()
    return bridge


def _events(path) -> list[dict]:
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


def _plan(*, action="BUY", stop_distance=12.0):
    return ExecutionPlan(
        symbol="WTI",
        action=action,
        confidence=0.8,
        reason="test",
        quantity=2.9,
        target_price=10350.5,
        stop_distance=stop_distance,
        take_profit_distance=12.0,
    )


def _market(*, bid=10346.5, offer=10354.5, ig_min=4.0):
    """Mimic the IG /markets/{epic} v3 response shape."""
    return {
        "snapshot": {"bid": bid, "offer": offer, "marketStatus": "TRADEABLE"},
        "instrument": {"expiry": "-"},
        "dealingRules": {
            "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": ig_min},
        },
    }


# --- direct unit tests of _check_stop_vs_spread ---------------------------


def test_stop_too_tight_vs_spread_blocks(tmp_path):
    """stop_distance=10 with spread=8 (× 2.0 multiplier) → required 16, blocked."""
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)
    plan = _plan(stop_distance=10.0)
    market = _market(bid=10346.5, offer=10354.5, ig_min=4.0)  # spread=8

    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=8.0,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is not None
    assert result.status == "failed"
    assert "STOP_TOO_TIGHT_VS_SPREAD" in (result.error or "")


def test_wide_stop_passes(tmp_path):
    """stop_distance=20 with spread=8 → required 16, allowed."""
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)
    plan = _plan(stop_distance=20.0)
    market = _market(bid=10346.5, offer=10354.5, ig_min=4.0)

    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=8.0,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is None


def test_below_ig_minimum_blocks_independently_of_spread(tmp_path):
    """Even with multiplier=0 (gate-by-spread disabled), the IG minimum
    still rejects plans below it."""
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=0.0)
    plan = _plan(stop_distance=2.0)
    market = _market(bid=10350, offer=10350.4, ig_min=10.0)  # spread=0.4

    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=0.4,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is not None
    assert result.status == "failed"
    assert "STOP_TOO_TIGHT_VS_SPREAD" in (result.error or "")
    assert "ig_min=10" in (result.error or "")


def test_disabled_when_multiplier_zero_and_no_ig_min(tmp_path):
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=0.0)
    plan = _plan(stop_distance=1.0)
    market = _market(bid=10346.5, offer=10354.5, ig_min=0.0)

    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=8.0,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is None


def test_close_plan_is_not_gated(tmp_path):
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)
    plan = ExecutionPlan(
        symbol="WTI", action="CLOSE", confidence=0.9, reason="close",
        target_price=10350.5, position_id="DI123",
    )
    market = _market()
    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=8.0,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is None


def test_no_stop_distance_is_not_gated(tmp_path):
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)
    plan = ExecutionPlan(
        symbol="WTI", action="BUY", confidence=0.9, reason="x",
        quantity=1.0, target_price=10350.5,
    )
    market = _market()
    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=8.0,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is None


def test_emits_risk_block_event_on_block(tmp_path):
    json_log = tmp_path / "events.jsonl"
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)
    plan = _plan(stop_distance=10.0)
    market = _market(bid=10346.5, offer=10354.5, ig_min=4.0)

    bridge._check_stop_vs_spread(
        plan,
        spread_abs=8.0,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    events = _events(json_log)
    risk_blocks = [e for e in events if e.get("event") == "risk_block"]
    assert len(risk_blocks) == 1
    rb = risk_blocks[0]
    assert rb.get("cycle_id") == "c_test"
    data = rb.get("data") or {}
    assert data.get("reason") == "STOP_TOO_TIGHT_VS_SPREAD"
    assert data.get("symbol") == "WTI"
    assert data.get("stop_distance") == 10.0
    assert data.get("required") == 16.0  # max(spread_floor=16, ig_min=4)


def test_percentage_unit_ig_minimum(tmp_path):
    """If IG returns minNormalStopOrLimitDistance.unit=PERCENTAGE, convert."""
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=0.0)
    plan = _plan(stop_distance=20.0)
    market = {
        "snapshot": {"bid": 10350, "offer": 10350.4, "marketStatus": "TRADEABLE"},
        "instrument": {"expiry": "-"},
        "dealingRules": {
            "minNormalStopOrLimitDistance": {"unit": "PERCENTAGE", "value": 1.0},
        },
    }
    # 1% of 10350.4 = 103.504 → stop=20 should be blocked.
    result = bridge._check_stop_vs_spread(
        plan,
        spread_abs=0.4,
        market=market,
        submitted_at="2026-04-29T10:00:00+00:00",
        cycle_id="c_test",
    )
    assert result is not None
    assert "STOP_TOO_TIGHT_VS_SPREAD" in (result.error or "")


# --- integration tests through _check_spread (the public-ish entry) -------


def test_check_spread_returns_stop_too_tight_block(tmp_path, monkeypatch):
    """Patched _request to return a synthetic market; the WTI scenario from
    today blocks the trade."""
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)

    market = _market(bid=10346.5, offer=10354.5, ig_min=4.0)  # spread=8

    def fake_request(method, path, **kwargs):
        if path.startswith("/markets/"):
            return market
        raise AssertionError(f"unexpected request: {path}")

    monkeypatch.setattr(bridge, "_request", fake_request)
    monkeypatch.setattr(bridge, "_resolve_epic", lambda symbol: "CC.D.CL.UMP.IP")

    plan = _plan(stop_distance=12.0)  # the actual today's value
    result = bridge._check_spread(plan, cycle_id="c_today")
    assert result is not None
    assert result.status == "failed"
    assert "STOP_TOO_TIGHT_VS_SPREAD" in (result.error or "")

    events = _events(bridge.settings.json_log_path)
    assert any(e.get("event") == "risk_block" for e in events)


def test_check_spread_passes_with_wide_stop(tmp_path, monkeypatch):
    bridge = _make_bridge(tmp_path, min_stop_multiplier_of_spread=2.0)
    market = _market(bid=10346.5, offer=10354.5, ig_min=4.0)

    def fake_request(method, path, **kwargs):
        return market

    monkeypatch.setattr(bridge, "_request", fake_request)
    monkeypatch.setattr(bridge, "_resolve_epic", lambda symbol: "CC.D.CL.UMP.IP")

    plan = _plan(stop_distance=24.0)  # > 2 × spread = 16, > ig_min = 4
    result = bridge._check_spread(plan, cycle_id="c_today")
    assert result is None
