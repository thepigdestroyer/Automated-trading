"""Tests for the forward-walk paper-eval statistics (Phase 3 pivot).

Pure-function tests: no I/O, no network. The eval consumes paper_pnl closed
rows + decision_records.jsonl rows and must answer, with honest uncertainty:
does this strategy have positive expectancy, and is the LLM's confidence
calibrated?
"""

from __future__ import annotations

import json

from trading_bot.forward_eval import (
    MIN_INDEPENDENT_TRADES,
    bootstrap_expectancy_ci,
    brier_score,
    build_report,
    calibration_table,
    wilson_ci,
)


# --- wilson_ci ---------------------------------------------------------


def test_wilson_ci_zero_n():
    lo, hi = wilson_ci(0, 0)
    assert (lo, hi) == (0.0, 1.0)  # no data -> maximal uncertainty


def test_wilson_ci_known_value():
    # 7/10 wins, z=1.96: Wilson interval ~ (0.397, 0.892)
    lo, hi = wilson_ci(7, 10)
    assert 0.39 < lo < 0.41
    assert 0.88 < hi < 0.90


def test_wilson_ci_bounds():
    lo, hi = wilson_ci(10, 10)
    assert 0.0 <= lo <= hi <= 1.0


# --- bootstrap expectancy ----------------------------------------------


def test_bootstrap_expectancy_empty():
    mean, lo, hi = bootstrap_expectancy_ci([], seed=1)
    assert mean == 0.0 and lo == 0.0 and hi == 0.0


def test_bootstrap_expectancy_deterministic_with_seed():
    rs = [1.0, -1.0, 2.0, -0.5, 1.5]
    a = bootstrap_expectancy_ci(rs, seed=42)
    b = bootstrap_expectancy_ci(rs, seed=42)
    assert a == b


def test_bootstrap_expectancy_sign():
    # All winners -> CI strictly positive; all losers -> strictly negative.
    mean_w, lo_w, _ = bootstrap_expectancy_ci([1.0, 2.0, 1.5, 0.5], seed=7)
    assert mean_w > 0 and lo_w > 0
    mean_l, _, hi_l = bootstrap_expectancy_ci([-1.0, -2.0, -1.5, -0.5], seed=7)
    assert mean_l < 0 and hi_l < 0


# --- brier + calibration ------------------------------------------------


def test_brier_perfect_and_worst():
    assert brier_score([(1.0, True), (0.0, False)]) == 0.0
    assert brier_score([(0.0, True), (1.0, False)]) == 1.0


def test_brier_uninformative_half():
    assert abs(brier_score([(0.5, True), (0.5, False)]) - 0.25) < 1e-9


def test_brier_empty_is_none():
    assert brier_score([]) is None


def test_calibration_table_bins():
    pairs = [(0.65, True), (0.65, False), (0.85, True), (0.85, True)]
    table = calibration_table(pairs)
    by_bin = {row["bin"]: row for row in table}
    assert by_bin["0.6-0.7"]["n"] == 2
    assert by_bin["0.6-0.7"]["win_rate"] == 0.5
    assert by_bin["0.8-0.9"]["n"] == 2
    assert by_bin["0.8-0.9"]["win_rate"] == 1.0


# --- build_report -------------------------------------------------------


def _closed_row(symbol="GOLD", r=1.0, win=True, decision_id="c1:GOLD", conf=0.8):
    return {
        "symbol": symbol, "r_multiple": r, "win": win,
        "decision_id": decision_id, "confidence": conf,
        "reason": "tp" if win else "stop",
    }


def _record_row(decision_id="c1:GOLD", prompt_sha="abc123", confidence=0.8):
    return {
        "decision_id": decision_id, "prompt_sha": prompt_sha,
        "confidence": confidence, "action": "BUY",
    }


def test_build_report_empty():
    rep = build_report([], [])
    assert rep["n_trades"] == 0
    assert rep["verdict"] == "insufficient_data"
    assert rep["progress"] == f"0/{MIN_INDEPENDENT_TRADES}"


def test_build_report_counts_and_joins():
    closed = [
        _closed_row(r=2.0, win=True, decision_id="c1:GOLD"),
        _closed_row(r=-1.0, win=False, decision_id="c2:GOLD", conf=0.7),
        _closed_row(symbol="BRENT", r=1.0, win=True, decision_id="c3:BRENT"),
    ]
    records = [
        _record_row("c1:GOLD"), _record_row("c2:GOLD", confidence=0.7),
        _record_row("c3:BRENT"),
    ]
    rep = build_report(closed, records)
    assert rep["n_trades"] == 3
    assert rep["wins"] == 2
    assert 0 < rep["win_rate_ci"][0] < rep["win_rate"] < rep["win_rate_ci"][1] <= 1.0
    assert rep["expectancy_r"] > 0
    assert rep["brier"] is not None
    assert rep["per_symbol"]["GOLD"]["n"] == 2
    assert rep["per_symbol"]["BRENT"]["n"] == 1
    assert rep["verdict"] == "insufficient_data"  # n < 60


def test_build_report_prompt_drift_flag():
    closed = [_closed_row(decision_id="c1:GOLD"), _closed_row(decision_id="c2:GOLD")]
    records = [
        _record_row("c1:GOLD", prompt_sha="aaa"),
        _record_row("c2:GOLD", prompt_sha="bbb"),
    ]
    rep = build_report(closed, records)
    assert rep["prompt_versions"] == 2
    assert rep["prompt_drift_warning"] is True


def test_build_report_verdict_requires_min_n_and_positive_ci():
    # 70 trades, strongly positive -> verdict positive_expectancy.
    closed = [
        _closed_row(r=1.5 if i % 3 else -1.0, win=bool(i % 3),
                    decision_id=f"c{i}:GOLD")
        for i in range(70)
    ]
    records = [_record_row(f"c{i}:GOLD") for i in range(70)]
    rep = build_report(closed, records)
    assert rep["n_trades"] == 70
    assert rep["verdict"] in {"positive_expectancy", "indeterminate"}
    # With 2/3 winners at +1.5 vs -1.0 the bootstrap CI must clear zero.
    assert rep["verdict"] == "positive_expectancy"


def test_build_report_negative_verdict():
    closed = [
        _closed_row(r=-1.0, win=False, decision_id=f"c{i}:GOLD")
        for i in range(70)
    ]
    records = [_record_row(f"c{i}:GOLD") for i in range(70)]
    rep = build_report(closed, records)
    assert rep["verdict"] == "negative_expectancy"


def test_report_is_json_serialisable():
    rep = build_report([_closed_row()], [_record_row()])
    json.dumps(rep)  # must not raise
