"""Cost-aware paper-fill simulator (upgraded for the Phase 3 forward-walk eval).

v1 flattered the strategy: entries at mid, stop/TP checked against the last
CLOSE only (intrabar stop-outs invisible), no expiry, no costs, no join key
to the decision that opened the trade. A forward-walk verdict computed on
those fills would be optimistically biased — exactly the failure mode the
2026-05 adversarial review told us to kill.

v2 (this file):
  - adverse fills: entry/exit adjusted by (half-spread + slippage) bps
    AGAINST the trade on both legs;
  - intrabar High/Low stop/TP detection over every bar since entry, with a
    conservative STOP-FIRST tie-break when one bar touches both levels;
  - max_holding_hours expiry at the last bar close (matches the live
    executor's holding discipline);
  - financing accrual (swap_annualised_bps pro-rated by hold time) deducted
    from P&L;
  - r_multiple / pnl_price_units / pnl_pct / win computed per close;
  - decision_id + confidence carried through so forward_eval.py can join
    closes back to decision_records.jsonl for calibration (Brier);
  - injectable `bar_fetcher` so tests run without network; default fetcher
    pulls hourly bars from Yahoo covering the position's lifetime;
  - schema-compatible with v1 state files (legacy rows lack the new fields
    and degrade to cost-free evaluation rather than raising).

Schema version on rows: paper_schema=2.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yfinance as yf

LOGGER = logging.getLogger(__name__)

_HOURS_PER_YEAR = 8766.0

BarFetcher = Callable[[str, str], "pd.DataFrame | None"]
"""(data_symbol, opened_at_iso) -> OHLC frame with High/Low/Close, or None."""


def _default_bar_fetcher(data_symbol: str, opened_at: str) -> pd.DataFrame | None:
    """Fetch hourly bars covering the position's lifetime (max 720d back —
    practically the eval window is days/weeks)."""
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        age_days = max(1, (datetime.now(timezone.utc) - opened).days + 2)
        period = f"{min(age_days, 720)}d"
        frame = yf.download(
            tickers=data_symbol,
            period=period,
            interval="60m",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if frame is None or frame.empty:
            return None
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        return frame
    except Exception:
        return None


def _adverse_fill(mid: float, side: str, *, leg: str, spread_bps: float, slippage_bps: float) -> float:
    """Fill price adjusted AGAINST the trade: buy entries/short exits fill
    higher; sell entries/long exits fill lower."""
    adj = mid * (spread_bps / 2.0 + slippage_bps) / 10_000.0
    worse_up = (side == "buy" and leg == "entry") or (side == "sell" and leg == "exit")
    return mid + adj if worse_up else mid - adj


class PaperPnL:
    def __init__(self, path: Path, *, bar_fetcher: BarFetcher | None = None):
        self.path = path
        self.bar_fetcher: BarFetcher = bar_fetcher or _default_bar_fetcher
        self.state: dict[str, Any] = {"open": [], "closed": []}
        if path.exists():
            try:
                self.state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

    # --- open -----------------------------------------------------------

    def record_entry(
        self,
        *,
        symbol: str,
        side: str,
        entry: float,
        stop: float,
        tp: float,
        opened_at: str,
        data_symbol: str,
        decision_id: str | None = None,
        confidence: float | None = None,
        spread_bps: float = 0.0,
        slippage_bps: float = 0.0,
        swap_annualised_bps: float = 0.0,
        max_holding_hours: float | None = None,
    ) -> None:
        fill_entry = _adverse_fill(
            entry, side, leg="entry", spread_bps=spread_bps, slippage_bps=slippage_bps
        )
        self.state.setdefault("open", []).append(
            {
                "paper_schema": 2,
                "symbol": symbol,
                "data_symbol": data_symbol,
                "side": side,
                "entry": entry,
                "fill_entry": fill_entry,
                "stop": stop,
                "tp": tp,
                "opened_at": opened_at,
                "decision_id": decision_id,
                "confidence": confidence,
                "spread_bps": spread_bps,
                "slippage_bps": slippage_bps,
                "swap_annualised_bps": swap_annualised_bps,
                "max_holding_hours": max_holding_hours,
            }
        )
        self._save()

    # --- evaluate ---------------------------------------------------------

    def evaluate(self, now: datetime | None = None) -> None:
        """Walk every open position through the bars since its entry; close on
        stop/TP touch (stop first on a both-touched bar) or holding expiry."""
        now = now or datetime.now(timezone.utc)
        still_open: list[dict] = []
        for item in self.state.get("open", []):
            try:
                closed = self._evaluate_one(item, now)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("paper evaluate failed for %s: %s", item.get("symbol"), exc)
                closed = None
            if closed is None:
                still_open.append(item)
            else:
                self.state.setdefault("closed", []).append(closed)
        self.state["open"] = still_open
        self._save()

    def _evaluate_one(self, item: dict, now: datetime) -> dict | None:
        bars = self.bar_fetcher(item["data_symbol"], item["opened_at"])
        if bars is None or len(bars) == 0 or "Close" not in bars.columns:
            return None
        opened = datetime.fromisoformat(str(item["opened_at"]).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)

        idx = bars.index
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize("UTC")
        mask = idx >= opened
        frame = bars.loc[mask]
        if frame.empty:
            frame = bars.tail(1)

        side = item["side"]
        stop = float(item["stop"])
        tp = float(item["tp"])
        has_hl = "High" in frame.columns and "Low" in frame.columns

        for ts, row in frame.iterrows():
            high = float(row["High"]) if has_hl else float(row["Close"])
            low = float(row["Low"]) if has_hl else float(row["Close"])
            if side == "buy":
                stop_hit, tp_hit = low <= stop, high >= tp
            else:
                stop_hit, tp_hit = high >= stop, low <= tp
            if stop_hit:  # conservative: stop wins a both-touched bar
                return self._close(item, exit_mid=stop, reason="stop", exit_at=ts, now=now)
            if tp_hit:
                return self._close(item, exit_mid=tp, reason="tp", exit_at=ts, now=now)

        # Holding-period expiry at the last available bar close.
        max_hold = item.get("max_holding_hours")
        if max_hold:
            held_hours = (now - opened).total_seconds() / 3600.0
            if held_hours >= float(max_hold):
                last_close = float(frame["Close"].iloc[-1])
                return self._close(
                    item, exit_mid=last_close, reason="expiry",
                    exit_at=frame.index[-1], now=now,
                )
        return None

    def _close(self, item: dict, *, exit_mid: float, reason: str, exit_at: Any, now: datetime) -> dict:
        side = item["side"]
        spread = float(item.get("spread_bps") or 0.0)
        slip = float(item.get("slippage_bps") or 0.0)
        swap = float(item.get("swap_annualised_bps") or 0.0)
        fill_entry = float(item.get("fill_entry") or item["entry"])
        fill_exit = _adverse_fill(exit_mid, side, leg="exit", spread_bps=spread, slippage_bps=slip)

        opened = datetime.fromisoformat(str(item["opened_at"]).replace("Z", "+00:00"))
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        exit_dt = exit_at.to_pydatetime() if hasattr(exit_at, "to_pydatetime") else now
        if exit_dt.tzinfo is None:
            exit_dt = exit_dt.replace(tzinfo=timezone.utc)
        hold_hours = max(0.0, (exit_dt - opened).total_seconds() / 3600.0)

        financing = fill_entry * (swap / 10_000.0) * (hold_hours / _HOURS_PER_YEAR)
        direction = 1.0 if side == "buy" else -1.0
        pnl_units = direction * (fill_exit - fill_entry) - financing

        stop_distance = abs(float(item["entry"]) - float(item["stop"])) or None
        r_multiple = (pnl_units / stop_distance) if stop_distance else None
        pnl_pct = pnl_units / fill_entry if fill_entry else None

        return {
            **item,
            "exit": exit_mid,
            "fill_exit": fill_exit,
            "exit_at": str(exit_at),
            "closed_at": now.isoformat(),
            "reason": reason,
            "hold_hours": round(hold_hours, 2),
            "financing_cost": round(financing, 6),
            "pnl_price_units": pnl_units,
            "pnl_pct": pnl_pct,
            "r_multiple": r_multiple,
            "win": pnl_units > 0,
        }

    # --- persistence / summary -------------------------------------------

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> dict[str, Any]:
        closed = self.state.get("closed", [])
        if not closed:
            return {"trades": 0, "win_rate": 0.0}
        wins = sum(1 for c in closed if c.get("win") or (
            # legacy v1 rows: derive from raw entry/exit
            "win" not in c and (
                (c["side"] == "buy" and c.get("exit", 0) > c.get("entry", 0))
                or (c["side"] == "sell" and c.get("exit", 0) < c.get("entry", 0))
            )
        ))
        return {"trades": len(closed), "win_rate": wins / len(closed)}
