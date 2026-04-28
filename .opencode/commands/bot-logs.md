---
description: Tail the bot log with classification icons (default 50 lines)
agent: bot-supervisor
---

Tail the last $1 (default 50 if blank) lines from `/tmp/cfd_bot.log` and prefix each line with the appropriate icon (see safety rules in the agent definition). If $1 is missing, default to 50.

Run: `!tail -n ${1:-50} /tmp/cfd_bot.log`
