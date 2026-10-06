"""Evaluate the 2026-10-06 trend-exit pre-registration from the backtest outputs.

Reads the CLI outputs of

    python -m trading_bot.backtest --years 5 --shorts --stop-mode daily_atr \
        --exit-mode channel_trail --costs {typical,max,zero} \
        --trades-out <dir>/trend-exit-trades-<costs>.csv > <dir>/trend-exit-5y-<costs>.json

and applies the pre-registered criterion literally (typical costs):
  1. full period: lower bound of the entry-week clustered 95% CI of net E[R] > 0;
  2. entries dated >= (last settled data date - 730 days): net E[R] point estimate > 0.
PASS only if both hold. Also prints the breakdowns the result note reports.

Usage: python scripts/eval_trend_exit_prereg.py [<dir>]   (default .superpowers/sdd)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from trading_bot.backtest import cluster_bootstrap_ci, entry_week

RECENT_DAYS = 730


def _ci(values, when) -> tuple[float, float, float]:
    return cluster_bootstrap_ci(list(values), [entry_week(t) for t in when])


def evaluate(directory: Path, costs: str) -> dict:
    report = json.loads((directory / f"trend-exit-5y-{costs}.json").read_text())
    meta = report["meta"]
    assert meta["exit_mode"] == "channel_trail" and meta["costs"] == costs and meta["allow_shorts"]
    full = report["daily_atr"]
    trades = pd.read_csv(directory / f"trend-exit-trades-{costs}.csv", float_precision="round_trip")
    trades["entry_at"] = pd.to_datetime(trades["entry_at"])
    trades["asset_class"] = trades["symbol"].map({s: d["asset_class"] for s, d in meta["data"].items()})
    assert len(trades) == full["trades"]

    _, lo, hi = _ci(trades["r_multiple"], trades["entry_at"])
    mean = sum(trades["r_multiple"]) / len(trades)  # _summarise's estimator
    # The CSV must reproduce the CLI's own numbers exactly.
    assert mean == full["expectancy_r"] and [lo, hi] == full["expectancy_r_ci_week"]

    last_data = max(pd.Timestamp(d["last_settled_bar"]) for d in meta["data"].values())
    cutoff = last_data.normalize() - pd.Timedelta(days=RECENT_DAYS)
    recent = trades[trades["entry_at"].dt.tz_localize(None).dt.normalize() >= cutoff]
    r_mean, r_lo, r_hi = _ci(recent["r_multiple"], recent["entry_at"]) if len(recent) else (0.0, 0.0, 0.0)

    rs = trades["r_multiple"].to_numpy()
    order = np.argsort(rs)[::-1]
    top = trades.iloc[order[:5]][["symbol", "side", "entry_at", "exit_at", "reason", "r_multiple"]]
    total = rs.sum()

    def without_top(k: int) -> float:
        return float(np.delete(rs, order[:k]).mean())

    by_class = trades.groupby("asset_class")["r_multiple"].agg(["count", "mean", "sum"])
    by_side = trades.groupby("side")["r_multiple"].agg(["count", "mean"])
    hold_days = trades["hold_hours"] / 24.0
    return {
        "costs": costs,
        "window_start": meta["window_start"],
        "last_data_date": str(last_data.date()),
        "recent_cutoff": str(cutoff.date()),
        "full": {
            "trades": int(full["trades"]),
            "entry_weeks": int(full["entry_weeks"]),
            "net_er": full["expectancy_r"],
            "net_er_ci_week": full["expectancy_r_ci_week"],
            "gross_er": full["expectancy_r_gross"],
            "gross_er_ci_week": full["expectancy_r_gross_ci_week"],
            "win_rate": full["win_rate"],
            "avg_win_r": full["avg_win_r"],
            "avg_loss_r": full["avg_loss_r"],
            "profit_factor_r": full["profit_factor_r"],
            "worst_r": full["worst_r"],
            "exit_reasons": full["exit_reasons"],
            "gap_exits": full["gap_exits"],
            "avg_stop_pct": full["avg_stop_pct"],
            "avg_hold_days": float(hold_days.mean()),
            "median_hold_days": float(hold_days.median()),
            "max_hold_days": float(hold_days.max()),
        },
        "recent": {
            "trades": int(len(recent)),
            "net_er": float(r_mean),
            "net_er_ci_week": [float(r_lo), float(r_hi)],
            "gross_er": float(recent["gross_r"].mean()) if len(recent) else 0.0,
            "win_rate": float((recent["pnl_pct"] > 0).mean()) if len(recent) else 0.0,
        },
        "by_year": {y: [v["trades"], v["expectancy_r"], v["expectancy_r_gross"]]
                    for y, v in full["by_year"].items()},
        "by_class": {k: [int(r["count"]), float(r["mean"]), float(r["sum"])] for k, r in by_class.iterrows()},
        "by_side": {k: [int(r["count"]), float(r["mean"])] for k, r in by_side.iterrows()},
        "concentration": {
            "sum_r": float(total),
            "top5": [
                {"symbol": r.symbol, "side": r.side, "entry": str(r.entry_at)[:10], "exit": str(r.exit_at)[:10],
                 "reason": r.reason, "r": float(r.r_multiple)}
                for r in top.itertuples()
            ],
            "top1_share_of_sum": float(rs[order[0]] / total) if total else None,
            "net_er_without_top1": without_top(1),
            "net_er_without_top2": without_top(2),
            "net_er_without_top5": without_top(5),
            "median_r": float(np.median(rs)),
        },
        "criterion": {
            "full_ci_lower_gt_0": lo > 0,
            "recent_point_gt_0": r_mean > 0,
        },
    }


def main(argv: list[str]) -> None:
    directory = Path(argv[0]) if argv else Path(".superpowers/sdd")
    results = {c: evaluate(directory, c) for c in ("typical", "max", "zero")}
    primary = results["typical"]
    passed = primary["criterion"]["full_ci_lower_gt_0"] and primary["criterion"]["recent_point_gt_0"]
    results["verdict"] = "PASS" if passed else "FAIL"
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
