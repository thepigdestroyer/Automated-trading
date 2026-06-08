from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from supervisor import journal, blacklist, rollups, rules, remediations
from supervisor.state import load_state, save_state


def _read_recent_events_file(path: Path, *, max_records: int = 5000) -> list[dict]:
    """Read up to max_records most-recent records from a JSONL events file.
    Used to feed the bot-emitted events.jsonl (data_quality, execution, ...)
    into watcher rules."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if max_records and len(out) > max_records:
        out = out[-max_records:]
    return out


def _read_runtime_state(path: Path | None) -> dict | None:
    """Best-effort read of the bot's runtime_state.json snapshot. Returns
    None if the file is missing, unreadable, or not valid JSON. Used by
    rules that need to consult open-position state (per review
    2026-05-05-adhoc-1 #3).
    """
    if path is None or not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


LOGGER = logging.getLogger("supervisor.watcher")


@dataclass
class WatcherConfig:
    bot_pid_file: Path
    bot_log: Path
    events_file: Path
    journal_file: Path
    blacklist_file: Path
    kill_switch_file: Path
    watcher_state_file: Path
    bot_command: list[str]
    # Per review 2026-04-29-adhoc-1 #8: per-cycle decision logs
    # (logs/YYYYMMDDTHHMMSSZ.json) accumulate at ~5MB/day. Run the
    # rotation once per day at most. Default policy: gzip after 3
    # days, delete after 30 days.
    decision_log_dir: Path | None = None
    decision_log_gzip_after_days: float = 3.0
    decision_log_delete_after_days: float = 30.0
    decision_log_rotation_interval_hours: float = 24.0
    # Per review 2026-04-30-adhoc-1 #4: events.jsonl rotation. Daily
    # rotate prior-day events into logs/archive/YYYY-MM/events-DATE.jsonl.gz
    # and expire archives older than events_delete_after_days days.
    events_delete_after_days: float = 30.0
    events_rotation_interval_hours: float = 24.0
    # Per review 2026-04-30-adhoc-1 #6: watcher_heartbeat journal entry.
    # Default 30min so the journal grows by ~48 entries/day (~5 KB/day).
    watcher_heartbeat_interval_minutes: float = 30.0
    consecutive_loss_threshold: int = 3
    # Per review 2026-05-02-adhoc-1 #5: legacy file-mtime-based
    # log_silence rule retired. The cycle_heartbeat /
    # heartbeat_silence rule (commit 1e0abf9) is the canonical
    # liveness signal; the legacy rule contributed 873 false-positive
    # triggers per 24h on 2026-05-02 (100% suppressed by
    # previous_pid_still_alive). Default raised to 0 to disable;
    # tests that exercise the rule explicitly pass a positive value.
    log_silence_minutes: float = 0.0
    # Per review 2026-04-29-adhoc-1 #7: positive cycle_heartbeat-based
    # silence detector. Default 50min ≈ 2× normal cycle cadence (~25m).
    # The bot emits cycle_heartbeat at start AND end of every cycle.
    # Setting log_silence_minutes to 0 disables the legacy detector.
    heartbeat_silence_minutes: float = 50.0
    daily_drawdown_pct: float = 3.0
    weekly_drawdown_pct: float = 6.0
    same_symbol_stop_threshold: int = 3
    poll_interval_seconds: float = 30.0
    max_restarts_per_hour: int = 3
    max_log_mb: float = 100.0
    # After issuing restart_bot for ANY rule, suppress further restart_bot
    # actions for this many seconds. Prevents the rapid-fire restart loop
    # observed on 2026-04-29 (3 concurrent bots within 90 s).
    restart_cooldown_seconds: float = 60.0
    # Recency window for the data_feed_degraded rule. Older streaks are
    # ignored so a long-finished streak doesn't keep firing on every tick.
    data_feed_max_age_minutes: float = 30.0
    # Per review 2026-05-02-adhoc-1 #9: minimum consecutive same-
    # last_good_ts data_quality events before data_feed_stale fires.
    # Default 8 ≈ 4h at 30-min cycles — long enough to filter normal
    # closed-market gaps, short enough to surface a genuinely stuck
    # feed within a single trading session.
    data_feed_stale_min_streak: int = 8
    # Per review 2026-05-05-adhoc-1 #3: cycle_stall_with_open_position
    # rule. Shorter, conditional liveness signal that fires alert_only
    # when heartbeat is silent for ≥ this many minutes AND the bot has
    # at least one open position. Default 20min — well below
    # heartbeat_silence_minutes (50min) so a 30+ min stall over an
    # open position is surfaced before the unconditional restart rule
    # would fire.
    cycle_stall_with_open_position_minutes: float = 20.0
    # Optional path to runtime_state.json so the watcher can read the
    # bot's account snapshot. Used by cycle_stall_with_open_position.
    # If unset / file missing, the rule is silent.
    runtime_state_file: Path | None = None
    # Per review 2026-05-05-adhoc-1 #7: news_memory_growth_anomaly rule
    # thresholds. Today's 2026-05-05 production size: 216 — well under
    # the 500-entry soft cap. The 72h age cap mirrors the bot's own
    # 48h-TTL plus a safety margin.
    news_memory_max_size: int = 500
    news_memory_max_age_hours: float = 72.0
    # Dedup window for repeated rule emissions with the same (rule, key)
    # signature. Within this window the second emission is suppressed.
    # Rules whose RuleEvent.key is None bypass dedup entirely.
    dedup_window_minutes: float = 15.0
    # Age out rule_dedup entries older than this so the state file stays small.
    dedup_max_age_minutes: float = 60.0
    # Per review 2026-04-29-adhoc-1 #4: auto-blacklist a symbol after this
    # many same-(symbol, error) events in the rolling window. Threshold
    # defaults strictly higher than repeated_execution_errors (3) so the
    # alert fires first and the blacklist only engages on a sustained
    # loop. TTL is short by default — a transient broker constraint
    # shouldn't lock the symbol out for the whole day.
    auto_blacklist_threshold: int = 5
    auto_blacklist_window_hours: float = 4.0
    auto_blacklist_ttl_hours: float = 4.0


def _pid_alive_default(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _spawn_default(command: list[str], pid_file: Path) -> int:
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid_file.write_text(str(proc.pid))
    return proc.pid


def _read_pid(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        return int(raw.splitlines()[0].strip())
    except ValueError:
        return None


def _seconds_since(iso_ts: str | None, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


def run_one_tick(
    cfg: WatcherConfig,
    *,
    pid_alive: Callable[[int], bool] | None = None,
    spawn: Callable[[], int] | None = None,
    now: datetime | None = None,
) -> None:
    pid_alive = pid_alive or _pid_alive_default
    spawn = spawn or (lambda: _spawn_default(cfg.bot_command, cfg.bot_pid_file))
    now = now or datetime.now(timezone.utc)

    state = load_state(cfg.watcher_state_file)
    state_dirty = False
    # At most one restart_bot per tick — multiple rules (e.g. process_dead +
    # log_silence) commonly fire together; we only need to spawn once.
    restart_done_this_tick = False

    triggered: list[rules.RuleEvent] = []

    # Process liveness
    ev = rules.process_dead(pid_file=cfg.bot_pid_file, pid_alive=pid_alive, bot_log=cfg.bot_log)
    if ev is not None:
        triggered.append(ev)

    # Log silence (legacy, mtime-based). Per review 2026-05-02-adhoc-1
    # #5: retired in favour of heartbeat_silence. Setting
    # log_silence_minutes <= 0 disables; the rule is preserved only
    # for tests / back-compat overrides.
    if cfg.log_silence_minutes and cfg.log_silence_minutes > 0:
        ev = rules.log_silence(log_file=cfg.bot_log, max_silence_minutes=cfg.log_silence_minutes)
        if ev is not None:
            triggered.append(ev)

    # Trade-derived rules
    recent_events = journal.read_recent(cfg.journal_file)
    recent_trades = [e for e in recent_events if e.get("type") == "trade_close"]
    ev = rules.consecutive_losses(recent_trades=recent_trades, threshold=cfg.consecutive_loss_threshold)
    if ev is not None:
        triggered.append(ev)
    ev = rules.same_symbol_stop_streak(recent_trades=recent_trades, threshold=cfg.same_symbol_stop_threshold)
    if ev is not None:
        triggered.append(ev)

    # Daily drawdown
    today = time.strftime("%Y-%m-%d")
    rollup = rollups.compute_daily_rollup(cfg.journal_file, date=today)
    ev = rules.daily_drawdown_breach(rollup=rollup, max_loss_pct=cfg.daily_drawdown_pct)
    if ev is not None:
        triggered.append(ev)

    # Weekly drawdown
    ev = rules.weekly_drawdown_breach(recent_trades=recent_trades, max_loss_pct=cfg.weekly_drawdown_pct)
    if ev is not None:
        triggered.append(ev)

    # Confidence/outcome divergence
    ev = rules.confidence_outcome_divergence(recent_trades=recent_trades)
    if ev is not None:
        triggered.append(ev)

    # Repeated startup failures
    ev = rules.repeated_startup_failures(recent_triggers=recent_events,
                                          threshold=3, window_hours=1.0)
    if ev is not None:
        triggered.append(ev)

    # Disk log growth
    ev = rules.disk_log_growth(log_file=cfg.bot_log, max_mb=cfg.max_log_mb)
    if ev is not None:
        triggered.append(ev)

    # Bot-emitted events.jsonl based rules
    bot_events = _read_recent_events_file(cfg.events_file)
    # Heartbeat-based silence detector (per review 2026-04-29-adhoc-1 #7).
    # Quieter and more reliable than the file-mtime log_silence rule.
    if bot_events:
        ev = rules.heartbeat_silence(
            recent_events=bot_events,
            max_silence_minutes=cfg.heartbeat_silence_minutes,
        )
        if ev is not None:
            triggered.append(ev)
    # Per review 2026-05-05-adhoc-1 #3: shorter-window stall detector
    # gated on capital-at-risk. Loads the bot's runtime_state.json once
    # per tick. Silent if runtime_state is unavailable (we don't want
    # this rule competing with heartbeat_silence on the dead-bot case).
    if bot_events:
        runtime_state = _read_runtime_state(cfg.runtime_state_file)
        if runtime_state is not None:
            ev = rules.cycle_stall_with_open_position(
                recent_events=bot_events,
                runtime_state=runtime_state,
                max_silence_minutes=cfg.cycle_stall_with_open_position_minutes,
            )
            if ev is not None:
                triggered.append(ev)
    # Per review 2026-05-05-adhoc-1 #7: news_memory growth/age anomaly.
    # Reads the latest cycle_heartbeat's news_memory_size and
    # news_memory_oldest_published_at fields.
    if bot_events:
        ev = rules.news_memory_growth_anomaly(
            recent_events=bot_events,
            max_size=cfg.news_memory_max_size,
            max_age_hours=cfg.news_memory_max_age_hours,
        )
        if ev is not None:
            triggered.append(ev)
    if bot_events:
        # Per-symbol data-feed degradation streak
        seen_symbols: set[str] = set()
        for ev_rec in reversed(bot_events):
            if ev_rec.get("event") == "data_quality":
                sym = (ev_rec.get("data") or {}).get("symbol")
                if sym and sym not in seen_symbols:
                    seen_symbols.add(sym)
                    rule_ev = rules.data_feed_degraded(
                        recent_events=bot_events, symbol=sym, min_streak=5,
                        max_age_minutes=cfg.data_feed_max_age_minutes,
                    )
                    if rule_ev is not None:
                        triggered.append(rule_ev)
                    # Per review 2026-05-02-adhoc-1 #9: also check for
                    # last_good_ts plateau (feed correctly closed but
                    # indicators computed on stale data).
                    rule_ev = rules.data_feed_stale(
                        recent_events=bot_events, symbol=sym,
                        min_streak=cfg.data_feed_stale_min_streak,
                    )
                    if rule_ev is not None:
                        triggered.append(rule_ev)

        # Repeated execution errors on same (symbol, error) within 4h
        rule_ev = rules.repeated_execution_errors(
            recent_events=bot_events, threshold=3, window_hours=4.0,
        )
        if rule_ev is not None:
            triggered.append(rule_ev)

        # Auto-blacklist: if the same (symbol, error) has happened ≥5 times
        # in the last 4h, blacklist the symbol for 4h. Per review
        # 2026-04-29-adhoc-1 #4. Mechanical only; preserves the
        # alert_only rule above so the supervisor still gets the signal.
        rule_ev = rules.auto_blacklist_repeated_errors(
            recent_events=bot_events,
            threshold=cfg.auto_blacklist_threshold,
            window_hours=cfg.auto_blacklist_window_hours,
            ttl_hours=cfg.auto_blacklist_ttl_hours,
        )
        if rule_ev is not None:
            triggered.append(rule_ev)

        # Risk-gate degraded streak (e.g. spread-check bypassed during outages)
        rule_ev = rules.risk_gate_degraded_streak(
            recent_events=bot_events, threshold=3, window_hours=1.0,
        )
        if rule_ev is not None:
            triggered.append(rule_ev)

        # Subagent failure streak (per review 2026-04-29-adhoc-1 #9).
        # Phase 0.4 roadmap re-baseline (2026-05): threshold raised 3→5.
        # The 491 firings over 2026-04/05 were dominated by OpenRouter free-tier
        # 402/429 errors, eliminated by the NVIDIA NIM provider swap (commit
        # 2c92bb7). Residual single JSON-salvage events (commit 9c1a25a) are
        # recovered in-cycle and should not trip an alert; 5-in-30min still
        # catches a genuine provider outage.
        rule_ev = rules.subagent_failure_streak(
            recent_events=bot_events, threshold=5, window_minutes=30.0,
        )
        if rule_ev is not None:
            triggered.append(rule_ev)

    # Apply remediations
    for ev in triggered:
        # Generic per-rule emission dedup. Rules with key=None bypass.
        # Suppresses both the journal entry AND the downstream action so a
        # noisy rule cannot spam rule_trigger entries every tick (incident
        # 2026-04-29: data_feed_degraded fired 87× in 30 min on a stale
        # 23-cycle NG=F streak).
        if ev.key is not None:
            dedup_id = f"{ev.rule}::{ev.key}"
            last_iso = state.rule_dedup.get(dedup_id)
            elapsed = _seconds_since(last_iso, now)
            if (elapsed is not None
                    and elapsed < cfg.dedup_window_minutes * 60.0):
                continue  # silent skip — no journal, no action
            state.rule_dedup[dedup_id] = now.isoformat()
            state_dirty = True
        journal.append_event(cfg.journal_file, {"type": "rule_trigger",
                                                  "rule": ev.rule, "action": ev.action,
                                                  "detail": ev.detail})
        if ev.action == "kill_switch":
            remediations.touch_kill_switch(cfg.kill_switch_file)
            journal.append_event(cfg.journal_file, {"type": "remediation",
                                                      "action": "kill_switch", "rule": ev.rule})
        elif ev.action == "blacklist_symbol":
            sym = ev.detail.get("symbol")
            ttl = float(ev.detail.get("ttl_hours", 24))
            if sym:
                remediations.blacklist_symbol_action(cfg.blacklist_file, symbol=sym,
                                                     reason=ev.rule, ttl_hours=ttl)
                journal.append_event(cfg.journal_file, {"type": "remediation",
                                                          "action": "blacklist_symbol",
                                                          "symbol": sym, "ttl_hours": ttl,
                                                          "reason": ev.rule})
        elif ev.action == "restart_bot":
            # Guard 0: at most one restart_bot per tick.
            if restart_done_this_tick:
                journal.append_event(
                    cfg.journal_file,
                    {
                        "type": "remediation_suppressed",
                        "action": "restart_bot",
                        "rule": ev.rule,
                        "reason": "already_restarted_this_tick",
                    },
                )
                continue
            # Guard 1: cooldown — once we issue restart_bot, suppress further
            # restart_bot actions for cfg.restart_cooldown_seconds.
            cd_remaining: float | None = None
            elapsed = _seconds_since(state.last_restart_at, now)
            if elapsed is not None and elapsed < cfg.restart_cooldown_seconds:
                cd_remaining = cfg.restart_cooldown_seconds - elapsed
                journal.append_event(
                    cfg.journal_file,
                    {
                        "type": "remediation_suppressed",
                        "action": "restart_bot",
                        "rule": ev.rule,
                        "reason": "cooldown_active",
                        "cooldown_remaining_seconds": round(cd_remaining, 2),
                    },
                )
                continue
            # Guard 2: liveness — if the previous bot.pid is still alive, the
            # bot is just slow to log; don't spawn a second instance.
            existing_pid = _read_pid(cfg.bot_pid_file)
            if existing_pid is not None and pid_alive(existing_pid):
                journal.append_event(
                    cfg.journal_file,
                    {
                        "type": "remediation_suppressed",
                        "action": "restart_bot",
                        "rule": ev.rule,
                        "reason": "previous_pid_still_alive",
                        "pid": existing_pid,
                    },
                )
                continue
            spawn()
            restart_done_this_tick = True
            state.last_restart_at = now.isoformat()
            state_dirty = True
            journal.append_event(cfg.journal_file, {"type": "remediation",
                                                      "action": "restart_bot", "rule": ev.rule})
        elif ev.action == "rotate_log":
            try:
                archive = remediations.rotate_log(cfg.bot_log)
                journal.append_event(cfg.journal_file, {"type": "remediation",
                                                          "action": "rotate_log",
                                                          "archive": str(archive)})
            except FileNotFoundError:
                pass
        # alert_only: journal trigger is enough

    # Housekeeping: expire blacklist entries
    blacklist.remove_expired(cfg.blacklist_file)

    # Housekeeping: per-cycle decision-log retention (review
    # 2026-04-29-adhoc-1 #8). Throttle to at most once per
    # decision_log_rotation_interval_hours so the rotation doesn't run
    # on every tick.
    if cfg.decision_log_dir is not None:
        elapsed_since_rot = _seconds_since(state.last_decision_log_rotation_at, now)
        rotation_interval_seconds = cfg.decision_log_rotation_interval_hours * 3600.0
        if (elapsed_since_rot is None
                or elapsed_since_rot >= rotation_interval_seconds):
            try:
                counters = remediations.rotate_decision_logs(
                    cfg.decision_log_dir,
                    gzip_after_days=cfg.decision_log_gzip_after_days,
                    delete_after_days=cfg.decision_log_delete_after_days,
                    now=now,
                )
                state.last_decision_log_rotation_at = now.isoformat()
                state_dirty = True
                if counters.get("gzipped") or counters.get("deleted"):
                    journal.append_event(
                        cfg.journal_file,
                        {"type": "remediation", "action": "rotate_decision_logs",
                         "counters": counters},
                    )
            except Exception as exc:
                LOGGER.warning("decision-log rotation failed: %s", exc)

    # Housekeeping: events.jsonl daily rotation (review
    # 2026-04-30-adhoc-1 #4). Same throttle approach as the decision
    # logs above. Cheap operation (one read + one write per day).
    elapsed_since_ev_rot = _seconds_since(state.last_events_rotation_at, now)
    ev_interval = cfg.events_rotation_interval_hours * 3600.0
    if (elapsed_since_ev_rot is None
            or elapsed_since_ev_rot >= ev_interval):
        try:
            ev_counters = remediations.rotate_events_jsonl(
                cfg.events_file,
                delete_after_days=cfg.events_delete_after_days,
                now=now,
            )
            state.last_events_rotation_at = now.isoformat()
            state_dirty = True
            if ev_counters.get("rotated") or ev_counters.get("deleted"):
                journal.append_event(
                    cfg.journal_file,
                    {"type": "remediation", "action": "rotate_events_jsonl",
                     "counters": ev_counters},
                )
        except Exception as exc:
            LOGGER.warning("events.jsonl rotation failed: %s", exc)

    # Per review 2026-04-30-adhoc-1 #6: emit a watcher_heartbeat to
    # raw_journal once per ``watcher_heartbeat_interval_minutes``. Lets
    # operators verify the watcher is alive even when no rules have
    # fired (the journal otherwise stays silent for many hours during
    # healthy operation, indistinguishable from a dead watcher).
    elapsed_since_hb = _seconds_since(state.last_heartbeat_at, now)
    hb_interval_seconds = cfg.watcher_heartbeat_interval_minutes * 60.0
    if elapsed_since_hb is None or elapsed_since_hb >= hb_interval_seconds:
        try:
            journal.append_event(
                cfg.journal_file,
                {
                    "type": "watcher_heartbeat",
                    "pid": os.getpid(),
                    "interval_minutes": cfg.watcher_heartbeat_interval_minutes,
                },
            )
            state.last_heartbeat_at = now.isoformat()
            state_dirty = True
        except Exception as exc:
            LOGGER.warning("watcher_heartbeat journal write failed: %s", exc)

    # Housekeeping: age out stale rule_dedup entries so the state file
    # stays small. Anything older than dedup_max_age_minutes is purged.
    if state.rule_dedup:
        max_age_seconds = cfg.dedup_max_age_minutes * 60.0
        stale_ids = [
            dedup_id for dedup_id, ts_iso in state.rule_dedup.items()
            if (elapsed := _seconds_since(ts_iso, now)) is None
            or elapsed > max_age_seconds
        ]
        if stale_ids:
            for dedup_id in stale_ids:
                state.rule_dedup.pop(dedup_id, None)
            state_dirty = True

    if state_dirty:
        save_state(cfg.watcher_state_file, state)


_STOP = False


def _handle_sig(signum, frame):
    global _STOP
    _STOP = True


def run_loop(cfg: WatcherConfig) -> None:
    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)
    while not _STOP:
        try:
            run_one_tick(cfg)
        except Exception:
            LOGGER.exception("watcher tick failed")
        time.sleep(cfg.poll_interval_seconds)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="supervisor.watcher")
    p.add_argument("--bot-pid-file", type=Path, default=Path("bot.pid"))
    # Per review 2026-04-30-adhoc-1 #5: default to BOT_DIR/logs/bot.log
    # (persistent across OCI reboots, OneDrive-synced) rather than
    # /tmp/cfd_bot.log (ephemeral).
    p.add_argument("--bot-log", type=Path, default=Path("logs/bot.log"))
    p.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    p.add_argument("--journal", type=Path, default=Path("supervisor/raw_journal.jsonl"))
    p.add_argument("--blacklist", type=Path, default=Path("supervisor/blacklist.json"))
    p.add_argument("--kill-switch", type=Path, default=Path("kill_switch"))
    p.add_argument("--state", type=Path, default=Path("supervisor/watcher_state.json"))
    p.add_argument("--interval", type=float, default=30.0)
    # Per-cycle decision-log directory (review 2026-04-29-adhoc-1 #8). Pass
    # the same path the bot writes to so the watcher can gzip/expire
    # files. Pass empty string to disable.
    p.add_argument("--decision-log-dir", type=str, default="logs")
    p.add_argument("--bot-command", nargs="+", default=[
        ".venv/bin/python", "-m", "trading_bot", "--log-level", "INFO",
        "--log-file", "logs/bot.log",
    ])
    p.add_argument("--once", action="store_true", help="Run a single tick and exit (for testing)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)
    decision_log_dir = (
        Path(args.decision_log_dir) if args.decision_log_dir else None
    )
    cfg = WatcherConfig(
        bot_pid_file=args.bot_pid_file,
        bot_log=args.bot_log,
        events_file=args.events,
        journal_file=args.journal,
        blacklist_file=args.blacklist,
        kill_switch_file=args.kill_switch,
        watcher_state_file=args.state,
        bot_command=args.bot_command,
        poll_interval_seconds=args.interval,
        decision_log_dir=decision_log_dir,
    )
    if args.once:
        run_one_tick(cfg)
    else:
        run_loop(cfg)


if __name__ == "__main__":
    main()
