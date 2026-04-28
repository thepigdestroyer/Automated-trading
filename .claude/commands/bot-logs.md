---
description: Tail the bot log with classification icons (default 50 lines)
agent: bot-supervisor
---

Tail the last $1 (default 50) lines from the bot log and prefix each line with the appropriate icon. The log is at `/tmp/cfd_bot.log` (already absolute).

!`tail -n ${1:-50} /tmp/cfd_bot.log 2>/dev/null || echo "log file not yet created"`

Now classify each line and prefix with:
- 🔴 ERROR / CRITICAL
- 🟡 WARNING
- 🔵 cycle events ("cycle", "Cycle")
- 📊 decisions / trades ("decision", "trade", "Model trade", "confidence")
- ⚡ executions / orders ("Execut", "order", "deal")
- 🚫 rejections ("Reject")
- 🔗 broker connect / reconcile

Plain INFO lines: just two-space indent.
