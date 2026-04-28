---
description: Stop the bot cleanly and restart it
agent: bot-supervisor
---

Send SIGTERM to the PID in `bot.pid`, wait up to 10 seconds for it to exit, then start a new instance with:

```bash
nohup .venv/bin/python -m t212_cfd_bot --log-level INFO --log-file /tmp/cfd_bot.log > /dev/null 2>&1 &
disown
```

Locate the actual python PID with `pgrep -f "python -m t212_cfd_bot"` (filter out bash shells), write it to `bot.pid`, and confirm the new PID.
