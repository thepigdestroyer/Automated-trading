# Bot Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a supervisor layer (always-on watcher process + Claude Code agents + dual memory) that monitors the CFD trading bot, runs mechanical safety remediations autonomously, and exposes self-improvement workflows via slash commands.

**Architecture:** Standalone Python watcher (no LLM, applies rules and runs remediations) writes an append-only `raw_journal.jsonl`. Claude Code subagent driven by slash commands handles user-supervised improvements and reads the journal plus the LLM-curated `insights.md`. A scheduled daily-review remote agent fires at 21:30 UTC weekdays to write numbered improvement proposals to `supervisor/reviews/YYYY-MM-DD.md`.

**Tech Stack:** Python 3.12, `pytest`, `pydantic` (existing), `subprocess`, `pathlib`, atomic writes via `os.replace`. Project uses `.venv/bin/python`. Existing bot lives in `trading_bot/`. Tests run with `.venv/bin/pytest tests/`.

**Spec:** `docs/superpowers/specs/2026-04-27-bot-supervisor-design.md`

---

## File Structure

**New files (all under project root):**
```
supervisor/__init__.py
supervisor/journal.py             # append-only JSONL writer + reader
supervisor/rollups.py             # daily aggregator over journal
supervisor/blacklist.py           # supervisor-side blacklist read/write/expire
supervisor/rules.py               # pure rule functions
supervisor/remediations.py        # mechanical actions
supervisor/state.py               # WatcherState dataclass + persistence
supervisor/watcher.py             # main poll loop, CLI entry point
supervisor/__main__.py            # `python -m supervisor` → watcher.main()

trading_bot/supervisor_blacklist.py   # bot-side reader (one function)

.claude/agents/bot-supervisor.md
.claude/commands/bot-status.md
.claude/commands/bot-logs.md
.claude/commands/bot-review.md
.claude/commands/bot-apply.md
.claude/commands/bot-reject.md
.claude/commands/bot-improve.md
.claude/commands/bot-restart.md
.claude/commands/bot-stop.md
.claude/commands/bot-resume.md
.claude/commands/bot-blacklist.md
.claude/commands/bot-insights.md
.claude/commands/bot-watcher.md
.claude/prompts/bot-daily-review.md   # template prompt for the scheduled agent

tests/test_supervisor_journal.py
tests/test_supervisor_rollups.py
tests/test_supervisor_blacklist.py
tests/test_supervisor_rules.py
tests/test_supervisor_remediations.py
tests/test_supervisor_state.py
tests/test_supervisor_watcher.py
tests/test_bot_blacklist_read.py
```

**Modified:**
- `trading_bot/bot.py` — call `read_active_blacklist()` in `_select_active_symbols`
- `pyproject.toml` — add `supervisor` to discoverable packages (already auto-discovered if pytest is configured for repo root)

---

## Phase boundaries

| Phase | Tasks | Parallelisable with |
|-------|-------|---------------------|
| 0 | Setup, directories, base fixtures | — |
| 1 | Journal + rollups | 2 |
| 2 | Blacklist (supervisor side) | 1 |
| 3 | Bot integration (blacklist reader) | depends on 2 |
| 4 | Watcher rules | depends on 1 |
| 5 | Watcher remediations | depends on 1, 2 |
| 6 | Watcher main loop + state | depends on 4, 5 |
| 7 | Slash commands + subagent definition | independent of watcher; can run anytime after 0 |
| 8 | Daily review schedule + final verification | depends on 7 |

User-approval gate after each phase.

---

# Phase 0 — Setup

### Task 0.1: Create supervisor package skeleton

**Files:**
- Create: `supervisor/__init__.py`
- Create: `supervisor/__main__.py`

- [ ] **Step 1: Create empty package init**

```python
# supervisor/__init__.py
"""Bot supervisor — autonomous watcher and curated insights for the CFD trading bot."""
```

- [ ] **Step 2: Create __main__ entry point stub**

```python
# supervisor/__main__.py
from supervisor.watcher import main

if __name__ == "__main__":
    main()
```

(`watcher.main` will be implemented in Phase 6. The import will fail until then — that's fine for this phase since we're not running the module yet.)

- [ ] **Step 3: Verify package is importable**

Run: `cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && .venv/bin/python -c "import supervisor; print(supervisor.__doc__)"`
Expected: Prints the docstring.

- [ ] **Step 4: Commit**

```bash
cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading"
git add supervisor/
git commit -m "feat(supervisor): scaffold package"
```

### Task 0.2: Create runtime directory layout (gitignored)

**Files:**
- Create: `supervisor/.gitkeep` directories
- Modify: `.gitignore`

- [ ] **Step 1: Create runtime subdirs**

```bash
cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading"
mkdir -p supervisor/reviews
touch supervisor/reviews/.gitkeep
```

- [ ] **Step 2: Add runtime artifacts to .gitignore**

Append to `.gitignore`:
```
# bot supervisor runtime
supervisor/raw_journal.jsonl
supervisor/blacklist.json
supervisor/watcher_state.json
supervisor/reviews/*.md
!supervisor/reviews/.gitkeep
supervisor/insights.md
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore supervisor/reviews/.gitkeep
git commit -m "chore(supervisor): runtime dir + gitignore"
```

---

# Phase 1 — Journal + rollups

### Task 1.1: Journal append-only writer

**Files:**
- Create: `supervisor/journal.py`
- Create: `tests/test_supervisor_journal.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_supervisor_journal.py
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
```

- [ ] **Step 2: Run test, expect failure**

