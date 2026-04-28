---
description: Show CFD bot status — process state, today's cycles, open positions, kill switch, blacklist, last alert
agent: bot-supervisor
---

Display a concise status report on the trading bot. The project root is `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/`. All shell commands below cd into it first.

Run this single shell snippet and present its output as a tidy table:

!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
echo "--- process ---" && \
if [ -f bot.pid ]; then PID=$(cat bot.pid); kill -0 "$PID" 2>/dev/null && echo "bot alive (PID $PID)" || echo "bot DEAD (stale PID $PID)"; else echo "bot.pid missing"; fi && \
if [ -f supervisor/watcher.pid ]; then WPID=$(cat supervisor/watcher.pid); kill -0 "$WPID" 2>/dev/null && echo "watcher alive (PID $WPID)" || echo "watcher DEAD (stale PID $WPID)"; else echo "watcher.pid missing"; fi && \
echo "--- kill switch ---" && \
if [ -f kill_switch ]; then echo "ENGAGED — bot will halt"; else echo "off"; fi && \
echo "--- today's cycles ---" && \
TODAY=$(date -u +%Y-%m-%d) && \
if [ -f supervisor/raw_journal.jsonl ]; then grep -c "\"type\":\"cycle\"" supervisor/raw_journal.jsonl 2>/dev/null | awk '{print $1" cycles in journal total"}'; grep "$TODAY" supervisor/raw_journal.jsonl | grep -c "\"type\":\"trade_close\"" 2>/dev/null | awk '{print $1" trade closes today"}'; else echo "no journal yet"; fi && \
echo "--- blacklist ---" && \
if [ -f supervisor/blacklist.json ]; then python3 -c "import json,sys; d=json.load(open('supervisor/blacklist.json')); print('\n'.join(f'  {s}: until {v.get(\"expires\",\"?\")}' for s,v in d.get('symbols',{}).items()) or '  (empty)')"; else echo "  (empty)"; fi && \
echo "--- runtime state ---" && \
if [ -f runtime_state.json ]; then python3 -c "import json; d=json.load(open('runtime_state.json')); pos=d.get('open_positions',[]) or d.get('positions',[]); print(f'open positions: {len(pos)}'); [print(f'  {p.get(\"symbol\")} {p.get(\"side\")} entry={p.get(\"entry\")} pnl={p.get(\"pnl_pct\",\"?\")}') for p in pos]" 2>/dev/null || echo "(unparseable runtime_state.json)"; else echo "no runtime state"; fi && \
echo "--- last 3 alerts ---" && \
ls -t alerts/*.log 2>/dev/null | head -1 | xargs -I {} tail -3 {} 2>/dev/null || echo "no alerts today"`

Then format the output as a status table for the user.
