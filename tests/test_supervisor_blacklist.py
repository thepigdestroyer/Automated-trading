import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from supervisor.blacklist import add_symbol, read_active, remove_expired, clear_symbol


def test_add_and_read_active(tmp_path):
    p = tmp_path / "bl.json"
    add_symbol(p, "NVDA", reason="3 stops", ttl_hours=24)
    active = read_active(p)
    assert active == {"NVDA"}


def test_expired_entries_filtered(tmp_path):
    p = tmp_path / "bl.json"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    p.write_text(json.dumps({
        "symbols": {"OLD": {"added": past, "reason": "x", "expires": past}}
    }))
    active = read_active(p)
    assert active == set()


def test_remove_expired_drops_them(tmp_path):
    p = tmp_path / "bl.json"
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    p.write_text(json.dumps({
        "symbols": {
            "OLD": {"added": past, "reason": "x", "expires": past},
            "NEW": {"added": past, "reason": "y", "expires": future},
        }
    }))
    remove_expired(p)
    data = json.loads(p.read_text())
    assert "OLD" not in data["symbols"]
    assert "NEW" in data["symbols"]


def test_clear_symbol(tmp_path):
    p = tmp_path / "bl.json"
    add_symbol(p, "NVDA", reason="x", ttl_hours=24)
    clear_symbol(p, "NVDA")
    assert read_active(p) == set()


def test_atomic_write_safe(tmp_path):
    p = tmp_path / "bl.json"
    add_symbol(p, "AAA", reason="x", ttl_hours=1)
    add_symbol(p, "BBB", reason="y", ttl_hours=1)
    # Both should be present, no corruption
    assert read_active(p) == {"AAA", "BBB"}