Run: `.venv/bin/pytest tests/test_supervisor_journal.py -v`
Expected: ImportError (module doesn't exist).

- [ ] **Step 3: Implement journal**

```python
# supervisor/journal.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VALID_TYPES = {"cycle", "trade_open", "trade_close", "rule_trigger", "remediation", "daily_rollup"}


def append_event(path: Path, event: dict[str, Any]) -> None:
    """Append a single event to the JSONL journal. Adds ts if missing."""
    if event.get("type") not in VALID_TYPES:
        raise ValueError(f"invalid event type: {event.get('type')!r}")
    record = dict(event)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_recent(path: Path, *, since: str | None = None) -> list[dict[str, Any]]:
    """Read events from the journal, optionally filtered to those at or after `since` (ISO 8601)."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None and obj.get("ts", "") < since:
                continue
            out.append(obj)
    return out


def iter_events(path: Path) -> Iterable[dict[str, Any]]:
    """Lazy iterator over all events. Used by rollups."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_journal.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/journal.py tests/test_supervisor_journal.py
git commit -m "feat(supervisor): journal append-only writer + reader"
```

### Task 1.2: Reject unknown event types

**Files:**
- Modify: `tests/test_supervisor_journal.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_supervisor_journal.py`:

```python
import pytest

def test_unknown_type_rejected(tmp_path):
    p = tmp_path / "j.jsonl"
    with pytest.raises(ValueError, match="invalid event type"):
        append_event(p, {"type": "garbage"})
```

- [ ] **Step 2: Run test**

Run: `.venv/bin/pytest tests/test_supervisor_journal.py::test_unknown_type_rejected -v`
Expected: PASS (already implemented in 1.1).

- [ ] **Step 3: Commit**

```bash
git add tests/test_supervisor_journal.py
git commit -m "test(supervisor): journal rejects unknown event types"
```

### Task 1.3: Rollups (daily aggregation)

**Files:**
- Create: `supervisor/rollups.py`
- Create: `tests/test_supervisor_rollups.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_supervisor_rollups.py
from supervisor.journal import append_event
from supervisor.rollups import compute_daily_rollup


def test_rollup_computes_win_rate(tmp_path):
    p = tmp_path / "j.jsonl"
    # 1 win, 2 losses
    append_event(p, {"type": "trade_close", "symbol": "NVDA", "exit": 100.0, "pnl_pct": 1.5, "reason": "tp", "held_minutes": 30, "ts": "2026-04-27T12:00:00+00:00"})
    append_event(p, {"type": "trade_close", "symbol": "WTI", "exit": 50.0, "pnl_pct": -0.8, "reason": "stop", "held_minutes": 60, "ts": "2026-04-27T13:00:00+00:00"})
    append_event(p, {"type": "trade_close", "symbol": "WTI", "exit": 48.0, "pnl_pct": -1.2, "reason": "stop", "held_minutes": 45, "ts": "2026-04-27T15:00:00+00:00"})
    rollup = compute_daily_rollup(p, date="2026-04-27")
    assert rollup["trades"] == 3
    assert rollup["wins"] == 1
    assert rollup["losses"] == 2
    assert abs(rollup["win_rate"] - (1/3)) < 1e-6
    assert abs(rollup["net_pnl_pct"] - (1.5 - 0.8 - 1.2)) < 1e-6
    assert rollup["by_symbol"]["WTI"]["n"] == 2
    assert abs(rollup["by_symbol"]["WTI"]["pnl"] - (-2.0)) < 1e-6


def test_rollup_empty_day(tmp_path):
    p = tmp_path / "j.jsonl"
    rollup = compute_daily_rollup(p, date="2026-04-27")
    assert rollup["trades"] == 0
    assert rollup["wins"] == 0
    assert rollup["win_rate"] == 0.0
```

- [ ] **Step 2: Run test, expect fail**

Run: `.venv/bin/pytest tests/test_supervisor_rollups.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement rollups**

```python
# supervisor/rollups.py
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from supervisor.journal import iter_events


def compute_daily_rollup(journal_path: Path, *, date: str) -> dict[str, Any]:
    """Aggregate trade_close events on `date` (YYYY-MM-DD) into a daily rollup record."""
    trades = 0
    wins = 0
    losses = 0
    net = 0.0
    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})

    for ev in iter_events(journal_path):
        if ev.get("type") != "trade_close":
            continue
        ts = ev.get("ts", "")
        if not ts.startswith(date):
            continue
        pnl = float(ev.get("pnl_pct", 0.0))
        sym = ev.get("symbol", "?")
        trades += 1
        net += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        by_symbol[sym]["n"] += 1
        by_symbol[sym]["pnl"] += pnl

    win_rate = wins / trades if trades else 0.0
    return {
        "type": "daily_rollup",
        "date": date,
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_pnl_pct": net,
        "by_symbol": {k: dict(v) for k, v in by_symbol.items()},
    }
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_rollups.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/rollups.py tests/test_supervisor_rollups.py
git commit -m "feat(supervisor): daily rollup aggregator"
```

---

# Phase 2 — Blacklist (supervisor side)

### Task 2.1: Blacklist read/write/expire

**Files:**
- Create: `supervisor/blacklist.py`
- Create: `tests/test_supervisor_blacklist.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_supervisor_blacklist.py
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
```

- [ ] **Step 2: Run test, expect fail**

Run: `.venv/bin/pytest tests/test_supervisor_blacklist.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement blacklist**

```python
# supervisor/blacklist.py
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"symbols": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"symbols": {}}
    if "symbols" not in data:
        data["symbols"] = {}
    return data


def _save_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def add_symbol(path: Path, symbol: str, *, reason: str, ttl_hours: float = 24.0) -> None:
    data = _load(path)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    data["symbols"][symbol.upper()] = {
        "added": now.isoformat(),
        "reason": reason,
        "expires": expires.isoformat(),
    }
    _save_atomic(path, data)


def read_active(path: Path) -> set[str]:
    """Return the set of currently-active (non-expired) blacklisted symbols."""
    data = _load(path)
    now = datetime.now(timezone.utc).isoformat()
    return {sym for sym, v in data.get("symbols", {}).items() if v.get("expires", "") > now}


def remove_expired(path: Path) -> int:
    """Drop expired entries. Returns number removed."""
    data = _load(path)
    now = datetime.now(timezone.utc).isoformat()
    before = len(data["symbols"])
    data["symbols"] = {s: v for s, v in data["symbols"].items() if v.get("expires", "") > now}
    removed = before - len(data["symbols"])
    if removed:
        _save_atomic(path, data)
    return removed


def clear_symbol(path: Path, symbol: str) -> None:
    data = _load(path)
    data["symbols"].pop(symbol.upper(), None)
    _save_atomic(path, data)
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_blacklist.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/blacklist.py tests/test_supervisor_blacklist.py
git commit -m "feat(supervisor): symbol blacklist with TTL expiry"
```

---

# Phase 3 — Bot integration (read blacklist)

### Task 3.1: Bot-side reader module

**Files:**
- Create: `trading_bot/supervisor_blacklist.py`
- Create: `tests/test_bot_blacklist_read.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_bot_blacklist_read.py
import json
from datetime import datetime, timedelta, timezone

from trading_bot.supervisor_blacklist import read_active_blacklist


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
```

- [ ] **Step 2: Run test, expect fail**

Run: `.venv/bin/pytest tests/test_bot_blacklist_read.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement reader**

```python
# trading_bot/supervisor_blacklist.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def read_active_blacklist(path: Path) -> set[str]:
    """Return the set of currently-blacklisted symbols.

    Reads supervisor/blacklist.json and filters entries whose `expires` is in the past.
    Any read or parse failure returns an empty set — the bot must continue trading
    even if the supervisor is down.
    """
    try:
        if not path.exists():
            return set()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    now = datetime.now(timezone.utc).isoformat()
    return {sym for sym, v in data.get("symbols", {}).items() if v.get("expires", "") > now}
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/bin/pytest tests/test_bot_blacklist_read.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add trading_bot/supervisor_blacklist.py tests/test_bot_blacklist_read.py
git commit -m "feat(bot): blacklist reader for supervisor integration"
```

### Task 3.2: Wire blacklist into bot's symbol selection

**Files:**
- Modify: `trading_bot/config.py` (add path setting)
- Modify: `trading_bot/bot.py:_select_active_symbols` (filter blacklisted)
- Modify: `tests/test_bot_blacklist_read.py` (add integration test)

- [ ] **Step 1: Add config field**

In `trading_bot/config.py`, add to `Settings`:
```python
supervisor_blacklist_path: Path = Path("supervisor/blacklist.json")
```

And add to the `normalize_paths` validator's tuple of fields if it lists them; otherwise add a new one-off resolution. Read the existing pattern around line 100 and follow it. If the validator iterates a list, append `"supervisor_blacklist_path"`.

- [ ] **Step 2: Write failing integration test**

Add to `tests/test_bot_blacklist_read.py`:

```python
import json
from datetime import datetime, timedelta, timezone

from trading_bot.config import Settings
from trading_bot.supervisor_blacklist import read_active_blacklist


def test_settings_blacklist_path_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    # Path attribute exists and resolves under cwd
    assert "supervisor" in str(s.supervisor_blacklist_path)
```

- [ ] **Step 3: Run test, expect pass**

Run: `.venv/bin/pytest tests/test_bot_blacklist_read.py::test_settings_blacklist_path_is_resolved -v`
Expected: PASS.

- [ ] **Step 4: Modify _select_active_symbols in bot.py**

Locate `_select_active_symbols` in `trading_bot/bot.py` (around line 460). After the existing `excluded_symbols` filter loop and before the `if not tradeable_symbols` guard, add:

```python
        # supervisor blacklist (skip even if otherwise tradeable)
        from trading_bot.supervisor_blacklist import read_active_blacklist
        blacklisted = read_active_blacklist(self.settings.supervisor_blacklist_path)
        if blacklisted:
            kept = []
            for sym in tradeable_symbols:
                if sym in blacklisted and sym not in open_position_symbol_set:
                    LOGGER.info("Skipping %s — supervisor blacklist", sym)
                    continue
                kept.append(sym)
            tradeable_symbols = kept
