# Bot Supervisor — Design Spec

**Created:** 2026-04-27
**Status:** Approved for plan
**Goal:** Add a supervisor layer that monitors the CFD trading bot continuously, runs mechanical safety remediations autonomously, and is the primary user interface for managing and improving the bot — with a daily LLM-driven review producing approval-gated improvement proposals.

---

## 1. Architecture

Three independent components with clear blast radii (one always-on process, two transient agents):

| Component | Type | LLM | Can do | Cannot do |
|-----------|------|-----|--------|-----------|
| **Watcher** | Standalone Python, always-on | No | Trigger kill switch, restart bot, blacklist symbols, rotate logs, write raw journal, emit alerts | Edit code, change config, place orders, edit insights memory |
| **Daily review agent** | Scheduled remote agent (Claude Code `schedule` skill) | Yes | Read all bot/supervisor data, write dated review files, update insights.md confirmed lessons | Modify code, modify config, write to journal/blacklist |
| **Interactive supervisor agent** | Claude Code subagent + slash commands | Yes | Everything (Edit, Bash, Read), gated by user approval per change | Bypass user approval for destructive changes |

The watcher writes one append-only journal file. The agents write a curated insights file. Reviews are dated files in their own directory. The bot itself is unchanged except for two small touchpoints (blacklist read, already-existing kill switch).

```
bot.log + events.jsonl + runtime_state.json + decision logs (existing)
            │
            ▼
   supervisor/watcher.py  ── writes ──>  raw_journal.jsonl, blacklist.json, alerts/
            │
            └── reads / informs ──> daily review agent (21:30 UTC weekdays)
                                         │
                                         └── writes ──> reviews/YYYY-MM-DD.md, insights.md
                                                              │
                                                              └── consumed by ──>
                                                                interactive agent
                                                                  via /bot-review
                                                                  /bot-apply N,M …
```

---

## 2. File layout

```
supervisor/
  watcher.py              # entry point of the standalone watcher
  rules.py                # rule definitions (one function per rule)
  remediations.py         # mechanical actions (kill, restart, blacklist, log rotate)
  journal.py              # raw_journal.jsonl writer with type-tagged records
  blacklist.py            # blacklist.json read/write helpers
  rollups.py              # daily/weekly stat aggregators
  state.py                # watcher's own state (last process check, last alert time)

  raw_journal.jsonl       # autonomous, append-only
  insights.md             # LLM-curated, structured
  blacklist.json          # autonomous, expires entries
  watcher_state.json      # internal watcher state (debounce, last actions)
  reviews/
    2026-04-27.md         # one per trading day

.claude/
  agents/
    bot-supervisor.md     # subagent definition for slash-command driven work
  commands/
    bot-status.md
    bot-logs.md
    bot-review.md
    bot-apply.md
    bot-reject.md
    bot-improve.md
    bot-restart.md
    bot-stop.md
    bot-resume.md
    bot-blacklist.md
    bot-insights.md
    bot-watcher.md

tests/
  test_watcher_rules.py
  test_watcher_remediations.py
  test_watcher_journal.py
  test_watcher_blacklist.py
  test_bot_blacklist_read.py     # bot honours blacklist
```

---

## 3. Watcher — rules and remediations

The watcher runs a poll loop (default 30s). Each tick it:
1. Reads bot process state, recent log entries, events.jsonl tail, runtime_state.json
2. Evaluates each rule in `rules.py`
3. For triggered rules, runs the associated remediation in `remediations.py`
4. Appends `rule_trigger` records to the journal
5. Computes incremental rollups when a cycle or trade completes

### Rules (each is a pure function returning `RuleEvent | None`)

**Process / liveness**
- `process_dead`: bot.pid file missing or PID not in `ps`
- `log_silence`: no new log line for >20 min during expected operational hours
- `repeated_startup_failures`: 3 process deaths within 1 hour
- `disk_log_growth`: bot.log >100MB

**LLM / external API**
- `llm_error_spike`: ≥3 OpenRouter errors in 10 min (parses bot.log)
- `ig_allowance_burst`: ≥2 `exceeded-api-key-allowance` errors in 10 min

