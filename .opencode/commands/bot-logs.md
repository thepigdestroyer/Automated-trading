---
description: Tail the bot log on OCI London with classification icons (default 50 lines)
agent: bot-supervisor
---

Tail $1 (default 50) lines from the bot log on the OCI London server.

!`ssh trading-bot "tail -n ${1:-50} /home/hostgbr/Automated-trading/logs/bot.log 2>/dev/null || echo 'log file not yet created'"`

Classify each line and prefix with:
- 🔴 ERROR / CRITICAL
- 🟡 WARNING
- 🔵 cycle events ("cycle", "Cycle")
- 📊 decisions / trades ("decision", "trade", "Model trade", "confidence")
- ⚡ executions / orders ("Execut", "order", "deal")
- 🚫 rejections ("Reject", "STOP_TOO_TIGHT")
- 🔗 broker connect / reconcile

Plain INFO lines: just two-space indent.