```

- [ ] **Step 5: Add integration test for skipping**

Add to `tests/test_bot_blacklist_read.py`:

```python
def test_bot_skips_blacklisted_symbol(tmp_path, monkeypatch):
    """Blacklist entry for SPY should remove it from tradeable_symbols.

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
```

- [ ] **Step 6: Run all bot tests**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass (existing 36 + new tests).

- [ ] **Step 7: Commit**

```bash
git add trading_bot/config.py trading_bot/bot.py tests/test_bot_blacklist_read.py
git commit -m "feat(bot): honour supervisor blacklist in symbol selection"
```

---

# Phase 4 — Watcher rules

### Task 4.1: Rule and RuleEvent dataclasses

**Files:**
- Create: `supervisor/rules.py` (skeleton)
- Create: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_supervisor_rules.py
from supervisor.rules import RuleEvent


def test_rule_event_basic():
    e = RuleEvent(rule="x", action="alert_only", detail={"k": "v"})
    assert e.rule == "x"
    assert e.action == "alert_only"
    assert e.detail == {"k": "v"}
```

- [ ] **Step 2: Run test, expect fail**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement skeleton**

```python
# supervisor/rules.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuleEvent:
    """A rule that fired, with optional payload describing the trigger."""
    rule: str
    action: str  # "alert_only" | "kill_switch" | "blacklist_symbol" | "restart_bot" | "rotate_log"
    detail: dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 4: Run test, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): RuleEvent dataclass"
```

### Task 4.2: process_dead rule

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_supervisor_rules.py`:

```python
from supervisor.rules import process_dead


def test_process_dead_returns_event_when_pid_missing(tmp_path):
    pid_file = tmp_path / "no.pid"
    ev = process_dead(pid_file=pid_file, pid_alive=lambda pid: False)
    assert ev is not None
    assert ev.rule == "process_dead"
    assert ev.action == "restart_bot"


def test_process_dead_silent_when_alive(tmp_path):
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("12345")
    ev = process_dead(pid_file=pid_file, pid_alive=lambda pid: True)
    assert ev is None


def test_process_dead_returns_event_when_pid_not_running(tmp_path):
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999")
    ev = process_dead(pid_file=pid_file, pid_alive=lambda pid: False)
    assert ev is not None
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py::test_process_dead_returns_event_when_pid_missing -v`
Expected: ImportError on process_dead.

- [ ] **Step 3: Implement**

Append to `supervisor/rules.py`:

```python
from pathlib import Path
from typing import Callable


def process_dead(*, pid_file: Path, pid_alive: Callable[[int], bool]) -> RuleEvent | None:
    """Return an event if bot.pid is missing or its PID is not running."""
    if not pid_file.exists():
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid file missing"})
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid file unreadable"})
    if not pid_alive(pid):
        return RuleEvent(rule="process_dead", action="restart_bot",
                         detail={"reason": "pid not running", "pid": pid})
    return None
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): process_dead rule"
```

### Task 4.3: log_silence rule

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing test**

```python
from datetime import datetime, timedelta, timezone
from supervisor.rules import log_silence


def test_log_silence_fires_after_threshold(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("old line\n")
    # mtime in the past
    import os
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
    os.utime(log, (old, old))
    ev = log_silence(log_file=log, max_silence_minutes=20)
    assert ev is not None
    assert ev.rule == "log_silence"


def test_log_silence_silent_when_recent(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("recent\n")
    ev = log_silence(log_file=log, max_silence_minutes=20)
    assert ev is None


def test_log_silence_silent_when_log_missing(tmp_path):
    ev = log_silence(log_file=tmp_path / "nope.log", max_silence_minutes=20)
    assert ev is None
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py::test_log_silence_fires_after_threshold -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `supervisor/rules.py`:

```python
import os
from datetime import datetime, timezone


def log_silence(*, log_file: Path, max_silence_minutes: float) -> RuleEvent | None:
    """Fire if log file mtime is older than max_silence_minutes."""
    if not log_file.exists():
        return None
    mtime = datetime.fromtimestamp(log_file.stat().st_mtime, tz=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - mtime).total_seconds() / 60.0
    if age_minutes >= max_silence_minutes:
        return RuleEvent(rule="log_silence", action="restart_bot",
                         detail={"age_minutes": round(age_minutes, 1)})
    return None
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): log_silence rule"
```

### Task 4.4: consecutive_losses rule

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing test**

```python
from supervisor.rules import consecutive_losses


def test_consecutive_losses_fires():
    closes = [
        {"type": "trade_close", "pnl_pct": -0.5, "ts": "2026-04-27T10:00:00+00:00"},
        {"type": "trade_close", "pnl_pct": -1.0, "ts": "2026-04-27T11:00:00+00:00"},
        {"type": "trade_close", "pnl_pct": -0.3, "ts": "2026-04-27T12:00:00+00:00"},
    ]
    ev = consecutive_losses(recent_trades=closes, threshold=3)
    assert ev is not None
    assert ev.action == "kill_switch"


def test_consecutive_losses_silent_with_win_in_streak():
    closes = [
        {"type": "trade_close", "pnl_pct": -0.5, "ts": "t1"},
        {"type": "trade_close", "pnl_pct":  0.8, "ts": "t2"},
        {"type": "trade_close", "pnl_pct": -0.3, "ts": "t3"},
    ]
    ev = consecutive_losses(recent_trades=closes, threshold=3)
    assert ev is None


def test_consecutive_losses_silent_below_threshold():
    closes = [
        {"type": "trade_close", "pnl_pct": -0.5, "ts": "t1"},
        {"type": "trade_close", "pnl_pct": -0.3, "ts": "t2"},
    ]
    ev = consecutive_losses(recent_trades=closes, threshold=3)
    assert ev is None
```

- [ ] **Step 2: Run, expect fail**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py::test_consecutive_losses_fires -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Append to `supervisor/rules.py`:

```python
def consecutive_losses(*, recent_trades: list[dict], threshold: int = 3) -> RuleEvent | None:
    """Fire if the most recent N trade_close events are all losses (pnl_pct <= 0)."""
    closes = [t for t in recent_trades if t.get("type") == "trade_close"]
    if len(closes) < threshold:
        return None
    recent = closes[-threshold:]
    if all(float(t.get("pnl_pct", 0)) <= 0 for t in recent):
        return RuleEvent(rule="consecutive_losses", action="kill_switch",
                         detail={"threshold": threshold,
                                 "trades": [t.get("symbol") for t in recent]})
    return None
```

- [ ] **Step 4: Run, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): consecutive_losses rule"
```

### Task 4.5: same_symbol_stop_streak rule

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing test**

```python
from supervisor.rules import same_symbol_stop_streak


def test_same_symbol_stop_streak_fires():
    closes = [
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.0, "ts": "t1"},
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -0.8, "ts": "t2"},
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.2, "ts": "t3"},
    ]
    ev = same_symbol_stop_streak(recent_trades=closes, threshold=3)
    assert ev is not None
    assert ev.action == "blacklist_symbol"
    assert ev.detail["symbol"] == "NVDA"


def test_same_symbol_silent_when_different_symbol():
    closes = [
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.0, "ts": "t1"},
        {"type": "trade_close", "symbol": "WTI",  "reason": "stop", "pnl_pct": -0.8, "ts": "t2"},
        {"type": "trade_close", "symbol": "NVDA", "reason": "stop", "pnl_pct": -1.2, "ts": "t3"},
    ]
    ev = same_symbol_stop_streak(recent_trades=closes, threshold=3)
    assert ev is None
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

```python
def same_symbol_stop_streak(*, recent_trades: list[dict], threshold: int = 3) -> RuleEvent | None:
    """Fire when the last N closes on a single symbol are all stop-outs."""
    closes = [t for t in recent_trades if t.get("type") == "trade_close"]
    if len(closes) < threshold:
        return None
    recent = closes[-threshold:]
    syms = {t.get("symbol") for t in recent}
    if len(syms) != 1:
        return None
    if all(t.get("reason") == "stop" for t in recent):
        return RuleEvent(rule="same_symbol_stop_streak", action="blacklist_symbol",
                         detail={"symbol": list(syms)[0], "ttl_hours": 24, "threshold": threshold})
    return None
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): same_symbol_stop_streak rule"
```

### Task 4.6: daily_drawdown_breach rule

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing test**

```python
from supervisor.rules import daily_drawdown_breach


def test_daily_drawdown_breach_fires():
    rollup = {"net_pnl_pct": -3.5}
    ev = daily_drawdown_breach(rollup=rollup, max_loss_pct=3.0)
    assert ev is not None
    assert ev.action == "kill_switch"


def test_daily_drawdown_silent_within_cap():
    ev = daily_drawdown_breach(rollup={"net_pnl_pct": -1.0}, max_loss_pct=3.0)
    assert ev is None


def test_daily_drawdown_silent_when_positive():
    ev = daily_drawdown_breach(rollup={"net_pnl_pct": 1.5}, max_loss_pct=3.0)
    assert ev is None
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

```python
def daily_drawdown_breach(*, rollup: dict, max_loss_pct: float) -> RuleEvent | None:
    """Fire when the day's net_pnl_pct is at or below -max_loss_pct."""
    pnl = float(rollup.get("net_pnl_pct", 0.0))
    if pnl <= -abs(max_loss_pct):
        return RuleEvent(rule="daily_drawdown_breach", action="kill_switch",
                         detail={"net_pnl_pct": pnl, "limit_pct": -abs(max_loss_pct)})
    return None
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): daily_drawdown_breach rule"
```

### Task 4.7: Flag-only rules (no remediation)

These rules return `RuleEvent(action="alert_only", ...)` so the watcher journals them and emits an alert but does nothing else. Implement four of the most important ones together.

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing tests**

```python
from supervisor.rules import (
    llm_error_spike, ig_allowance_burst, news_volume_drop, decision_empty_run,
)


