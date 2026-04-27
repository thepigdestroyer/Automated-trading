from pathlib import Path

from supervisor.watcher import run_one_tick, WatcherConfig


def test_run_one_tick_dead_process_writes_remediation(tmp_path):
    cfg = WatcherConfig(
        bot_pid_file=tmp_path / "bot.pid",
        bot_log=tmp_path / "bot.log",
        events_file=tmp_path / "events.jsonl",
        journal_file=tmp_path / "raw_journal.jsonl",
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill",
        watcher_state_file=tmp_path / "ws.json",
        consecutive_loss_threshold=3,
        log_silence_minutes=20,
        daily_drawdown_pct=3.0,
        weekly_drawdown_pct=6.0,
        same_symbol_stop_threshold=3,
        max_log_mb=100.0,
        bot_command=["/bin/true"],
    )
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=lambda: 99999)
    journal_lines = cfg.journal_file.read_text().splitlines() if cfg.journal_file.exists() else []
    assert any("rule_trigger" in l and "process_dead" in l for l in journal_lines)


def test_run_one_tick_consecutive_losses_triggers_kill_switch(tmp_path):
    journal = tmp_path / "raw_journal.jsonl"
    from supervisor.journal import append_event
    for i in range(3):
        append_event(journal, {"type": "trade_close", "symbol": "NVDA",
                                "exit": 100, "pnl_pct": -1.0, "reason": "stop", "held_minutes": 30})
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("12345")
    log = tmp_path / "bot.log"
    log.write_text("hi")
    cfg = WatcherConfig(
        bot_pid_file=pid_file,
        bot_log=log,
        events_file=tmp_path / "events.jsonl",
        journal_file=journal,
        blacklist_file=tmp_path / "blacklist.json",
        kill_switch_file=tmp_path / "kill",
        watcher_state_file=tmp_path / "ws.json",
        consecutive_loss_threshold=3,
        log_silence_minutes=20,
        daily_drawdown_pct=3.0,
        weekly_drawdown_pct=6.0,
        same_symbol_stop_threshold=3,
        max_log_mb=100.0,
        bot_command=["/bin/true"],
    )
    run_one_tick(cfg, pid_alive=lambda pid: True, spawn=lambda: 0)
    assert cfg.kill_switch_file.exists(), "kill switch file should have been created"
