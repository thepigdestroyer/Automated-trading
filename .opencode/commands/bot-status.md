---
description: Show CFD bot status — process state, today's cycles, open positions, kill switch, blacklist, last alert
agent: bot-supervisor
---

Display a concise status report on the trading bot:

1. Process state: read `bot.pid`, check if PID is running.
2. Today's cycles: count cycle records in `supervisor/raw_journal.jsonl` for today's date.
3. Open positions: read `runtime_state.json` and list open positions with entry/PnL.
4. Kill switch: check whether `kill_switch` file exists in project root.
5. Blacklist: list active symbols from `supervisor/blacklist.json` with expiry times.
6. Last alert: tail `alerts/` directory for newest entry.

Format as a short table. No emojis unless tagging severity.
