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


def process_dead(*, pid_file: Path, pid_alive: Callable[[int], bool], bot_log: Path | None = None) -> RuleEvent | None:
    """Return an event if bot.pid is missing or its PID is not running."""
    is_dead = False
    reason = ""
    pid = None
    if not pid_file.exists():
        is_dead = True
        reason = "pid file missing"
    else:
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            is_dead = True
            reason = "pid file unreadable"
        
        if not is_dead and not pid_alive(pid):
            is_dead = True
            reason = "pid not running"

    if is_dead:
        if bot_log and bot_log.exists():
            import collections
            try:
                with bot_log.open("r", encoding="utf-8") as f:
                    tail = collections.deque(f, 50)
                now = datetime.now(timezone.utc)
                for line in reversed(tail):
                    lower_line = line.lower()
                    if "signal 15 received" in lower_line or "graceful" in lower_line or "shutting down" in lower_line:
                        parts = line.split(" ")
                        if len(parts) >= 2:
                            ts_str = parts[0] + " " + parts[1]
                            try:
                                log_ts = datetime.fromisoformat(ts_str.replace(",", "."))
                                if log_ts.tzinfo is None:
                                    log_ts = log_ts.replace(tzinfo=timezone.utc)
                                if (now - log_ts).total_seconds() < 30.0:
                                    return None  # hold off for 30s
                            except ValueError:
                                pass
            except OSError:
                pass
        
        detail = {"reason": reason}
        if pid is not None:
            detail["pid"] = pid
        return RuleEvent(rule="process_dead", action="restart_bot", detail=detail)
    return None


def log_silence(*, log_file: Path, max_silence_minutes: float) -> RuleEvent | None:
    """Fire if log file mtime is older than max_silence_minutes.

    DEPRECATED in favour of ``heartbeat_silence`` (review
    2026-04-29-adhoc-1 #7). Per review 2026-05-02-adhoc-1 #5, the
    default in ``WatcherConfig.log_silence_minutes`` is now ``0`` so
    the rule is disabled by default; the watcher skips invocation
    entirely when ``log_silence_minutes <= 0``. The function is kept
    for back-compat with explicit-threshold tests and for the rare
    deployment that still wants file-mtime-based liveness.
    """
    if not log_file.exists():
        return None
    if max_silence_minutes <= 0:
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


def cycle_stall_with_open_position(
    *,
    recent_events: list[dict],
    runtime_state: dict | None,
    max_silence_minutes: float = 20.0,
) -> RuleEvent | None:
    """Fire ``alert_only`` when ``cycle_heartbeat`` has been silent for
    ≥ ``max_silence_minutes`` AND the bot has at least one open
    position at the broker. Per review 2026-05-05-adhoc-1 #3.

    Distinct from ``heartbeat_silence``: this rule has a shorter window
    (default 20 min vs heartbeat_silence's 50 min) and is GATED on
    capital-at-risk. Today's 2026-05-05 12:31 → 13:03 gap (32 min) was
    just under heartbeat_silence's 50 min threshold, but a position
    closed during it without surface signal.

    Returns None when:
      - no heartbeat older than the threshold (bot healthy);
      - heartbeat silent but ``runtime_state`` reports zero open
        positions (no exposure → routine quiet cycle);
      - ``runtime_state`` is unavailable (be permissive — let
        heartbeat_silence at the longer threshold handle the dead-bot
        case unconditionally).

    Action is ``alert_only`` (intentionally not restart_bot — we want
    operator visibility, not an automatic remediation that disturbs
    trading).
    """
    if not runtime_state:
        return None
    account = runtime_state.get("account") or {}
    open_positions = account.get("open_positions")
    # ``open_positions`` may be a list (preferred) or an int count.
    if isinstance(open_positions, list):
        has_positions = len(open_positions) > 0
    elif isinstance(open_positions, (int, float)):
        has_positions = open_positions > 0
    else:
        # Fallback: check the ``positions`` dict the bot persists.
        positions = runtime_state.get("positions") or {}
        has_positions = bool(positions)
    if not has_positions:
        return None

    hbs = [e for e in recent_events if e.get("event") == "cycle_heartbeat"]
    if not hbs:
        # If we have positions but no heartbeats at all in the window,
        # fire defensively — same logic as heartbeat_silence's cold-start
        # branch but only when capital is at risk.
        if recent_events:
            return RuleEvent(
                rule="cycle_stall_with_open_position",
                action="alert_only",
                detail={
                    "reason": "no heartbeat events in window",
                    "max_silence_minutes": max_silence_minutes,
                },
                key="no_heartbeat",
            )
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
            rule="cycle_stall_with_open_position",
            action="alert_only",
            detail={
                "age_minutes": round(age_minutes, 1),
                "max_silence_minutes": max_silence_minutes,
                "open_position_count": (
                    len(open_positions) if isinstance(open_positions, list)
                    else int(open_positions or 0)
                    if isinstance(open_positions, (int, float))
                    else len(runtime_state.get("positions") or {})
                ),
            },
            key=f"stalled_{round(age_minutes / 5) * 5}m",  # dedup at 5-min buckets
        )
    return None


