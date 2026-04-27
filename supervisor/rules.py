from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class RuleEvent:
    """A rule that fired, with optional payload describing the trigger."""
    rule: str
    action: str  # "alert_only" | "kill_switch" | "blacklist_symbol" | "restart_bot" | "rotate_log"
    detail: dict[str, Any] = field(default_factory=dict)


def process_dead(*, pid_file: Path, pid_alive: Callable[[int], bool]) -> RuleEvent | None:
    """Return an event if bot.pid is missing or its PID is not running."""
    if not pid_file.exists():
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid file missing"})
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid file unreadable"})
    if not pid_alive(pid):
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid not running", "pid": pid})
    return None


def log_silence(*, log_file: Path, max_silence_minutes: float) -> RuleEvent | None:
    """Fire if log file mtime is older than max_silence_minutes."""
    if not log_file.exists():
        return None
    mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0
    if age_minutes >= max_silence_minutes:
        return RuleEvent(rule="log_silence", action="restart_bot",
                         detail={"age_minutes": round(age_minutes, 1)})
    return None


def consecutive_losses(*, recent_trades: list[dict], threshold: int = 3) -> RuleEvent | None:
    """Fire if the most recent N trade_close events are all losses (pnl_pct <= 0)."""
    closes = [t for t in recent_trades if t.get("type") == "trade_close"]
    if len(closes) < threshold:
        return None
    recent = closes[-threshold:]
    if all(float(t.get("pnl_pct", 0)) <= 0 for t in recent):
        return RuleEvent(rule="consecutive_losses", action="kill_switch",
                         detail={"threshold": threshold,
                                 "trades": [t.get("symbol") for t in recent]})
    return None


def same_symbol_stop_streak(*, recent_trades: list[dict], threshold: int = 3) -> RuleEvent | None:
    """Fire when the last N closes on a single symbol are all stop-outs."""
    closes = [t for t in recent_trades if t.get("type") == "trade_close"]
    if len(closes) < threshold:
        return None
    recent = closes[-threshold:]
    syms = {t.get("symbol") for t in recent}
    if len(syms) != 1:
        return None
    if all(t.get("reason") == "stop" for t in recent):
        return RuleEvent(rule="same_symbol_stop_streak", action="blacklist_symbol",
                         detail={"symbol": list(syms)[0], "ttl_hours": 24, "threshold": threshold})
    return None


def daily_drawdown_breach(*, rollup: dict, max_loss_pct: float) -> RuleEvent | None:
    """Fire when the day's net_pnl_pct is at or below -max_loss_pct."""
    pnl = float(rollup.get("net_pnl_pct", 0.0))
    if pnl <= -abs(max_loss_pct):
        return RuleEvent(rule="daily_drawdown_breach", action="kill_switch",
                         detail={"net_pnl_pct": pnl, "limit_pct": -abs(max_loss_pct)})
    return None


def llm_error_spike(*, recent_log_lines: list[str], threshold: int = 3) -> RuleEvent | None:
    n_err = sum(1 for line in recent_log_lines if "ERROR" in line and (
        "openai" in line.lower() or "openrouter" in line.lower()))
    if n_err >= threshold:
        return RuleEvent(rule="llm_error_spike", action="alert_only",
                         detail={"errors": n_err, "threshold": threshold})
    return None


def ig_allowance_burst(*, recent_log_lines: list[str], threshold: int = 2) -> RuleEvent | None:
    n = sum(1 for line in recent_log_lines if "exceeded-api-key-allowance" in line)
    if n >= threshold:
        return RuleEvent(rule="ig_allowance_burst", action="alert_only",
                         detail={"hits": n, "threshold": threshold})
    return None


def news_volume_drop(*, recent_cycles: list[dict], symbol: str, min_cycles_zero: int = 6) -> RuleEvent | None:
    relevant = [c for c in recent_cycles if symbol in (c.get("symbols") or [])]
    if len(relevant) < min_cycles_zero:
        return None
    last = relevant[-min_cycles_zero:]
    if all((c.get("headlines_per_symbol") or {}).get(symbol, 0) == 0 for c in last):
        return RuleEvent(rule="news_volume_drop", action="alert_only",
                         detail={"symbol": symbol, "cycles_with_zero": min_cycles_zero})
    return None


def decision_empty_run(*, recent_cycles: list[dict], threshold: int = 2) -> RuleEvent | None:
    cycles = [c for c in recent_cycles if c.get("type") == "cycle"]
    if len(cycles) < threshold:
        return None
    last = cycles[-threshold:]
    if all(c.get("trades_approved", 0) == 0 and c.get("trades_rejected", 0) == 0 for c in last):
        return RuleEvent(rule="decision_empty_run", action="alert_only",
                         detail={"cycles": threshold})
    return None
