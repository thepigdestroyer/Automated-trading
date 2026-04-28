from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from supervisor import journal, blacklist, rollups, rules, remediations


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
    consecutive_loss_threshold: int = 3
    log_silence_minutes: float = 20.0
    daily_drawdown_pct: float = 3.0
    weekly_drawdown_pct: float = 6.0
    same_symbol_stop_threshold: int = 3
    poll_interval_seconds: float = 30.0
    max_restarts_per_hour: int = 3
    max_log_mb: float = 100.0


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


def run_one_tick(
    cfg: WatcherConfig,
    *,
    pid_alive: Callable[[int], bool] | None = None,
    spawn: Callable[[], int] | None = None,
) -> None:
    pid_alive = pid_alive or _pid_alive_default
    spawn = spawn or (lambda: _spawn_default(cfg.bot_command, cfg.bot_pid_file))

    triggered: list[rules.RuleEvent] = []

    # Process liveness
    ev = rules.process_dead(pid_file=cfg.bot_pid_file, pid_alive=pid_alive)
    if ev is not None:
        triggered.append(ev)

    # Log silence
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
                    )
                    if rule_ev is not None:
                        triggered.append(rule_ev)

        # Repeated execution errors on same (symbol, error) within 4h
        rule_ev = rules.repeated_execution_errors(
            recent_events=bot_events, threshold=3, window_hours=4.0,
        )
        if rule_ev is not None:
            triggered.append(rule_ev)

        # Risk-gate degraded streak (e.g. spread-check bypassed during outages)
        rule_ev = rules.risk_gate_degraded_streak(
            recent_events=bot_events, threshold=3, window_hours=1.0,
        )
        if rule_ev is not None:
            triggered.append(rule_ev)

    # Apply remediations
    for ev in triggered:
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
            spawn()
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
    p.add_argument("--bot-log", type=Path, default=Path("/tmp/cfd_bot.log"))
    p.add_argument("--events", type=Path, default=Path("logs/events.jsonl"))
    p.add_argument("--journal", type=Path, default=Path("supervisor/raw_journal.jsonl"))
    p.add_argument("--blacklist", type=Path, default=Path("supervisor/blacklist.json"))
    p.add_argument("--kill-switch", type=Path, default=Path("kill_switch"))
    p.add_argument("--state", type=Path, default=Path("supervisor/watcher_state.json"))
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--bot-command", nargs="+", default=[
        ".venv/bin/python", "-m", "t212_cfd_bot", "--log-level", "INFO",
        "--log-file", "/tmp/cfd_bot.log",
    ])
    p.add_argument("--once", action="store_true", help="Run a single tick and exit (for testing)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)
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
    )
    if args.once:
        run_one_tick(cfg)
    else:
        run_loop(cfg)


if __name__ == "__main__":
    main()