def news_memory_growth_anomaly(
    *,
    recent_events: list[dict],
    max_size: int = 500,
    max_age_hours: float = 72.0,
) -> RuleEvent | None:
    """Fire ``alert_only`` when the latest cycle_heartbeat shows
    ``news_memory_size > max_size`` OR
    ``news_memory_oldest_published_at`` older than ``max_age_hours``.
    Per review 2026-05-05-adhoc-1 #7.

    Today (2026-05-05) the in-memory store sat at 216 entries despite
    the 2026-05-03 #10 fix that prunes on every cycle. That's well
    below the soft cap here (500) so this rule wouldn't fire — but
    catches future inflow-rate-vs-prune regressions or TTL bugs.
    """
    hbs = [e for e in recent_events if e.get("event") == "cycle_heartbeat"]
    if not hbs:
        return None
    latest = hbs[-1].get("data") or {}
    size = latest.get("news_memory_size")
    oldest_iso = latest.get("news_memory_oldest_published_at")

    breached: list[str] = []
    if isinstance(size, (int, float)) and size > max_size:
        breached.append(f"size={int(size)}>{max_size}")
    if oldest_iso:
        try:
            oldest_dt = datetime.fromisoformat(str(oldest_iso))
            if oldest_dt.tzinfo is None:
                oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
            age_hours = (
                datetime.now(timezone.utc) - oldest_dt
            ).total_seconds() / 3600.0
            if age_hours > max_age_hours:
                breached.append(
                    f"oldest_age={age_hours:.1f}h>{max_age_hours}h"
                )
        except (ValueError, TypeError):
            pass

    if not breached:
        return None
    return RuleEvent(
        rule="news_memory_growth_anomaly",
        action="alert_only",
        detail={
            "size": int(size) if isinstance(size, (int, float)) else None,
            "oldest_published_at": oldest_iso,
            "max_size": max_size,
            "max_age_hours": max_age_hours,
            "breached": breached,
        },
        # Bucket dedup: same breach class = same key for ~1h, prevents
        # spam when the bot stays in an anomalous state.
        key="|".join(b.split("=")[0] for b in breached),
    )


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


def data_feed_stale(*, recent_events: list[dict], symbol: str | None = None,
                    min_streak: int = 8) -> RuleEvent | None:
    """Fire when a symbol has >= ``min_streak`` consecutive ``data_quality``
    events with the **same** ``last_good_ts`` value. Per review
    2026-05-02-adhoc-1 #9.

    The existing ``data_feed_degraded`` rule catches the chronic
    bars-dropped pattern but misses the "feed is correctly closed
    over a weekend gap" scenario where the LLM is shown indicators
    computed against >24h-old data presented as fresh. This rule
    fires when the same ``last_good_ts`` plateau appears in N
    consecutive cycles for a single symbol — a deterministic signal
    that the upstream feed has stopped advancing.

    Returns ``alert_only`` so it doesn't trigger remediation; the
    purpose is to surface the staleness to the operator and to
    insights.md, not to auto-act.
    """
    dq_all = [e for e in recent_events
              if e.get("event") == "data_quality" or e.get("type") == "data_quality"]
    if not dq_all:
        return None

    # If symbol not given, pick the most-recent data_quality emitter.
    if symbol is None:
        last_data = (dq_all[-1].get("data") or {}) if isinstance(dq_all[-1], dict) else {}
        symbol = last_data.get("symbol")
        if not symbol:
            return None

    dq_sym = [e for e in dq_all if (e.get("data") or {}).get("symbol") == symbol]
    if len(dq_sym) < min_streak:
        return None

    # Walk backwards counting events whose last_good_ts equals the most
    # recent event's last_good_ts.
    last_good_ts = (dq_sym[-1].get("data") or {}).get("last_good_ts")
    if not last_good_ts:
        return None

    streak = 0
    for ev in reversed(dq_sym):
        data = ev.get("data") or {}
        if data.get("last_good_ts") == last_good_ts:
            streak += 1
        else:
            break

    if streak >= min_streak:
        return RuleEvent(
            rule="data_feed_stale",
            action="alert_only",
            detail={
                "symbol": symbol,
                "consecutive_same_last_good_ts": streak,
                "last_good_ts": last_good_ts,
                "threshold": min_streak,
            },
            key=f"{symbol}:{last_good_ts}",
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
