from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yfinance as yf


class PaperPnL:
    def __init__(self, path: Path):
        self.path = path
        self.state: dict[str, Any] = {"open": [], "closed": []}
        if path.exists():
            try:
                self.state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass

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
    ) -> None:
        self.state.setdefault("open", []).append(
            {
                "symbol": symbol,
                "data_symbol": data_symbol,
                "side": side,
                "entry": entry,
                "stop": stop,
                "tp": tp,
                "opened_at": opened_at,
            }
        )
        self._save()

    def evaluate(self) -> None:
        still_open: list[dict] = []
        for item in self.state.get("open", []):
            price = self._last_price(item["data_symbol"])
            if price is None:
                still_open.append(item)
                continue
            closed = self._check(item, price)
            if closed is None:
                still_open.append(item)
            else:
                self.state.setdefault("closed", []).append(closed)
        self.state["open"] = still_open
        self._save()

    def _check(self, item: dict, price: float) -> dict | None:
        side = item["side"]
        entry = item["entry"]
        stop = item["stop"]
        tp = item["tp"]
        now = datetime.now(timezone.utc).isoformat()
        if side == "buy":
            if price <= stop:
                return {**item, "exit": price, "exit_at": now, "reason": "stop"}
            if price >= tp:
                return {**item, "exit": price, "exit_at": now, "reason": "tp"}
        elif side == "sell":
            if price >= stop:
                return {**item, "exit": price, "exit_at": now, "reason": "stop"}
            if price <= tp:
                return {**item, "exit": price, "exit_at": now, "reason": "tp"}
        return None

    def _last_price(self, data_symbol: str) -> float | None:
        try:
            frame = yf.download(
                tickers=data_symbol,
                period="2d",
                interval="60m",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if frame.empty:
                return None
            return float(frame["Close"].dropna().iloc[-1])
        except Exception:
            return None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> dict[str, Any]:
        closed = self.state.get("closed", [])
        if not closed:
            return {"trades": 0, "win_rate": 0.0}
        wins = sum(
            1
            for c in closed
            if (c["side"] == "buy" and c["exit"] > c["entry"])
            or (c["side"] == "sell" and c["exit"] < c["entry"])
        )
        return {"trades": len(closed), "win_rate": wins / len(closed)}
