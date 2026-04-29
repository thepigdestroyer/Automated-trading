from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class RuleEvent:
    """A rule that fired, with optional payload describing the trigger.

    ``key`` (optional) is a stable signature used by the watcher to dedupe
    repeated emissions. If two consecutive ticks produce the same
    (rule, key) pair within the configured window, the second is suppressed.
    Rules that should fire on every tick (e.g. process_dead, log_silence —
    the latter is already gated by a separate cooldown) leave key=None.
    """
    rule: str
    action: str  # "alert_only" | "kill_switch" | "blacklist_symbol" | "restart_bot" | "rotate_log"
    detail: dict[str, Any] = field(default_factory=dict)
    key: str | None = None


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
    """Fire if log file mtime is older than max_silence_minutes.

    DEPRECATED in favour of ``heartbeat_silence`` (review 2026-04-29-adhoc-1
    #7). Kept for back-compat — set ``log_silence_minutes`` very high in
    WatcherConfig to disable. The new heartbeat-based rule is both quieter
    (no false positives during normal cycle gaps) and more reliable for
    genuine hangs.
    """
    if not log_file.exists():
        return None
    mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0
    if age_minutes >= max_silence_minutes:
        return RuleEvent(rule="log_silence", action="restart_bot",
                         detail={"age_minutes": round(age_minutes, 1)})
    return None


def heartbeat_silence(*, recent_events: list[dict],
                      max_silence_minutes: float) -> RuleEvent | None:
    """Fire if no ``cycle_heartbeat`` event has been written for more than
    ``max_silence_minutes``. Per review 2026-04-29-adhoc-1 #7.

    The bot writes a heartbeat at the start AND end of every cycle. With
    a normal cycle cadence of 15-25min, ``max_silence_minutes`` should
    be set to roughly 2× normal cadence (default 50min). Absence of any
    heartbeat for that long means the bot has hung or died — either way
    the watcher should restart it.

    If the events stream contains zero heartbeats at all, we also fire
    (covers the cold-start case where the bot crashes before the first
    cycle has time to log).
    """
    hbs = [e for e in recent_events if e.get("event") == "cycle_heartbeat"]
    if not hbs:
        # No heartbeat in the visible window. Only fire if we have *some*
        # other recent events (otherwise the events file is genuinely
        # empty and the bot may not have started yet). Fire defensively.
        if recent_events:
            return RuleEvent(rule="heartbeat_silence", action="restart_bot",
                             detail={"reason": "no heartbeat events in window"})
        return None

    last_ts_iso = str(hbs[-1].get("timestamp") or hbs[-1].get("ts") or "")
    if not last_ts_iso:
        return None
    try:
        last_ts = datetime.fromisoformat(last_ts_iso)
    except ValueError:
        return None
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60.0
    if age_minutes >= max_silence_minutes:
        return RuleEvent(
            rule="heartbeat_silence",
            action="restart_bot",
            detail={"age_minutes": round(age_minutes, 1),
                    "max_silence_minutes": max_silence_minutes},
        )
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


