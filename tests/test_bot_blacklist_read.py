import json
from datetime import datetime, timedelta, timezone

from t212_cfd_bot.config import Settings
from t212_cfd_bot.supervisor_blacklist import read_active_blacklist


def test_returns_active_symbols(tmp_path):
    p = tmp_path / "bl.json"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    p.write_text(json.dumps({
        "symbols": {
            "ACTIVE": {"added": past, "reason": "x", "expires": future},
            "EXPIRED": {"added": past, "reason": "y", "expires": past},
        }
    }))
    assert read_active_blacklist(p) == {"ACTIVE"}


def test_missing_file_returns_empty(tmp_path):
    assert read_active_blacklist(tmp_path / "nope.json") == set()


def test_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "bl.json"
    p.write_text("not json")
    assert read_active_blacklist(p) == set()


def test_settings_blacklist_path_is_resolved():
    s = Settings()
    assert "supervisor" in str(s.supervisor_blacklist_path)
    assert "blacklist" in str(s.supervisor_blacklist_path)


def test_bot_skips_blacklisted_symbol(tmp_path):
    """Blacklist entry for SPY should remove it from a candidate list.

    Done at the unit level by checking the read_active_blacklist set is honoured."""
    p = tmp_path / "bl.json"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    p.write_text(json.dumps({
        "symbols": {"SPY": {"added": "2026-04-27T00:00:00+00:00", "reason": "x", "expires": future}}
    }))
    bl = read_active_blacklist(p)
    candidates = ["SPY", "NVDA", "WTI"]
    kept = [s for s in candidates if s not in bl]
    assert kept == ["NVDA", "WTI"]
