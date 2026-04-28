---
description: Stop the bot cleanly and restart it
agent: bot-supervisor
---

Send SIGTERM to the bot, wait for it to exit, then start a new instance.

!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
if [ -f bot.pid ]; then \
  PID=$(cat bot.pid); \
  if kill -0 "$PID" 2>/dev/null; then \
    kill "$PID"; \
    for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$PID" 2>/dev/null || break; sleep 1; done; \
    kill -9 "$PID" 2>/dev/null; \
  fi; \
fi && \
nohup .venv/bin/python -m t212_cfd_bot --log-level INFO --log-file /tmp/cfd_bot.log > /dev/null 2>&1 & disown && \
sleep 2 && \
NEW_PID=$(ps -eo pid,cmd | awk '/python -m t212_cfd_bot/ && !/awk/{print $1; exit}') && \
echo "$NEW_PID" > bot.pid && \
echo "bot restarted PID=$NEW_PID"`
