from supervisor.state import WatcherState, save_state, load_state


def test_state_round_trip(tmp_path):
    p = tmp_path / "ws.json"
    s = WatcherState(last_kill_switch_at="2026-04-27T13:00:00+00:00",
                     last_alert_at="2026-04-27T13:01:00+00:00",
                     restart_attempts_last_hour=2)
    save_state(p, s)
    loaded = load_state(p)
    assert loaded == s


def test_state_default_when_missing(tmp_path):
    s = load_state(tmp_path / "missing.json")
    assert s.last_kill_switch_at is None
    assert s.restart_attempts_last_hour == 0
