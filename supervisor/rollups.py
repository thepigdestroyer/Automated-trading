from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from supervisor.journal import iter_events


def compute_daily_rollup(journal_path: Path, *, date: str) -> dict[str, Any]:
    """Aggregate trade_close events on `date` (YYYY-MM-DD) into a daily rollup record."""
    trades = 0
    wins = 0
    losses = 0
    net = 0.0
    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})

    for ev in iter_events(journal_path):
        if ev.get("type") != "trade_close":
            continue
        ts = ev.get("ts", "")
        if not ts.startswith(date):
            continue
        pnl = float(ev.get("pnl_pct", 0.0))
        sym = ev.get("symbol", "?")
        trades += 1
        net += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        by_symbol[sym]["n"] += 1
        by_symbol[sym]["pnl"] += pnl

    win_rate = wins / trades if trades else 0.0
    return {
        "type": "daily_rollup",
        "date": date,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_pnl_pct": net,
        "by_symbol": {k: dict(v) for k, v in by_symbol.items()},
    }