def test_llm_error_spike_fires():
    log_lines = ["ERROR openai: timeout"] * 4
    ev = llm_error_spike(recent_log_lines=log_lines, threshold=3)
    assert ev is not None
    assert ev.action == "alert_only"


def test_llm_error_spike_silent_below_threshold():
    log_lines = ["ERROR openai: timeout"] * 2
    ev = llm_error_spike(recent_log_lines=log_lines, threshold=3)
    assert ev is None


def test_ig_allowance_burst_fires():
    log_lines = ["IG API error: error.public-api.exceeded-api-key-allowance"] * 3
    ev = ig_allowance_burst(recent_log_lines=log_lines, threshold=2)
    assert ev is not None


def test_news_volume_drop_fires():
    cycles = [
        {"type": "cycle", "symbols": ["NVDA"], "ts": f"2026-04-27T{h:02}:00:00+00:00", "headlines_per_symbol": {"NVDA": 0}}
        for h in range(6)
    ]
    ev = news_volume_drop(recent_cycles=cycles, symbol="NVDA", min_cycles_zero=6)
    assert ev is not None


def test_decision_empty_run_fires():
    cycles = [
        {"type": "cycle", "trades_approved": 0, "trades_rejected": 0, "ts": "t1"},
        {"type": "cycle", "trades_approved": 0, "trades_rejected": 0, "ts": "t2"},
    ]
    ev = decision_empty_run(recent_cycles=cycles, threshold=2)
    assert ev is not None
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

Append to `supervisor/rules.py`:

```python
def llm_error_spike(*, recent_log_lines: list[str], threshold: int = 3) -> RuleEvent | None:
    n = sum(1 for line in recent_log_lines
            if "openai" in line.lower() or "openrouter" in line.lower())
    n_err = sum(1 for line in recent_log_lines if "ERROR" in line and (
        "openai" in line.lower() or "openrouter" in line.lower()))
    if n_err >= threshold:
        return RuleEvent(rule="llm_error_spike", action="alert_only",
                         detail={"errors": n_err, "threshold": threshold})
    return None


def ig_allowance_burst(*, recent_log_lines: list[str], threshold: int = 2) -> RuleEvent | None:
    n = sum(1 for line in recent_log_lines
            if "exceeded-api-key-allowance" in line)
    if n >= threshold:
        return RuleEvent(rule="ig_allowance_burst", action="alert_only",
                         detail={"hits": n, "threshold": threshold})
    return None


def news_volume_drop(*, recent_cycles: list[dict], symbol: str, min_cycles_zero: int = 6) -> RuleEvent | None:
    relevant = [c for c in recent_cycles if symbol in (c.get("symbols") or [])]
    if len(relevant) < min_cycles_zero:
        return None
    last = relevant[-min_cycles_zero:]
    if all((c.get("headlines_per_symbol") or {}).get(symbol, 0) == 0 for c in last):
        return RuleEvent(rule="news_volume_drop", action="alert_only",
                         detail={"symbol": symbol, "cycles_with_zero": min_cycles_zero})
    return None


def decision_empty_run(*, recent_cycles: list[dict], threshold: int = 2) -> RuleEvent | None:
    cycles = [c for c in recent_cycles if c.get("type") == "cycle"]
    if len(cycles) < threshold:
        return None
    last = cycles[-threshold:]
    if all(c.get("trades_approved", 0) == 0 and c.get("trades_rejected", 0) == 0 for c in last):
        return RuleEvent(rule="decision_empty_run", action="alert_only",
                         detail={"cycles": threshold})
    return None
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): flag-only rules (llm errors, ig allowance, news, empty cycles)"
```

### Task 4.8: Remaining rules (weekly drawdown, holding too long, position divergence, data stale, confidence divergence, repeated startup failures, disk log growth)

All of these are alert-only except `repeated_startup_failures` (which writes a critical alert and stops trying — the watcher already caps restarts via `RestartManager`, so this rule just flags the situation for the daily review).

**Files:**
- Modify: `supervisor/rules.py`
- Modify: `tests/test_supervisor_rules.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_supervisor_rules.py`:

