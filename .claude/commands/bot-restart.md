---
description: Stop the bot cleanly and restart it
---

Use the `bot-supervisor` subagent. Send SIGTERM to the PID in `bot.pid`, wait up to 10 seconds for it to exit, then start a new instance with the same arguments. Confirm new PID.