**Trading anomalies**
- `consecutive_losses`: configurable N losing trades in a row (default 3)
- `daily_drawdown_breach`: net pnl pct ≤ -daily_max_loss
- `weekly_drawdown_breach`: rolling 7-day pnl pct ≤ -weekly cap
- `same_symbol_stop_streak`: ≥3 stop hits on same symbol in a row → blacklist for 24h
- `confidence_outcome_divergence`: trades with confidence ≥0.80 winning <30% over last 10 (flag only)

**Pipeline health**
- `decision_empty_run`: ≥2 cycles in a row produced no decision (flag)
- `news_volume_drop`: active symbol with 0 headlines for ≥6 cycles (flag)
- `data_stale`: yfinance bar timestamp >2h old during market hours (flag)

**Risk / reconciliation**
- `position_book_divergence`: open positions in runtime_state not at broker (flag)
- `holding_too_long`: open position older than `max_holding_hours` × 1.2 (flag)

### Remediations

| Trigger rule | Action | Side effect |
|---|---|---|
| `process_dead` | Restart via `subprocess.Popen` with same args | Increment startup_failure counter |
| `repeated_startup_failures` | Stop trying, write critical alert | watcher_state.last_critical = now |
| `log_silence` | Send SIGTERM to PID, restart after 5s | Log line in journal |
| `consecutive_losses` | Touch kill switch file (existing mechanism) | Bot flattens and halts |
| `daily_drawdown_breach` | Touch kill switch file | Bot flattens and halts |
| `same_symbol_stop_streak` | Add symbol to `blacklist.json` with 24h TTL | Bot skips on next cycle |
| `disk_log_growth` | Rotate bot.log to bot.log.YYYYMMDD-HHMM, gzip | Free disk |
| `llm_error_spike` | No action — flag only | Alert emitted |
| Other "flag" rules | Append to journal, emit alert | Daily review picks up |

**Safety constraints (hard-coded):**
- Watcher never modifies `.env`, code files, runtime_state.json, decision logs, or insights.md
- Kill switch is sticky — once triggered it does not auto-clear; user must clear via `/bot-resume`
- Restart attempts capped at 3 per hour
- Blacklist entries auto-expire (default 24h) — not permanent without LLM curation

---

## 4. Memory schemas

### 4.1 `raw_journal.jsonl` — watcher-written, append-only

One JSON object per line. Six record types:

```json
{"ts":"2026-04-27T13:15:00Z","type":"cycle","cycle_id":"c_abc","symbols":["NVDA","WTI"],"duration_s":287,"trades_approved":1,"trades_rejected":3}

{"ts":"2026-04-27T13:15:01Z","type":"trade_open","cycle_id":"c_abc","symbol":"NVDA","side":"sell","entry":485.20,"stop":492.50,"tp":470.60,"confidence":0.78,"thesis_hash":"a1b2"}

{"ts":"2026-04-27T15:42:18Z","type":"trade_close","symbol":"NVDA","exit":478.10,"pnl_pct":1.46,"reason":"tp","held_minutes":147}

{"ts":"2026-04-27T13:18:22Z","type":"rule_trigger","rule":"consecutive_losses","threshold":3,"action":"kill_switch","detail":"NVDA,WTI,QQQ stopped"}

{"ts":"2026-04-27T13:20:00Z","type":"remediation","action":"blacklist_symbol","symbol":"NVDA","ttl_hours":24,"reason":"same_symbol_stop_streak"}

{"ts":"2026-04-27T22:00:00Z","type":"daily_rollup","date":"2026-04-27","trades":5,"wins":2,"losses":3,"win_rate":0.40,"net_pnl_pct":-0.8,"by_symbol":{"NVDA":{"n":2,"pnl":-1.2},"WTI":{"n":3,"pnl":0.4}}}
```

### 4.2 `insights.md` — LLM-curated, structured markdown

```markdown
# Bot Supervisor Insights

## Symbol notes
### NVDA  (last reviewed 2026-04-27)
- Earnings-week shorts repeatedly stop out (3/3 since 04-15). See trade_open IDs in raw_journal.

## Regime observations
- Underperforms on Sunday→Monday gap risk (lost on QQQ/NVDA week of 04-21).

## Improvements applied
| Date | Change | Files | Rationale | Review-on | Outcome |
|------|--------|-------|-----------|-----------|---------|
| 2026-04-25 | correlation_cap 0.7 → 0.6 | .env | Two NVDA-correlated losses same day | 2026-05-02 | pending |

## Pending suggestions (awaiting approval)
- [ ] [reviews/2026-04-26.md#1] Tighten ATR stop multiplier 1.5× → 1.8×

## Rejected ideas (don't re-suggest)
- 2026-04-22: Auto-flip side on stop — too prone to whipsaw
```