def weekly_drawdown_breach(*, recent_trades: list[dict], max_loss_pct: float) -> RuleEvent | None:
    """Sum of pnl_pct over the last 7 days; fire if at or below -max_loss_pct."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pnls = [float(t.get("pnl_pct", 0)) for t in recent_trades
            if t.get("type") == "trade_close" and t.get("ts", "") >= cutoff]
    total = sum(pnls)
    if total <= -abs(max_loss_pct):
        return RuleEvent(rule="weekly_drawdown_breach", action="alert_only",
                         detail={"net_pnl_pct": total, "limit_pct": -abs(max_loss_pct)})
    return None


def holding_too_long(*, positions: list[dict], max_hours: float) -> RuleEvent | None:
    """Fire if any open position has been held longer than max_hours × 1.2."""
    threshold_hours = max_hours * 1.2
    now = datetime.now(timezone.utc)
    for pos in positions:
        opened = pos.get("opened_at")
        if not opened:
            continue
        try:
            opened_dt = datetime.fromisoformat(opened)
        except ValueError:
            continue
        held_hours = (now - opened_dt).total_seconds() / 3600.0
        if held_hours >= threshold_hours:
            return RuleEvent(rule="holding_too_long", action="alert_only",
                             detail={"symbol": pos.get("symbol"),
                                     "held_hours": round(held_hours, 1),
                                     "threshold_hours": threshold_hours})
    return None


def position_book_divergence(*, local_positions: set[str], broker_positions: set[str]) -> RuleEvent | None:
    """Fire when local and broker position sets disagree."""
    only_local = sorted(local_positions - broker_positions)
    only_broker = sorted(broker_positions - local_positions)
    if only_local or only_broker:
        return RuleEvent(rule="position_book_divergence", action="alert_only",
                         detail={"only_local": only_local, "only_broker": only_broker})
    return None


def data_stale(*, last_bar_ts: str, symbol: str, max_age_hours: float) -> RuleEvent | None:
    """Fire if last bar timestamp is older than max_age_hours."""
    try:
        bar_dt = datetime.fromisoformat(last_bar_ts)
    except ValueError:
        return None
    age_hours = (datetime.now(timezone.utc) - bar_dt).total_seconds() / 3600.0
    if age_hours >= max_age_hours:
        return RuleEvent(rule="data_stale", action="alert_only",
                         detail={"symbol": symbol, "age_hours": round(age_hours, 1)})
    return None


def confidence_outcome_divergence(*, recent_trades: list[dict],
                                  min_n: int = 10,
                                  min_confidence: float = 0.80,
                                  min_win_rate: float = 0.30) -> RuleEvent | None:
    """Fire when high-confidence trades (>= min_confidence) win below min_win_rate over min_n samples."""
    high_conf = [t for t in recent_trades
                 if t.get("type") == "trade_close" and float(t.get("confidence", 0)) >= min_confidence]
    if len(high_conf) < min_n:
        return None
    wins = sum(1 for t in high_conf if float(t.get("pnl_pct", 0)) > 0)
    rate = wins / len(high_conf)
    if rate < min_win_rate:
        return RuleEvent(rule="confidence_outcome_divergence", action="alert_only",
                         detail={"n": len(high_conf), "win_rate": rate, "min_confidence": min_confidence},
                         key="divergence")
    return None


def repeated_startup_failures(*, recent_triggers: list[dict],
                              threshold: int = 3,
                              window_hours: float = 1.0) -> RuleEvent | None:
    """Fire if N process_dead triggers occurred within the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    relevant = [t for t in recent_triggers
                if t.get("type") == "rule_trigger" and t.get("rule") == "process_dead"
                and t.get("ts", "") >= cutoff]
    if len(relevant) >= threshold:
        return RuleEvent(rule="repeated_startup_failures", action="alert_only",
                         detail={"count": len(relevant), "window_hours": window_hours},
                         key=f"count={len(relevant)}")
    return None


def data_feed_degraded(*, recent_events: list[dict], symbol: str | None = None,
                       min_streak: int = 5,
                       max_age_minutes: float = 30.0) -> RuleEvent | None:
    """Fire when the target symbol has produced >= min_streak consecutive
    data_quality (bars-dropped) events within the last ``max_age_minutes``.
    Per review 2026-04-28-adhoc-2 #8.

    Bug fix 2026-04-29 (a): previously the streak counter walked the full
    data_quality event list and broke as soon as it hit a different symbol,
    so NG=F's streak reset every time CL=F (or any other symbol) emitted
    a data_quality event in between. Filter per-symbol FIRST, then count.

    Bug fix 2026-04-29 (b): an old streak that has stopped growing kept
    firing forever. NG=F showed ``consecutive_dropped_cycles=23`` while
    the most recent NG=F event was 3+ hours stale. We now drop events
    older than ``max_age_minutes`` BEFORE counting; if no recent events
    remain (or none for the target symbol), return None.
    """
    dq_all = [e for e in recent_events
              if e.get("event") == "data_quality" or e.get("type") == "data_quality"]
    if not dq_all:
        return None

    # Recency filter: drop events older than the window. We accept either
    # 'timestamp' (bot's JsonEventLogger) or 'ts' (journal-style) keys.
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=max(0.0, float(max_age_minutes)))).isoformat()

    def _ev_ts(ev: dict) -> str:
        return str(ev.get("timestamp") or ev.get("ts") or "")

    dq = [e for e in dq_all if _ev_ts(e) >= cutoff_iso]
    if not dq:
        return None

    if symbol is None:
        last_data = (dq[-1].get("data") or {}) if isinstance(dq[-1], dict) else {}
        symbol = last_data.get("symbol")
        if not symbol:
            return None
    # Per-symbol filter — a different symbol's event is irrelevant, not a break.
    dq_sym = [e for e in dq if (e.get("data") or {}).get("symbol") == symbol]
    if not dq_sym:
        return None
    streak = 0
    for ev in reversed(dq_sym):
        data = ev.get("data") or {}
        try:
            bars_dropped = int(data.get("bars_dropped", 0))
        except (TypeError, ValueError):
            bars_dropped = 0
        if bars_dropped <= 0:
            break
        streak += 1
    if streak >= min_streak:
        return RuleEvent(
            rule="data_feed_degraded",
            action="alert_only",
            detail={"symbol": symbol, "consecutive_dropped_cycles": streak,
                    "threshold": min_streak,
                    "max_age_minutes": float(max_age_minutes)},
            key=f"{symbol}:{streak}",
        )
    return None