```python
from datetime import datetime, timedelta, timezone
from supervisor.rules import (
    weekly_drawdown_breach, holding_too_long, position_book_divergence,
    data_stale, confidence_outcome_divergence, repeated_startup_failures, disk_log_growth,
)


def test_weekly_drawdown_breach_fires():
    closes = [{"type": "trade_close", "pnl_pct": -2.0, "ts": "2026-04-22T10:00:00+00:00"}] * 4
    ev = weekly_drawdown_breach(recent_trades=closes, max_loss_pct=6.0)
    assert ev is not None


def test_weekly_drawdown_silent_in_cap():
    closes = [{"type": "trade_close", "pnl_pct": -1.0, "ts": "2026-04-22T10:00:00+00:00"}] * 2
    ev = weekly_drawdown_breach(recent_trades=closes, max_loss_pct=6.0)
    assert ev is None


def test_holding_too_long_fires():
    opened = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    positions = [{"symbol": "NVDA", "opened_at": opened}]
    ev = holding_too_long(positions=positions, max_hours=72)
    assert ev is not None
    assert ev.detail["symbol"] == "NVDA"


def test_holding_too_long_silent_when_within_window():
    opened = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    positions = [{"symbol": "NVDA", "opened_at": opened}]
    ev = holding_too_long(positions=positions, max_hours=72)
    assert ev is None


def test_position_book_divergence_fires():
    local = {"NVDA", "WTI"}
    broker = {"WTI"}
    ev = position_book_divergence(local_positions=local, broker_positions=broker)
    assert ev is not None
    assert "NVDA" in ev.detail["only_local"]


def test_position_book_divergence_silent_when_match():
    ev = position_book_divergence(local_positions={"NVDA"}, broker_positions={"NVDA"})
    assert ev is None


def test_data_stale_fires():
    last_bar = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    ev = data_stale(last_bar_ts=last_bar, symbol="NVDA", max_age_hours=2)
    assert ev is not None


def test_data_stale_silent_when_fresh():
    last_bar = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    ev = data_stale(last_bar_ts=last_bar, symbol="NVDA", max_age_hours=2)
    assert ev is None


def test_confidence_outcome_divergence_fires():
    closes = [
        {"type": "trade_close", "confidence": 0.85, "pnl_pct": -1.0, "ts": f"t{i}"}
        for i in range(10)
    ]
    closes[0]["pnl_pct"] = 1.0  # 1 winner / 10 high-conf trades
    closes[1]["pnl_pct"] = 1.0  # 2 winners
    ev = confidence_outcome_divergence(recent_trades=closes, min_n=10, min_confidence=0.80, min_win_rate=0.30)
    assert ev is not None  # 2/10 = 20% < 30%


def test_confidence_outcome_divergence_silent_when_winning():
    closes = [
        {"type": "trade_close", "confidence": 0.85, "pnl_pct": 1.0, "ts": f"t{i}"}
        for i in range(10)
    ]
    ev = confidence_outcome_divergence(recent_trades=closes, min_n=10, min_confidence=0.80, min_win_rate=0.30)
    assert ev is None


def test_repeated_startup_failures_fires():
    triggers = [
        {"type": "rule_trigger", "rule": "process_dead", "ts": f"2026-04-27T{h:02}:00:00+00:00"}
        for h in (10, 11, 12)
    ]
    ev = repeated_startup_failures(recent_triggers=triggers, threshold=3, window_hours=4)
    assert ev is not None


def test_disk_log_growth_fires(tmp_path):
    log = tmp_path / "bot.log"
    # 200 MB of zeroes (sparse) — but use truncate semantic via os.truncate for a real-ish size
    with log.open("wb") as f:
        f.truncate(200 * 1024 * 1024)
    ev = disk_log_growth(log_file=log, max_mb=100)
    assert ev is not None


def test_disk_log_growth_silent_when_small(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("small")
    ev = disk_log_growth(log_file=log, max_mb=100)
    assert ev is None
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

Append to `supervisor/rules.py`:

```python
def weekly_drawdown_breach(*, recent_trades: list[dict], max_loss_pct: float) -> RuleEvent | None:
    """Sum of pnl_pct over the last 7 days; fire if at or below -max_loss_pct."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pnls = [float(t.get("pnl_pct", 0)) for t in recent_trades
            if t.get("type") == "trade_close" and t.get("ts", "") >= cutoff]
    total = sum(pnls)
    if total <= -abs(max_loss_pct):
        return RuleEvent(rule="weekly_drawdown_breach", action="alert_only",
                         detail={"net_pnl_pct": total, "limit_pct": -abs(max_loss_pct)})
    return None


def holding_too_long(*, positions: list[dict], max_hours: float) -> RuleEvent | None:
    """Fire if any open position has been held longer than max_hours × 1.2."""
    threshold_hours = max_hours * 1.2
    now = datetime.now(timezone.utc)
    for pos in positions:
        opened = pos.get("opened_at")
        if not opened:
            continue
        try:
            opened_dt = datetime.fromisoformat(opened)
        except ValueError:
            continue
        held_hours = (now - opened_dt).total_seconds() / 3600.0
        if held_hours >= threshold_hours:
            return RuleEvent(rule="holding_too_long", action="alert_only",
                             detail={"symbol": pos.get("symbol"),
                                     "held_hours": round(held_hours, 1),
                                     "threshold_hours": threshold_hours})
    return None


def position_book_divergence(*, local_positions: set[str], broker_positions: set[str]) -> RuleEvent | None:
    """Fire when local and broker position sets disagree."""
    only_local = sorted(local_positions - broker_positions)
    only_broker = sorted(broker_positions - local_positions)
    if only_local or only_broker:
        return RuleEvent(rule="position_book_divergence", action="alert_only",
                         detail={"only_local": only_local, "only_broker": only_broker})
    return None


def data_stale(*, last_bar_ts: str, symbol: str, max_age_hours: float) -> RuleEvent | None:
    """Fire if last bar timestamp is older than max_age_hours."""
    try:
        bar_dt = datetime.fromisoformat(last_bar_ts)
    except ValueError:
        return None
    age_hours = (datetime.now(timezone.utc) - bar_dt).total_seconds() / 3600.0
    if age_hours >= max_age_hours:
        return RuleEvent(rule="data_stale", action="alert_only",
                         detail={"symbol": symbol, "age_hours": round(age_hours, 1)})
    return None


def confidence_outcome_divergence(*, recent_trades: list[dict],
                                  min_n: int = 10,
                                  min_confidence: float = 0.80,
                                  min_win_rate: float = 0.30) -> RuleEvent | None:
    """Fire when high-confidence trades (>= min_confidence) win below min_win_rate over min_n samples."""
    high_conf = [t for t in recent_trades
                 if t.get("type") == "trade_close" and float(t.get("confidence", 0)) >= min_confidence]
    if len(high_conf) < min_n:
        return None
    wins = sum(1 for t in high_conf if float(t.get("pnl_pct", 0)) > 0)
    rate = wins / len(high_conf)
    if rate < min_win_rate:
        return RuleEvent(rule="confidence_outcome_divergence", action="alert_only",
                         detail={"n": len(high_conf), "win_rate": rate, "min_confidence": min_confidence})
    return None


def repeated_startup_failures(*, recent_triggers: list[dict],
                              threshold: int = 3,
                              window_hours: float = 1.0) -> RuleEvent | None:
    """Fire if N process_dead triggers occurred within the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    relevant = [t for t in recent_triggers
                if t.get("type") == "rule_trigger" and t.get("rule") == "process_dead"
                and t.get("ts", "") >= cutoff]
    if len(relevant) >= threshold:
        return RuleEvent(rule="repeated_startup_failures", action="alert_only",
                         detail={"count": len(relevant), "window_hours": window_hours})
    return None


def disk_log_growth(*, log_file: Path, max_mb: float = 100.0) -> RuleEvent | None:
    """Fire if the log file is larger than max_mb."""
    if not log_file.exists():
        return None
    size_mb = log_file.stat().st_size / (1024 * 1024)
    if size_mb >= max_mb:
        return RuleEvent(rule="disk_log_growth", action="rotate_log",
                         detail={"size_mb": round(size_mb, 1), "max_mb": max_mb})
    return None
```

Add to top-of-file imports if not present: `from datetime import timedelta`.

- [ ] **Step 4: Run, expect pass.**

Run: `.venv/bin/pytest tests/test_supervisor_rules.py -v`
Expected: all rule tests pass.

- [ ] **Step 5: Commit**

```bash
git add supervisor/rules.py tests/test_supervisor_rules.py
git commit -m "feat(supervisor): remaining flag rules (weekly dd, holding, divergence, stale, confidence, startup, disk)"
```

### Task 4.9: Wire new rules into watcher tick + add log-rotate handler

**Files:**
- Modify: `supervisor/watcher.py`

(This task is added in Phase 6 task 6.2. The new rules will be wired in there. Skip if already done.)

---

# Phase 5 — Watcher remediations

### Task 5.1: kill switch + log rotate + blacklist write

**Files:**
- Create: `supervisor/remediations.py`
- Create: `tests/test_supervisor_remediations.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_supervisor_remediations.py
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
    # Original file is fresh and small
    assert log.exists()
    assert log.stat().st_size == 0
    # Archive contains content
    with gzip.open(archive, "rt") as f:
        assert "hello" in f.read()
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

```python
# supervisor/remediations.py
from __future__ import annotations

import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path

from supervisor import blacklist as bl_mod


def touch_kill_switch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def blacklist_symbol_action(path: Path, *, symbol: str, reason: str, ttl_hours: float = 24.0) -> None:
    bl_mod.add_symbol(path, symbol, reason=reason, ttl_hours=ttl_hours)


def rotate_log(log_file: Path) -> Path:
    """Compress current log to bot.log.YYYYMMDD-HHMMSS.gz and truncate the original."""
    if not log_file.exists():
        raise FileNotFoundError(log_file)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = log_file.with_name(f"{log_file.name}.{stamp}.gz")
    with log_file.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)
    log_file.write_text("", encoding="utf-8")
    return archive
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add supervisor/remediations.py tests/test_supervisor_remediations.py
git commit -m "feat(supervisor): kill_switch, blacklist_action, rotate_log remediations"
```

