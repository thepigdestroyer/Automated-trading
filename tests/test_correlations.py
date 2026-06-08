"""Tests for the Phase 1.3 cross-asset correlation context."""

from __future__ import annotations

import numpy as np
import pandas as pd

from trading_bot.correlations import build_correlation_context
from trading_bot.models import CorrelationContext


def _returns_frame(spec: dict[str, np.ndarray]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(next(iter(spec.values()))), freq="h")
    return pd.DataFrame(spec, index=idx)


def test_positively_correlated_pair_is_notable():
    n = 120
    base = np.random.RandomState(1).normal(0, 0.01, n)
    df = _returns_frame({
        "A": base,
        "B": base + np.random.RandomState(2).normal(0, 0.001, n),  # ~identical
    })
    ctx = build_correlation_context(df, as_of="t")
    assert isinstance(ctx, CorrelationContext)
    assert len(ctx.notable_pairs) == 1
    assert ctx.notable_pairs[0].corr > 0.8


def test_negatively_correlated_pair_is_notable():
    n = 120
    base = np.random.RandomState(3).normal(0, 0.01, n)
    df = _returns_frame({"A": base, "B": -base})
    ctx = build_correlation_context(df, as_of="t")
    assert len(ctx.notable_pairs) == 1
    assert ctx.notable_pairs[0].corr < -0.8


def test_uncorrelated_pair_not_notable():
    n = 120
    df = _returns_frame({
        "A": np.random.RandomState(4).normal(0, 0.01, n),
        "B": np.random.RandomState(5).normal(0, 0.01, n),
    })
    ctx = build_correlation_context(df, as_of="t")
    # Independent random series → |corr| should be well below 0.5 threshold.
    assert ctx.notable_pairs == []


def test_decoupling_detected():
    # First 90 bars: A and B move together (strong baseline). Last 30 bars:
    # B goes independent → short-window corr collapses → decoupled flag.
    rng = np.random.RandomState(7)
    base_long = rng.normal(0, 0.01, 90)
    a = np.concatenate([base_long, rng.normal(0, 0.01, 30)])
    b = np.concatenate([base_long + rng.normal(0, 0.0005, 90),
                        rng.normal(0, 0.01, 30)])  # last 30 independent
    df = _returns_frame({"A": a, "B": b})
    ctx = build_correlation_context(df, as_of="t")
    assert any(p.a == "A" and p.b == "B" for p in ctx.decoupled_pairs)
    assert ctx.note is not None and "decoupled" in ctx.note


def test_still_coupled_not_flagged_decoupled():
    n = 120
    base = np.random.RandomState(8).normal(0, 0.01, n)
    df = _returns_frame({"A": base, "B": base + np.random.RandomState(9).normal(0, 0.0005, n)})
    ctx = build_correlation_context(df, as_of="t")
    assert ctx.decoupled_pairs == []


def test_notable_pairs_capped_and_sorted():
    # 8 assets → 28 pairs; all strongly correlated to a common factor so many
    # exceed threshold. Must cap at _MAX_PAIRS (12) and be sorted desc by |corr|.
    n = 120
    factor = np.random.RandomState(10).normal(0, 0.01, n)
    spec = {}
    for i in range(8):
        noise = np.random.RandomState(100 + i).normal(0, 0.002, n)
        spec[f"S{i}"] = factor + noise
    df = _returns_frame(spec)
    ctx = build_correlation_context(df, as_of="t")
    assert len(ctx.notable_pairs) <= 12
    corrs = [abs(p.corr) for p in ctx.notable_pairs]
    assert corrs == sorted(corrs, reverse=True)


def test_empty_returns_safe():
    ctx = build_correlation_context(pd.DataFrame(), as_of="t")
    assert ctx.notable_pairs == []
    assert ctx.decoupled_pairs == []