def repeated_execution_errors(*, recent_events: list[dict],
                              threshold: int = 3,
                              window_hours: float = 4.0) -> RuleEvent | None:
    """Fire when ≥threshold failed execution results with the SAME error on the
    SAME symbol occur within window_hours. Per review 2026-04-28-adhoc-2 #10.

    Note: an `execution` event's ``data`` field is a *list* of ExecutionResult
    dicts (one per trade plan in the cycle), each shaped roughly:
        {"plan": {"symbol": "GOLD", ...}, "status": "failed", "error": "..."}
    We iterate the list and count only results with ``status == "failed"`` and
    a non-empty ``error`` string.

    Per review 2026-04-29-adhoc-1 #3: also counts `risk_block` events emitted
    by the pre-submit risk gate (e.g. ``STOP_TOO_TIGHT_VS_SPREAD``). The
    2026-04-29 incident produced 8 same-symbol-same-reason WTI risk_blocks
    that this rule previously ignored because they aren't `execution`
    events. Both event families now share the same (symbol, signature)
    counter so a sustained loop is surfaced regardless of which gate
    rejected the plan.

    Default action: alert_only (does NOT auto-blacklist or kill switch)."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    grouped: dict[tuple[str, str], int] = {}
    for ev in recent_events:
        ev_type = ev.get("event")
        ts = ev.get("timestamp") or ev.get("ts") or ""
        if ts and ts < cutoff_iso:
            continue
        if ev_type == "execution":
            results = ev.get("data") or []
            # Tolerate legacy/alternate shapes where data might be a dict.
            if isinstance(results, dict):
                results = [results]
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                if result.get("status") != "failed":
                    continue
                err = (result.get("error") or "").strip()
                if not err:
                    continue
                plan = result.get("plan") or {}
                symbol = (
                    result.get("symbol")
                    or (plan.get("symbol") if isinstance(plan, dict) else None)
                    or ""
                ).strip().upper()
                if not symbol:
                    continue
                key = (symbol, err)
                grouped[key] = grouped.get(key, 0) + 1
        elif ev_type == "risk_block":
            # Pre-submit risk-gate rejections (e.g. STOP_TOO_TIGHT_VS_SPREAD)
            # carry their reason in data.reason and symbol in data.symbol.
            data = ev.get("data") or {}
            if not isinstance(data, dict):
                continue
            reason = (data.get("reason") or "").strip()
            symbol = str(data.get("symbol") or "").strip().upper()
            if not reason or not symbol:
                continue
            # Use the reason code as the "error" so multiple risk-block
            # families don't collide with execution.error strings.
            key = (symbol, f"risk_block:{reason}")
            grouped[key] = grouped.get(key, 0) + 1
    for (symbol, err), count in grouped.items():
        if count >= threshold:
            return RuleEvent(
                rule="repeated_execution_errors",
                action="alert_only",
                detail={"symbol": symbol, "error": err, "count": count,
                        "window_hours": window_hours, "threshold": threshold},
                key=f"{symbol}:{err}:{count}",
            )
    return None


def auto_blacklist_repeated_errors(*, recent_events: list[dict],
                                   threshold: int = 5,
                                   window_hours: float = 4.0,
                                   ttl_hours: float = 4.0) -> RuleEvent | None:
    """Mechanical sibling of ``repeated_execution_errors`` that auto-adds the
    offending symbol to the supervisor blacklist for a short TTL.

    Per review 2026-04-29-adhoc-1 #4. The 2026-04-29 incident burned ~10
    minutes of LLM compute and ~40 model calls re-deciding into 8 WTI
    plans that were all guaranteed to be blocked at the executor. Once
    the same (symbol, error) pair has been observed at least ``threshold``
    times within ``window_hours``, blacklist the symbol for ``ttl_hours``
    so the bot stops wasting cycles on it.

    Default threshold is intentionally HIGHER than the alert-only rule
    (5 vs 3) so the alert fires first and the blacklist only engages
    when the loop is sustained. Default TTL is short (4 h) so a
    transient broker constraint doesn't lock out the symbol all day.

    Action: ``blacklist_symbol`` with detail ``ttl_hours``. Watcher
    routes this through ``remediations.blacklist_symbol_action``.
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    grouped: dict[tuple[str, str], int] = {}
    for ev in recent_events:
        ev_type = ev.get("event")
        ts = ev.get("timestamp") or ev.get("ts") or ""
        if ts and ts < cutoff_iso:
            continue
        if ev_type == "execution":
            results = ev.get("data") or []
            if isinstance(results, dict):
                results = [results]
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict):
                    continue
                if result.get("status") != "failed":
                    continue
                err = (result.get("error") or "").strip()
                if not err:
                    continue
                plan = result.get("plan") or {}
                symbol = (
                    result.get("symbol")
                    or (plan.get("symbol") if isinstance(plan, dict) else None)
                    or ""
                ).strip().upper()
                if not symbol:
                    continue
                grouped[(symbol, err)] = grouped.get((symbol, err), 0) + 1
        elif ev_type == "risk_block":
            data = ev.get("data") or {}
            if not isinstance(data, dict):
                continue
            reason = (data.get("reason") or "").strip()
            symbol = str(data.get("symbol") or "").strip().upper()
            if not reason or not symbol:
                continue
            grouped[(symbol, f"risk_block:{reason}")] = (
                grouped.get((symbol, f"risk_block:{reason}"), 0) + 1
            )
    for (symbol, err), count in grouped.items():
        if count >= threshold:
            return RuleEvent(
                rule="auto_blacklist_repeated_errors",
                action="blacklist_symbol",
                detail={
                    "symbol": symbol,
                    "error": err,
                    "count": count,
                    "window_hours": window_hours,
                    "ttl_hours": ttl_hours,
                    "threshold": threshold,
                },
                key=f"{symbol}:{err}:{count}",
            )
    return None


