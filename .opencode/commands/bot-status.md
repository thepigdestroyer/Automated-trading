---
description: Show CFD bot status — process state, today's cycles, open positions, kill switch, blacklist
agent: bot-supervisor
---

The bot runs on the OCI London server (alias `trading-bot`, project root `~/Automated-trading/`). Display a concise status table.

!`ssh trading-bot 'cd ~/Automated-trading && \
echo "--- systemd ---" && \
systemctl is-active trading-bot.service trading-bot-watcher.service | paste -sd " / " | awk "{print \"bot: \"\$1\"  watcher: \"\$3}" && \
BPID=$(systemctl show trading-bot.service -p MainPID --value); WPID=$(systemctl show trading-bot-watcher.service -p MainPID --value); \
echo "PIDs: bot=$BPID watcher=$WPID" && \
ps -p $BPID -o etime=,pcpu=,pmem= 2>/dev/null | awk "{printf \"bot uptime=%s cpu=%s%% mem=%s%%\n\", \$1, \$2, \$3}" && \
echo "--- kill switch ---" && \
[ -f kill_switch ] && echo "ENGAGED" || echo "off" && \
echo "--- runtime state ---" && \
.venv/bin/python -c "import json; s=json.load(open(\"runtime_state.json\")); pos=s.get(\"positions\") or {}; closed=s.get(\"closed_trades\"); print(f\"account_balance: {s.get(\\\"account_balance\\\")}  starting: {s.get(\\\"starting_balance\\\")}  daily_pnl: {s.get(\\\"daily_pnl\\\")}\"); print(f\"open positions: {len(pos)}\"); [print(f\"  {p.get(\\\"symbol\\\")} {p.get(\\\"direction\\\")} entry={p.get(\\\"entry_price\\\")} last={p.get(\\\"last_price\\\")}\") for p in pos.values()]; print(f\"closed trades today: {len(closed) if isinstance(closed, list) else 0}\")" 2>/dev/null && \
echo "--- blacklist ---" && \
[ -f supervisor/blacklist.json ] && .venv/bin/python -c "import json; d=json.load(open(\"supervisor/blacklist.json\")); items=d.get(\"symbols\",{}); print(\"\\n\".join(f\"  {s}: until {v.get(\\\"expires\\\",\\\"?\\\")}\" for s,v in items.items()) or \"  (empty)\")" || echo "  (empty)" && \
echo "--- last 5 events ---" && \
tail -5 logs/events.jsonl 2>/dev/null | .venv/bin/python -c "import sys,json
for line in sys.stdin:
    d=json.loads(line); ts=d.get(\"timestamp\",\"\")[:19]
    print(f\"  {ts}  {d.get(\\\"event\\\",\\\"?\\\"):14}  {(d.get(\\\"cycle_id\\\") or \\\"\\\")[:10]}\")"'`

Format the output as a status table for the user.