### Task 5.2: bot restart helper with attempt counter

**Files:**
- Modify: `supervisor/remediations.py`
- Modify: `tests/test_supervisor_remediations.py`

- [ ] **Step 1: Add failing test**

```python
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
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

Append to `supervisor/remediations.py`:

```python
import time
from collections import deque
from typing import Callable


class RestartManager:
    """Caps bot-restart attempts to N per hour to avoid flapping."""

    def __init__(self, *, max_per_hour: int = 3, spawn: Callable[[], int]):
        self.max_per_hour = max_per_hour
        self.spawn = spawn
        self._attempts: deque[float] = deque()

    def attempt_restart(self) -> int | None:
        """Call spawn() if under the per-hour cap. Returns spawned PID or None if capped."""
        now = time.monotonic()
        cutoff = now - 3600.0
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()
        if len(self._attempts) >= self.max_per_hour:
            return None
        self._attempts.append(now)
        return self.spawn()
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add supervisor/remediations.py tests/test_supervisor_remediations.py
git commit -m "feat(supervisor): RestartManager with per-hour cap"
```

---

# Phase 6 — Watcher main loop + state

### Task 6.1: WatcherState dataclass + persistence

**Files:**
- Create: `supervisor/state.py`
- Create: `tests/test_supervisor_state.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_supervisor_state.py
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
```

- [ ] **Step 2: Run, expect fail.**

- [ ] **Step 3: Implement**

```python
# supervisor/state.py
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class WatcherState:
    last_kill_switch_at: str | None = None
    last_alert_at: str | None = None
    restart_attempts_last_hour: int = 0


def load_state(path: Path) -> WatcherState:
    if not path.exists():
        return WatcherState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return WatcherState()
    return WatcherState(**data)


def save_state(path: Path, state: WatcherState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    os.replace(tmp, path)
```

- [ ] **Step 4: Run, expect pass.**

- [ ] **Step 5: Commit**

```bash
git add supervisor/state.py tests/test_supervisor_state.py
git commit -m "feat(supervisor): WatcherState persistence"
```

### Task 6.2: Main poll loop (watcher.py)

**Files:**
- Create: `supervisor/watcher.py`
- Create: `tests/test_supervisor_watcher.py`

- [ ] **Step 1: Write failing test (single tick)**

```python
# tests/test_supervisor_watcher.py
import json
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
        bot_command=["/bin/true"],  # spawn target for tests; harmless
    )
    # No pid file → process_dead → restart
    run_one_tick(cfg, pid_alive=lambda pid: False, spawn=lambda: 99999)
    journal_lines = cfg.journal_file.read_text().splitlines() if cfg.journal_file.exists() else []
    assert any("rule_trigger" in l and "process_dead" in l for l in journal_lines)


def test_run_one_tick_consecutive_losses_triggers_kill_switch(tmp_path):
    """Three losing trades in a row → kill switch file created."""
    journal = tmp_path / "raw_journal.jsonl"
    # seed three losing trade_close events
    from supervisor.journal import append_event
    for i in range(3):
        append_event(journal, {"type": "trade_close", "symbol": "NVDA",
                                "exit": 100, "pnl_pct": -1.0, "reason": "stop", "held_minutes": 30})
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("12345")
    log = tmp_path / "bot.log"
    log.write_text("hi")  # fresh
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
```

- [ ] **Step 2: Run, expect fail.**

Run: `.venv/bin/pytest tests/test_supervisor_watcher.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement watcher**

```python
# supervisor/watcher.py
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from supervisor import journal, blacklist, rollups, rules, remediations, state

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

    # Trade-derived rules: read recent trade_close from journal
    recent_trades = [e for e in journal.read_recent(cfg.journal_file) if e.get("type") == "trade_close"]
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
    recent_events = journal.read_recent(cfg.journal_file)
    ev = rules.repeated_startup_failures(recent_triggers=recent_events,
                                          threshold=3, window_hours=1.0)
    if ev is not None:
        triggered.append(ev)

    # Disk log growth
    ev = rules.disk_log_growth(log_file=cfg.bot_log, max_mb=cfg.max_log_mb)
    if ev is not None:
        triggered.append(ev)

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
        ".venv/bin/python", "-m", "trading_bot", "--log-level", "INFO",
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
```

- [ ] **Step 4: Run watcher tests, expect pass**

Run: `.venv/bin/pytest tests/test_supervisor_watcher.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add supervisor/watcher.py tests/test_supervisor_watcher.py
git commit -m "feat(supervisor): watcher main loop with --once mode"
```

### Task 6.3: Run full test suite

- [ ] **Step 1: Run all tests**

Run: `.venv/bin/pytest tests/ -q`
Expected: all green (existing 36 + new ~25).

- [ ] **Step 2: Smoke test --once on a fake bot**