def risk_gate_degraded_streak(*, recent_events: list[dict],
                              threshold: int = 3,
                              window_hours: float = 1.0) -> RuleEvent | None:
    """Fire when ≥threshold risk_gate_degraded events occur within window_hours.
    Per review 2026-04-28-adhoc-2 #11. Action: alert_only."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    relevant = []
    for ev in recent_events:
        if ev.get("event") != "risk_gate_degraded":
            continue
        ts = ev.get("timestamp") or ev.get("ts") or ""
        if ts and ts < cutoff_iso:
            continue
        relevant.append(ev)
    if len(relevant) >= threshold:
        gates = sorted({(ev.get("data") or {}).get("gate", "?") for ev in relevant})
        symbols = sorted({(ev.get("data") or {}).get("symbol", "?") for ev in relevant})
        return RuleEvent(
            rule="risk_gate_degraded_streak",
            action="alert_only",
            detail={"count": len(relevant), "window_hours": window_hours,
                    "threshold": threshold, "gates": gates, "symbols": symbols},
            key=f"streak={len(relevant)}",
        )
    return None


def subagent_failure_streak(*, recent_events: list[dict],
                             threshold: int = 3,
                             window_minutes: float = 30.0) -> RuleEvent | None:
    """Fire when ≥threshold ``subagent_failure`` events occur within
    window_minutes. Per review 2026-04-29-adhoc-1 #9. Action: alert_only.

    The 2026-04-29 incident: 2 distinct provider failures in 80min
    (OpenRouter no-choices + Fireworks 503). If a third had landed, the
    bot would have produced low-quality decisions for the rest of the
    day with no signal to the supervisor.
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat()
    relevant = []
    for ev in recent_events:
        if ev.get("event") != "subagent_failure":
            continue
        ts = ev.get("timestamp") or ev.get("ts") or ""
        if ts and ts < cutoff_iso:
            continue
        relevant.append(ev)
    if len(relevant) >= threshold:
        providers = sorted({(ev.get("data") or {}).get("provider", "?")
                            for ev in relevant})
        subagents = sorted({(ev.get("data") or {}).get("subagent", "?")
                            for ev in relevant})
        return RuleEvent(
            rule="subagent_failure_streak",
            action="alert_only",
            detail={"count": len(relevant),
                    "window_minutes": window_minutes,
                    "threshold": threshold,
                    "providers": providers,
                    "subagents": subagents},
            key=f"streak={len(relevant)}",
        )
    return None


def disk_log_growth(*, log_file: Path, max_mb: float = 100.0) -> RuleEvent | None:
    """Fire if the log file is larger than max_mb."""
    if not log_file.exists():
        return None
    size_mb = log_file.stat().st_size / (1024 * 1024)
    if size_mb >= max_mb:
        return RuleEvent(rule="disk_log_growth", action="rotate_log",
                         detail={"size_mb": round(size_mb, 1), "max_mb": max_mb})
    return None
