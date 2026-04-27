---
description: Tail the bot log with classification icons (default 50 lines)
argument-hint: [count]
---

Use the `bot-supervisor` subagent. Tail the last $1 (default 50) lines from `/tmp/cfd_bot.log` and prefix each line with the appropriate icon (see safety rules in the agent definition). If $1 is missing, default to 50.
