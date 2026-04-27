import json
from pathlib import Path
from datetime import datetime, timezone

from supervisor.journal import append_event, read_recent


def test_append_event_writes_json_line(tmp_path):
    p = tmp_path / "j.jsonl"
    append_event(p, {"type": "cycle", "cycle_id": "c_1", "symbols": ["NVDA"]})
    line = p.read_text(encoding="utf-8").strip()
    obj = json.loads(line)
    assert obj["type"] == "cycle"
    assert obj["cycle_id"] == "c_1"
    assert "ts" in obj  # writer adds timestamp


def test_append_multiple_events(tmp_path):
    p = tmp_path / "j.jsonl"
    append_event(p, {"type": "cycle", "cycle_id": "c_1"})
    append_event(p, {"type": "trade_open", "symbol": "NVDA", "side": "sell"})
    lines = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["type"] == "cycle"
    assert lines[1]["type"] == "trade_open"


def test_read_recent_returns_events_after_cutoff(tmp_path):
    p = tmp_path / "j.jsonl"
    append_event(p, {"type": "cycle", "cycle_id": "old"})
    cutoff = datetime.now(timezone.utc).isoformat()
    append_event(p, {"type": "cycle", "cycle_id": "new"})
    events = read_recent(p, since=cutoff)
    assert len(events) == 1
    assert events[0]["cycle_id"] == "new"