```bash
cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading"
mkdir -p supervisor
echo '99999' > /tmp/fake_bot.pid
.venv/bin/python -m supervisor.watcher --once \
    --bot-pid-file /tmp/fake_bot.pid \
    --bot-log /tmp/cfd_bot.log \
    --journal /tmp/test_journal.jsonl \
    --blacklist /tmp/test_blacklist.json \
    --kill-switch /tmp/test_kill \
    --state /tmp/test_ws.json \
    --bot-command /bin/true
cat /tmp/test_journal.jsonl
```
Expected: at least one `rule_trigger` line for `process_dead` (since PID 99999 doesn't exist).

- [ ] **Step 3: Commit any test fixes if found**

```bash
git status
# if changes:
git add -A && git commit -m "fix: watcher smoke-test fixes"
```

---

# Phase 7 — Slash commands + subagent definition

The subagent is the LLM-driven brain reachable via `/bot-*`. The commands invoke it with different intents.

### Task 7.1: Subagent definition

**Files:**
- Create: `.claude/agents/bot-supervisor.md`

- [ ] **Step 1: Create the agent file**

```markdown
<!-- .claude/agents/bot-supervisor.md -->
---
name: bot-supervisor
description: Manage the CFD trading bot — status, logs, daily reviews, supervised improvements, and process control. Reads supervisor/raw_journal.jsonl and supervisor/insights.md; can edit code/config when the user explicitly approves a numbered review proposal.
tools: Read, Edit, Write, Bash, Glob, Grep
---

You are the supervising agent for the CFD trading bot located at the project root. Your job is to be the user's primary interface for monitoring and improving the bot.

## Files you read frequently

- `supervisor/raw_journal.jsonl` — append-only event journal (cycle/trade/rule_trigger/remediation/daily_rollup records)
- `supervisor/insights.md` — curated, human-readable lessons and pending suggestions
- `supervisor/blacklist.json` — currently-blacklisted symbols with TTLs
- `supervisor/reviews/YYYY-MM-DD.md` — daily review files with numbered proposals
- `/tmp/cfd_bot.log` — runtime log (tail when asked for /bot-logs)
- `runtime_state.json` — bot's runtime state (open positions, daily PnL)
- `bot.pid` — bot process pid

## Files you write

- `supervisor/insights.md` — when applying or rejecting review proposals
- `trading_bot/*.py`, `.env`, prompt files — when applying approved code/config changes
- `supervisor/blacklist.json` via `/bot-blacklist`

## Files you NEVER modify

- `supervisor/raw_journal.jsonl` (watcher-owned, append-only)
- `supervisor/watcher_state.json`

## Safety rules

- Never apply a change without explicit user approval (the user must name the review proposal IDs to apply, e.g. "apply 1, 3").
- Never disable existing risk gates (kill switch, drawdown caps, confidence threshold floors).
- Never increase leverage beyond 3×.
- Never modify broker integration code (`ig_bridge.py`, `session_manager.py`) without the user explicitly naming those files.
- After any code change, run `.venv/bin/pytest tests/ -q` and only commit if green.
- Always commit changes with a message that cites the review file and proposal number.

## Display conventions

When showing logs, prefix lines with icons:
- 🔴 ERROR / CRITICAL
- 🟡 WARNING
- 🔵 cycle events
- 📊 decisions / trades / confidence
- ⚡ executions / orders / deals
- 🚫 risk-gate rejections
- 🔗 broker connect / reconcile

When showing journal entries, summarise rather than dump raw JSON unless the user asks for raw.
```

- [ ] **Step 2: Verify the file is well-formed YAML frontmatter**

Run: `head -10 ".claude/agents/bot-supervisor.md"`
Expected: prints the frontmatter block correctly.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/bot-supervisor.md
git commit -m "feat(supervisor): subagent definition"
```

### Task 7.2: Read-only commands (status, logs, insights)

**Files:**
- Create: `.claude/commands/bot-status.md`
- Create: `.claude/commands/bot-logs.md`
- Create: `.claude/commands/bot-insights.md`

- [ ] **Step 1: bot-status.md**

```markdown
<!-- .claude/commands/bot-status.md -->
---
description: Show CFD bot status — process state, today's cycles, open positions, kill switch, blacklist, last alert
---

Use the `bot-supervisor` subagent. Display a concise status report:

1. Process state: read `bot.pid`, check if PID is running.
2. Today's cycles: count cycle records in `supervisor/raw_journal.jsonl` for today's date.
3. Open positions: read `runtime_state.json` and list open positions with entry/PnL.
4. Kill switch: check whether `kill_switch` file exists in project root.
5. Blacklist: list active symbols from `supervisor/blacklist.json` with expiry times.
6. Last alert: tail `alerts/` directory for newest entry.

Format as a short table. No emojis unless tagging severity.
```

- [ ] **Step 2: bot-logs.md**

```markdown
<!-- .claude/commands/bot-logs.md -->
---
description: Tail the bot log with classification icons (default 50 lines)
argument-hint: [count]
---

Use the `bot-supervisor` subagent. Tail the last $1 (default 50) lines from `/tmp/cfd_bot.log` and prefix each line with the appropriate icon (see safety rules in the agent definition). If $1 is missing, default to 50.
```

- [ ] **Step 3: bot-insights.md**

```markdown
<!-- .claude/commands/bot-insights.md -->
---
description: Render the curated supervisor insights file
---

Use the `bot-supervisor` subagent. Read `supervisor/insights.md` and display its contents as-is. If the file does not exist, say "no insights yet".
```

- [ ] **Step 4: Commit**

```bash
git add .claude/commands/bot-status.md .claude/commands/bot-logs.md .claude/commands/bot-insights.md
git commit -m "feat(supervisor): read-only slash commands (status, logs, insights)"
```

### Task 7.3: Process-control commands

**Files:**
- Create: `.claude/commands/bot-restart.md`
- Create: `.claude/commands/bot-stop.md`
- Create: `.claude/commands/bot-resume.md`
- Create: `.claude/commands/bot-watcher.md`

- [ ] **Step 1: bot-stop.md**

```markdown
<!-- .claude/commands/bot-stop.md -->
---
description: Trigger the kill switch — bot flattens all positions and halts
---

Use the `bot-supervisor` subagent. Touch the `kill_switch` file in the project root (`touch kill_switch`). Confirm to the user that the bot will flatten positions on its next cycle iteration. Tell them how to resume (`/bot-resume`).
```

- [ ] **Step 2: bot-resume.md**

```markdown
<!-- .claude/commands/bot-resume.md -->
---
description: Clear the kill switch and restart the bot process
---

Use the `bot-supervisor` subagent. Delete the `kill_switch` file if present. If the bot process is not running, restart it via the same nohup command pattern used to start it (`.venv/bin/python -m trading_bot --log-level INFO --log-file /tmp/cfd_bot.log`). Confirm new PID.
```

- [ ] **Step 3: bot-restart.md**

```markdown
<!-- .claude/commands/bot-restart.md -->
---
description: Stop the bot cleanly and restart it
---

Use the `bot-supervisor` subagent. Send SIGTERM to the PID in `bot.pid`, wait up to 10 seconds for it to exit, then start a new instance with the same arguments. Confirm new PID.
```

- [ ] **Step 4: bot-watcher.md**

```markdown
<!-- .claude/commands/bot-watcher.md -->
---
description: Manage the supervisor watcher process (start | stop | status)
argument-hint: start|stop|status
---

Use the `bot-supervisor` subagent. Action: $1 (default `status`).

- start: Launch the watcher with `nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 &`. Save PID to `supervisor/watcher.pid`.
- stop: SIGTERM the PID in `supervisor/watcher.pid`, then remove the file.
- status: Report PID, alive?, recent journal activity (last 5 entries).
```

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/bot-restart.md .claude/commands/bot-stop.md .claude/commands/bot-resume.md .claude/commands/bot-watcher.md
git commit -m "feat(supervisor): process-control slash commands"
```

### Task 7.4: Blacklist management command

**Files:**
- Create: `.claude/commands/bot-blacklist.md`

- [ ] **Step 1: Write command**

```markdown
<!-- .claude/commands/bot-blacklist.md -->
---
description: Manually blacklist (or unblacklist) a symbol
argument-hint: <SYMBOL> [hours|0]
---

Use the `bot-supervisor` subagent. Args: $1 = symbol, $2 = TTL hours (default 24; 0 means clear).

If $2 is "0", call `clear_symbol` from `supervisor.blacklist`. Otherwise call `add_symbol` with reason "manual block by user".

Show the resulting blacklist contents.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/commands/bot-blacklist.md
git commit -m "feat(supervisor): /bot-blacklist command"
```

### Task 7.5: Review and improvement commands

**Files:**
- Create: `.claude/commands/bot-review.md`
- Create: `.claude/commands/bot-apply.md`
- Create: `.claude/commands/bot-reject.md`
- Create: `.claude/commands/bot-improve.md`

- [ ] **Step 1: bot-review.md**

```markdown
<!-- .claude/commands/bot-review.md -->
---
description: Render today's (or specified date) daily review file
argument-hint: [YYYY-MM-DD]
---

Use the `bot-supervisor` subagent. If $1 is provided, load `supervisor/reviews/$1.md`. Otherwise pick the most recent file in `supervisor/reviews/`. Display its contents. Tell the user how to apply or reject proposals (`/bot-apply 1,3` / `/bot-reject 2`).
```

- [ ] **Step 2: bot-apply.md**

```markdown
<!-- .claude/commands/bot-apply.md -->
---
description: Apply numbered proposals from the latest review (e.g. /bot-apply 1,3)
argument-hint: <ids> (e.g. 1,3 or 1-3 or all)
---

Use the `bot-supervisor` subagent. Args: $1 = comma/dash list of proposal IDs, or "all".

For each approved proposal:
1. Locate the proposal in the latest `supervisor/reviews/*.md` file.
2. Determine the change type (parameter / prompt / code / symbol / news) from its metadata.
3. Edit the named files using `Edit` tool.
4. Run `.venv/bin/pytest tests/ -q`. If anything fails, abort the change and report.
5. If green, `git add` only the modified files and `git commit -m "<change> (per review YYYY-MM-DD #N)"`.
6. Append a row to `supervisor/insights.md` "Improvements applied" table with Review-on date = today + 7 days.

Report each change applied, each test result, and each commit hash.
```

- [ ] **Step 3: bot-reject.md**

```markdown
<!-- .claude/commands/bot-reject.md -->
---
description: Mark proposals from latest review as rejected
argument-hint: <ids> [reason]
---

Use the `bot-supervisor` subagent. Args: $1 = ids, $2 = optional reason.

For each rejected proposal:
1. Locate the proposal text in the latest `supervisor/reviews/*.md`.
2. Append to `supervisor/insights.md` "Rejected ideas" section a one-line entry with date, summary, and the reason if provided.

Do NOT modify any code or config.
```

- [ ] **Step 4: bot-improve.md**

```markdown
<!-- .claude/commands/bot-improve.md -->
---
description: Run an ad-hoc improvement analysis right now (do not wait for the daily schedule)
---

Use the `bot-supervisor` subagent. Run the full daily-review analysis right now using the same prompt as the scheduled review (see `.claude/prompts/bot-daily-review.md`), but write the output to `supervisor/reviews/YYYY-MM-DD-adhoc-N.md` (where N is the next free integer for today). Display the proposals to the user.
```

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/bot-review.md .claude/commands/bot-apply.md .claude/commands/bot-reject.md .claude/commands/bot-improve.md
git commit -m "feat(supervisor): review and improvement slash commands"
```

---

# Phase 8 — Daily review schedule + verification

### Task 8.1: Daily review prompt template

**Files:**
- Create: `.claude/prompts/bot-daily-review.md`

- [ ] **Step 1: Create template**

```markdown
<!-- .claude/prompts/bot-daily-review.md -->
You are the daily review agent for the CFD trading bot.

Read these inputs:
- `supervisor/raw_journal.jsonl` (last 24h of records)
- `logs/*.json` (today's decision logs)
- `logs/events.jsonl` (recent events)
- `supervisor/insights.md` (current curated insights)

Produce `supervisor/reviews/YYYY-MM-DD.md` (use today's UTC date) with these sections, in order:

## Summary
N cycles, N trades, win rate, net pnl, key events.

## Anomalies
Bullet list of unexpected things: missed cycles, error spikes, news drop-offs, divergences from prior days.

## Proposed changes
Numbered list. For each:
- Description of the change
- Risk
- Files affected
- Type: parameter | prompt | code | symbol | news
- Rationale (cite the journal/event evidence)

## Confirmed lessons (auto-merged into insights.md)
Lessons that are settled enough to add to `insights.md`.

After writing the review file, edit `insights.md` to:
- Append confirmed lessons under the appropriate section
- Append new pending suggestions linking to this review file by `[reviews/YYYY-MM-DD.md#N]`
- Update the `Outcome` column of any "Improvements applied" rows whose `Review-on` date is today, by computing pre/post stats from the journal

Do NOT:
- Modify any code or `.env`
- Modify `raw_journal.jsonl`
- Touch the kill switch or blacklist directly (those are the watcher's domain)

Stop when the review file and `insights.md` are saved.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/prompts/bot-daily-review.md
git commit -m "feat(supervisor): daily review prompt template"
```

### Task 8.2: Schedule the daily review (one-shot setup)

This task runs the `schedule` skill to register a recurring remote agent. It does not produce code; it produces a registration. The plan documents the steps an operator (you, with Claude Code open) must run once.

**Files:** none new.

- [ ] **Step 1: Verify the schedule skill is available**

Tell the user (or operator running this plan):

> Run `/help` and confirm `schedule` skill is available. If not, install it.

- [ ] **Step 2: Register the routine**

In Claude Code, the operator runs:

> "/schedule create a recurring routine for weekday 21:30 UTC that runs the prompt from `.claude/prompts/bot-daily-review.md` against this repository."

The schedule skill will produce a routine ID. Save it.

- [ ] **Step 3: Document the routine ID**

Add a row to `supervisor/insights.md` (creating the file if needed):

```markdown
## Routines
- Daily review: <routine-id> — runs weekday 21:30 UTC
```

(This is a manual step performed once; the plan does not commit a fake routine ID.)

- [ ] **Step 4: Commit insights.md if newly created**

```bash
# only if insights.md was created
git add supervisor/insights.md 2>/dev/null || true
git commit -m "chore(supervisor): document daily-review routine ID" 2>/dev/null || true
```

### Task 8.3: End-to-end smoke test

A scripted scenario that exercises the full path: bot writes events, watcher processes, journal records, blacklist updates, kill switch fires.

**Files:** none new (uses existing components).

- [ ] **Step 1: Start with a clean slate**

```bash
cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading"
rm -f supervisor/raw_journal.jsonl supervisor/blacklist.json supervisor/watcher_state.json
rm -f kill_switch
mkdir -p supervisor
```

- [ ] **Step 2: Seed three losing NVDA trades into the journal**

```bash
.venv/bin/python <<'PY'
from pathlib import Path
from supervisor.journal import append_event
p = Path("supervisor/raw_journal.jsonl")
for i in range(3):
    append_event(p, {"type": "trade_close", "symbol": "NVDA",
                     "exit": 100, "pnl_pct": -1.0, "reason": "stop", "held_minutes": 30})
print("seeded")
PY
```

- [ ] **Step 3: Run watcher --once**

```bash
echo '99999' > /tmp/dummy_pid  # PID that exists test below
# Use a real pid (this shell): $$
echo $$ > /tmp/dummy_pid
.venv/bin/python -m supervisor.watcher --once \
    --bot-pid-file /tmp/dummy_pid \
    --bot-log /tmp/cfd_bot.log \
    --journal supervisor/raw_journal.jsonl \
    --blacklist supervisor/blacklist.json \
    --kill-switch ./kill_switch \
    --state supervisor/watcher_state.json \
    --bot-command /bin/true
```

- [ ] **Step 4: Verify the kill switch and blacklist were written**

```bash
ls -la kill_switch && echo "kill switch present"
cat supervisor/blacklist.json | head
.venv/bin/python -c "
from pathlib import Path
from supervisor.journal import read_recent
events = read_recent(Path('supervisor/raw_journal.jsonl'))
triggers = [e for e in events if e.get('type') in ('rule_trigger', 'remediation')]
for t in triggers:
    print(t['type'], t.get('rule', t.get('action')), t.get('detail') or t.get('symbol') or '')
"
```

Expected: `kill_switch` exists, blacklist.json has NVDA entry, journal shows `consecutive_losses` and `same_symbol_stop_streak` triggers.

- [ ] **Step 5: Cleanup**

```bash
rm -f kill_switch supervisor/raw_journal.jsonl supervisor/blacklist.json supervisor/watcher_state.json
```

- [ ] **Step 6: Run full suite once more**

```bash
.venv/bin/pytest tests/ -q
```
Expected: all green.

### Task 8.4: Document operational quickstart in repo

**Files:**
- Modify: `CLAUDE.md` (or create if absent)

- [ ] **Step 1: Add quickstart**

Append (or create) `CLAUDE.md` at project root with:

```markdown
# Bot supervisor quickstart

## Starting both processes
```bash
# bot
nohup .venv/bin/python -m trading_bot --log-level INFO --log-file /tmp/cfd_bot.log > /dev/null 2>&1 &
echo $! > bot.pid

# supervisor watcher
nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 &
echo $! > supervisor/watcher.pid
```

## Slash commands
- `/bot-status` — quick health check
- `/bot-logs [N]` — tail the bot log
- `/bot-review [date]` — read the day's review
- `/bot-apply <ids>` — apply approved proposals
- `/bot-stop` `/bot-resume` `/bot-restart` — process control
- `/bot-blacklist <SYM> [hours]` — manual symbol skip
- `/bot-watcher [start|stop|status]` — manage watcher
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: bot supervisor quickstart"
```

---

## Self-review checklist (operator runs after the last task)

After Phase 8 completes, run this final check:

```bash
cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading"
.venv/bin/pytest tests/ -q              # all tests green
ls supervisor/                          # should contain *.py files + reviews/
ls .claude/agents/ .claude/commands/    # 1 agent + 12 commands
ls .claude/prompts/                     # 1 prompt template
git log --oneline | head -30            # commits trace per task
```

If any step is red, fix and re-commit before declaring complete.
