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
