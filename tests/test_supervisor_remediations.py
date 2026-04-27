import gzip
import json
from pathlib import Path

from supervisor.remediations import (
    touch_kill_switch, blacklist_symbol_action, rotate_log,
)


def test_touch_kill_switch_creates_file(tmp_path):
    sw = tmp_path / "kill"
    touch_kill_switch(sw)
    assert sw.exists()


def test_touch_kill_switch_idempotent(tmp_path):
    sw = tmp_path / "kill"
    touch_kill_switch(sw)
    touch_kill_switch(sw)  # second call no error
    assert sw.exists()


def test_blacklist_symbol_action_writes_entry(tmp_path):
    bl = tmp_path / "bl.json"
    blacklist_symbol_action(bl, symbol="NVDA", reason="3 stops", ttl_hours=24)
    data = json.loads(bl.read_text())
    assert "NVDA" in data["symbols"]


def test_rotate_log_compresses(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("hello\n" * 100, encoding="utf-8")
    archive = rotate_log(log)
    assert archive.exists()
    assert archive.suffix == ".gz"
    assert log.exists()
    assert log.stat().st_size == 0
    with gzip.open(archive, "rt") as f:
        assert "hello" in f.read()


from supervisor.remediations import RestartManager


def test_restart_manager_allows_initial_attempts():
    fake_calls = []
    rm = RestartManager(max_per_hour=3,
                        spawn=lambda: fake_calls.append("spawn") or 12345)
    pid = rm.attempt_restart()
    assert pid == 12345
    assert fake_calls == ["spawn"]


def test_restart_manager_blocks_after_cap():
    fake_calls = []
    rm = RestartManager(max_per_hour=2,
                        spawn=lambda: fake_calls.append("spawn") or 12345)
    rm.attempt_restart()
    rm.attempt_restart()
    pid3 = rm.attempt_restart()
    assert pid3 is None
    assert len(fake_calls) == 2
