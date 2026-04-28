from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from trading_bot.models import OpenPosition


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    direction: str
    entry: float
    exit: float
    pnl_pct: float
    r: float
    reason: str
    entry_at: str
    exit_at: str


class TradeJournal:
    def __init__(self, *, state: dict[str, Any]):
        self.state = state
        self.state.setdefault("trade_journal", [])

    def record(
        self,
        *,
        symbol: str,
        direction: str,
        entry: float,
        exit: float,
        entry_at: str,
        exit_at: str,
        reason: str,
        stop_distance: float | None = None,
    ) -> None:
        sign = 1.0 if direction.lower() in {"buy", "long"} else -1.0
        pnl_pct = sign * (exit - entry) / entry if entry else 0.0
        if stop_distance and stop_distance > 0:
            r = sign * (exit - entry) / stop_distance
        else:
            r = pnl_pct / 0.012
        self.state["trade_journal"].append(
            {
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "exit": exit,
                "pnl_pct": pnl_pct,
                "r": r,
                "reason": reason,
                "entry_at": entry_at,
                "exit_at": exit_at,
            }
        )

    def entries(self) -> list[TradeRecord]:
        return [
            TradeRecord(**{k: v for k, v in item.items() if k in TradeRecord.__dataclass_fields__})
            for item in self.state["trade_journal"]
        ]

    def recent(self, n: int = 10) -> list[TradeRecord]:
        return self.entries()[-n:]

    def rolling_accuracy(self, window: int = 30) -> float:
        records = self.entries()[-window:]
        if not records:
            return 0.0
        wins = sum(1 for r in records if r.pnl_pct > 0)
        return wins / len(records)


class ConsecutiveLossTracker:
    def __init__(self, *, state: dict[str, Any], limit: int, pause_hours: int):
        self.state = state
        self.limit = limit
        self.pause_hours = pause_hours

    def check(self, *, now: datetime) -> tuple[bool, str]:
        pause_until_raw = self.state.get("consecutive_loss_pause_until")
        if pause_until_raw:
            try:
                pause_until = datetime.fromisoformat(str(pause_until_raw).replace("Z", "+00:00"))
                if pause_until.tzinfo is None:
                    pause_until = pause_until.replace(tzinfo=timezone.utc)
                if pause_until > now:
                    return False, f"Consecutive-loss pause active until {pause_until.isoformat()}"
            except ValueError:
                self.state["consecutive_loss_pause_until"] = None

        journal = self.state.get("trade_journal", [])
        if not journal:
            return True, ""
        streak = 0
        for item in reversed(journal):
            if item.get("pnl_pct", 0.0) < 0:
                streak += 1
            else:
                break
        if streak >= self.limit:
            pause_until = now + timedelta(hours=self.pause_hours)
            self.state["consecutive_loss_pause_until"] = pause_until.isoformat()
            return False, f"{streak} consecutive losses — pausing new entries"
        return True, ""


class CurrencyExposureAggregator:
    def __init__(self, *, instruments: dict[str, Any]):
        self._instruments = instruments

    def aggregate(self, open_positions: list[OpenPosition], equity: float) -> dict[str, float]:
        if equity <= 0:
            return {}
        out: dict[str, float] = {}
        for pos in open_positions:
            instrument = self._instruments.get(pos.symbol.upper())
            currency = self._infer_currency(instrument)
            notional = abs(pos.quantity) * float(pos.current_price or 0.0)
            out[currency] = out.get(currency, 0.0) + (notional / equity)
        return out

    @staticmethod
    def _infer_currency(instrument) -> str:
        if instrument is None:
            return "USD"
        cg = (getattr(instrument, "correlation_group", "") or "").lower()
        if cg.startswith("fx_"):
            return cg.split("_", 1)[1].upper()
        mapping = {
            "us_equity": "USD",
            "us_equity_mega": "USD",
            "us_equity_small": "USD",
            "uk_equity": "GBP",
            "eu_equity": "EUR",
            "precious_metals": "USD",
            "industrial_metals": "USD",
            "energy_oil": "USD",
            "energy_gas": "USD",
        }
        return mapping.get(cg, "USD")


class CorrelationCap:
    def __init__(self, *, cap: float):
        self.cap = cap

    def would_breach(
        self,
        new_symbol: str,
        existing_symbols: list[str],
        *,
        instruments: dict[str, Any],
    ) -> bool:
        new_inst = instruments.get(new_symbol.upper())
        if new_inst is None:
            return False
        for existing in existing_symbols:
            existing_inst = instruments.get(existing.upper())
            if existing_inst is None:
                continue
            if (
                getattr(existing_inst, "correlation_group", None)
                == getattr(new_inst, "correlation_group", None)
                and new_inst.correlation_group != "misc"
            ):
                return True
        return False