Sections are stable; daily-review agent edits in place. The interactive agent updates the "Improvements applied" table when changes are accepted, and appends to "Rejected ideas" when proposals are rejected.

### 4.3 `blacklist.json` — watcher writes, bot reads

```json
{
  "symbols": {
    "NVDA": {
      "added": "2026-04-27T15:42:18Z",
      "reason": "3 consecutive stop-outs",
      "expires": "2026-04-28T13:00:00Z"
    }
  }
}
```

Bot reads this at the start of each cycle (in `bot.py` symbol-discovery flow) and skips listed symbols whose `expires` is in the future. Expired entries get cleaned up by the watcher on next tick.

### 4.4 `reviews/YYYY-MM-DD.md` — daily-review output

Numbered, structured for selective approval:

```markdown
# Daily Review — 2026-04-27

## Summary
- N cycles, N trades, win rate, net pnl, key events

## Anomalies
- Bullet list of unexpected things from raw_journal

## Proposed changes
1. <change description>. Risk: <risk>. Files: <files>. Type: parameter|prompt|code|symbol|news.
2. ...

## Confirmed lessons (auto-merged into insights.md)
- ...
```

The "Type" tag on each proposal lets the interactive agent route the change to the right implementation pattern (parameter = .env edit; prompt = openai_decision.py edit; etc.).

---

## 5. Daily review workflow

**Schedule:** Every weekday at 21:30 UTC (after NY close + 30 min buffer). Created via Claude Code's `schedule` skill as a remote-agent routine.

**Trigger prompt** (passed to the scheduled remote agent):
```
Read the trading bot's last 24h of activity from supervisor/raw_journal.jsonl,
today's logs/*.json decision logs, recent events.jsonl, and current
supervisor/insights.md. Write supervisor/reviews/YYYY-MM-DD.md with:

(1) Summary — cycles, trades, outcomes, key events
(2) Anomalies — surprises that warrant attention
(3) Proposed changes — numbered, each with: description, risk, files affected, change type
(4) Confirmed lessons — to be merged into insights.md

Update insights.md by appending confirmed lessons to "Symbol notes" or
"Regime observations" sections. Append new pending suggestions linking
to this review file. Do NOT modify code or config files. Do NOT modify
.env. Do NOT modify raw_journal.jsonl.
```

**Acceptance flow next morning:**

```
User opens Claude Code → /bot-review
   Agent reads supervisor/reviews/<latest>.md, displays it.

User: "apply 1, 3 — skip 2, 4"
   Agent for each approved (1, 3):
     1. Identify file(s) and edit
     2. Run pytest -q
     3. If green: git commit -m "<change> (per review YYYY-MM-DD #N)"
     4. Append row to insights.md "Improvements applied" table
        with Review-on = today + 7 days
   For each rejected (2, 4):
     • Append to insights.md "Rejected ideas" with date and one-line reason
```

**Effectiveness tracking:** the daily-review agent, when it sees an Improvements row whose `Review-on` is today, computes pre/post stats from raw_journal and updates the `Outcome` column ("helped: +N% win rate" / "hurt: -N%" / "no effect"). User can revert if hurtful by saying "revert improvement on YYYY-MM-DD".

---

## 6. User interface — slash commands

All commands dispatch the `bot-supervisor` subagent (`.claude/agents/bot-supervisor.md`) with the relevant intent.

