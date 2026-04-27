---
description: Clear the kill switch and restart the bot process
---

Use the `bot-supervisor` subagent. Delete the `kill_switch` file if present. If the bot process is not running, restart it via the same nohup command pattern used to start it (`.venv/bin/python -m t212_cfd_bot --log-level INFO --log-file /tmp/cfd_bot.log`). Confirm new PID.
