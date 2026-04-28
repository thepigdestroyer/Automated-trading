---
description: Clear the kill switch and restart the bot process
agent: bot-supervisor
---

Delete the `kill_switch` file if present. If the bot process is not running (check `bot.pid`), restart it with:

```bash
nohup .venv/bin/python -m t212_cfd_bot --log-level INFO --log-file /tmp/cfd_bot.log > /dev/null 2>&1 &
disown
```

Then find the actual python PID via `pgrep -f "python -m t212_cfd_bot"` (filter out bash shells), write to `bot.pid`, and confirm the new PID.
