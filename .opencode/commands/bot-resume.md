---
description: Clear the kill switch and restart the bot process
agent: bot-supervisor
---

Delete the kill switch and (re)start the bot from the project root.

!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
rm -f kill_switch && \
if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then \
  echo "bot already running PID $(cat bot.pid)"; \
else \
  nohup .venv/bin/python -m trading_bot --log-level INFO --log-file /tmp/cfd_bot.log > /dev/null 2>&1 & disown; \
  sleep 2; \
  PID=$(ps -eo pid,cmd | awk '/python -m trading_bot/ && !/awk/{print $1; exit}'); \
  echo "$PID" > bot.pid; \
  echo "bot started PID=$PID"; \
fi`

Confirm the new PID to the user.
