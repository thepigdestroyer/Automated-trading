"""Cross-asset correlation context (Phase 1.3 roadmap).

The bot previously only had a coarse string-equality ``correlation_group``
test (portfolio.py): two energy positions in group ``energy_oil`` were caught,
but a USD-index long + EURUSD short (functionally the same macro bet) slipped
through because they sit in different groups. This module computes REAL
rolling-return correlations across a fixed macro reference basket so the LLM
can reason about genuine cross-asset exposure and, more valuably, about
DECOUPLING — when a normally-correlated pair breaks its relationship, which
often precedes a directional move in the laggard.

Reuses the existing 60m Yahoo pipeline (no new deps / cost). All Yahoo calls
are best-effort: a basket member that fails to download is simply dropped
from the matrix rather than failing the cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

from trading_bot.config import Settings
from trading_bot.models import CorrelationContext, CorrelationPair

LOGGER = logging.getLogger(__name__)

# Fixed macro reference basket. Keys are human-readable; values are Yahoo
# tickers. Chosen to span the dollar, equities, vol, rates proxy, metals and
# energy — the axes that explain most cross-asset moves the bot trades around.
REFERENCE_BASKET: dict[str, str] = {
    "DXY": "DX-Y.NYB",   # US dollar index
    "SPX": "ES=F",        # S&P 500 futures (risk-on/off)
    "VIX": "^VIX",        # equity vol
    "GOLD": "GC=F",       # gold futures
    "WTI": "CL=F",        # crude oil
    "USDJPY": "JPY=X",    # carry / rates proxy
    "UST10Y": "^TNX",     # 10y treasury yield
}

# Short window = recent correlation; long window = baseline. A pair whose
# long-window |corr| is "strong" but whose short-window |corr| has collapsed
# is flagged decoupled.
_SHORT_WINDOW = 30
_LONG_WINDOW = 90
_NOTABLE_THRESHOLD = 0.5      # |corr| above this is worth surfacing
_BASELINE_STRONG = 0.6        # baseline |corr| that defines "normally correlated"
_DECOUPLE_DROP = 0.35         # short-window |corr| below this = decoupled
_MAX_PAIRS = 12


class CorrelationContextClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self) -> CorrelationContext:
        as_of = datetime.now(timezone.utc).isoformat()
        if not getattr(self.settings, "correlation_context_enabled", True):
            return CorrelationContext(as_of=as_of, note="disabled")

        closes = self._fetch_basket_closes()
        if len(closes.columns) < 2 or len(closes) < _SHORT_WINDOW + 2:
            return CorrelationContext(
                as_of=as_of,
                note=f"insufficient data ({len(closes.columns)} assets, "
                     f"{len(closes)} bars)",
            )
        returns = closes.pct_change().dropna(how="all")
        return build_correlation_context(returns, as_of=as_of)

    def _fetch_basket_closes(self) -> pd.DataFrame:
        """Download aligned close series for the reference basket. Members
        that fail are dropped. Returns a DataFrame indexed by timestamp with
        one column per successfully-fetched basket key."""
        series: dict[str, pd.Series] = {}
        interval = "60m"
        for name, ticker in REFERENCE_BASKET.items():
            try:
                frame = yf.download(
                    tickers=ticker,
                    period="30d",
                    interval=interval,
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if frame.empty:
                    continue
                if isinstance(frame.columns, pd.MultiIndex):
                    frame.columns = frame.columns.get_level_values(0)
                close = frame["Close"].astype(float).dropna()
                if not close.empty:
                    series[name] = close
            except Exception as exc:  # pragma: no cover - network path
                LOGGER.warning("correlation basket fetch failed for %s: %s", name, exc)
        if not series:
            return pd.DataFrame()
        # Align on a common index (outer join then forward-fill small gaps).
        combined = pd.DataFrame(series).sort_index()
        return combined.ffill().dropna(how="all")


def build_correlation_context(
    returns: pd.DataFrame,
    *,
    as_of: str,
) -> CorrelationContext:
    """Pure: build a CorrelationContext from a returns DataFrame (one column
    per asset). Computes short-window correlations for the notable-pairs list
    and compares against a long-window baseline to flag decoupling."""
    cols = list(returns.columns)
    short = returns.tail(_SHORT_WINDOW)
    long = returns.tail(_LONG_WINDOW)

    short_corr = short.corr() if len(short) >= 3 else pd.DataFrame()
    long_corr = long.corr() if len(long) >= 3 else pd.DataFrame()

    notable: list[CorrelationPair] = []
    decoupled: list[CorrelationPair] = []

    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            s = _safe_corr(short_corr, a, b)
            if s is None:
                continue
            pair = CorrelationPair(a=a, b=b, corr=round(s, 3))
            if abs(s) >= _NOTABLE_THRESHOLD:
                notable.append(pair)
            base = _safe_corr(long_corr, a, b)
            if (
                base is not None
                and abs(base) >= _BASELINE_STRONG
                and abs(s) <= _DECOUPLE_DROP
            ):
                pair_dc = CorrelationPair(a=a, b=b, corr=round(s, 3), decoupled=True)
                decoupled.append(pair_dc)

    # Rank notable by |corr| desc; cap.
    notable.sort(key=lambda p: abs(p.corr), reverse=True)
    notable = notable[:_MAX_PAIRS]

    note_parts = []
    if decoupled:
        names = ", ".join(f"{p.a}/{p.b}" for p in decoupled)
        note_parts.append(f"decoupled: {names}")
    note = "; ".join(note_parts) if note_parts else None

    return CorrelationContext(
        as_of=as_of,
        window_bars=int(len(short)),
        notable_pairs=notable,
        decoupled_pairs=decoupled,
        note=note,
    )


def _safe_corr(corr: pd.DataFrame, a: str, b: str) -> float | None:
    if corr.empty or a not in corr.index or b not in corr.columns:
        return None
    val = corr.loc[a, b]
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    return float(val)