| Command | Behaviour |
|---------|-----------|
| `/bot-status` | Display: process state (alive?), cycles today, open positions, recent trades, kill-switch state, blacklisted symbols, last alert. Sources: ps, runtime_state.json, blacklist.json, alerts/. |
| `/bot-logs [N]` | Tail last N=50 (default) lines of bot.log with icons (🔴 ERROR, 🟡 WARN, 🔵 cycle, 📊 decision, ⚡ exec, 🚫 reject, 🔗 broker). |
| `/bot-review [date]` | Render `supervisor/reviews/<date or latest>.md`. |
| `/bot-apply <ids>` | Apply numbered proposals from latest review. Args: `1,3` or `1-3` or `all`. |
| `/bot-reject <ids> [reason]` | Mark proposals rejected, append to insights.md. |
| `/bot-improve` | Ask agent to do ad-hoc analysis now (without waiting for the schedule). Writes a review file with today's date suffixed `-adhoc-N.md`. |
| `/bot-restart` | Stop bot cleanly (SIGTERM), wait, restart with same args. |
| `/bot-stop` | Touch kill switch file. Bot flattens and halts. |
| `/bot-resume` | Remove kill switch file, restart bot. |
| `/bot-blacklist <SYM> [hours]` | Manually add to blacklist (default 24h). |
| `/bot-insights` | Render `insights.md`. |
| `/bot-watcher [start\|stop\|status]` | Manage watcher process. |

---

## 7. Watcher operational model

The watcher itself is a Python process started via:
```bash
nohup .venv/bin/python -m supervisor.watcher \
    --bot-pid-file bot.pid \
    --bot-log /tmp/cfd_bot.log \
    --events logs/events.jsonl \
    --interval 30 \
    > /tmp/watcher.log 2>&1 &
```

Configuration overrides via env vars (e.g. `WATCHER_CONSECUTIVE_LOSS_LIMIT=3`).

The watcher is itself crash-resilient: if it dies, hard rules don't trigger but the bot still has its own internal kill-switch + drawdown checks; on restart the watcher reloads its state from `watcher_state.json` and rebuilds rollups by replaying the journal.

`/bot-watcher start` and `/bot-watcher stop` manage the watcher process; both write `supervisor/watcher.pid` for liveness checks.

---

## 8. Bot integration touchpoints

Two changes to the bot codebase, both small:

1. **Read blacklist at cycle start.** In `bot.py` symbol discovery (`run_cycle` or `_select_active_symbols`), filter out blacklisted symbols. Add `t212_cfd_bot/supervisor_blacklist.py` with one function `read_active_blacklist(path) -> set[str]` and call it from the symbol-selection path. Log skipped symbols.

2. **Honour kill switch on resume.** Already exists; nothing new required. Confirm in tests.

No other bot code changes.

---

## 9. Testing

**Unit tests** (in `tests/`):
- `test_watcher_rules.py` — each rule fires on the right input and stays silent otherwise. Use synthetic journal fixtures.
- `test_watcher_remediations.py` — kill switch touch, blacklist write, restart command (mocked subprocess), log rotation.
- `test_watcher_journal.py` — record schemas validate, append is atomic.
- `test_watcher_blacklist.py` — TTL expiry, atomic write, concurrent read/write safe.
- `test_bot_blacklist_read.py` — bot skips blacklisted symbols, includes non-blacklisted.

**Integration test** (manual, recorded in plan): start watcher against a fake bot that emits a known event sequence; verify expected journal entries and remediation actions.

---

## 10. Failure modes & safety boundaries

| Failure | Effect | Mitigation |
|---------|--------|------------|
| Watcher crashes | Hard-rule remediations stop; bot's internal kill switch still works | `/bot-watcher status` flags it; restart by user |
| Watcher mis-blacklists a symbol | Symbol skipped for 24h | TTL expires; user can `/bot-blacklist <SYM> 0` to clear |
| Daily review proposes bad change | User selectively rejects | Approval-gated by design |
| Applied change worsens performance | Review-on stats flag it 7 days later | User can revert via `/bot-improve revert <change>` |
| Watcher restarts bot in flaky loop | 3-attempts-per-hour cap | Critical alert; user diagnoses |
| Insights.md becomes huge | Slow to read | Daily-review agent compacts old entries quarterly (out-of-scope for v1) |

---

## 11. Out of scope (v1)

- Web dashboard (terminal + Claude Code only)
- Push notifications (Telegram/email/SMS) — alerts go to alerts/ files for now
- Multi-bot supervision (only one bot supervised)
- Backtesting harness for proposed changes
- Quarterly insights compaction

These can be added incrementally if needed.
