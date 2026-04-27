import json
from datetime import datetime, timedelta, timezone

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
