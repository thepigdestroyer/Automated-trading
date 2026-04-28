---
description: Manage the supervisor watcher process (start | stop | status)
agent: bot-supervisor
---

Action: $1 (default `status`).

For **start**:
!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
if [ -f supervisor/watcher.pid ] && kill -0 "$(cat supervisor/watcher.pid)" 2>/dev/null; then \
  echo "watcher already running PID $(cat supervisor/watcher.pid)"; \
else \
  nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 & disown; \
  sleep 2; \
  PID=$(ps -eo pid,cmd | awk '/python -m supervisor.watcher/ && !/awk/{print $1; exit}'); \
  echo "$PID" > supervisor/watcher.pid; \
  echo "watcher started PID=$PID"; \
fi`

For **stop**:
!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
if [ -f supervisor/watcher.pid ]; then \
  PID=$(cat supervisor/watcher.pid); \
  kill "$PID" 2>/dev/null && echo "watcher $PID signalled" || echo "watcher $PID not running"; \
  sleep 2; kill -9 "$PID" 2>/dev/null; \
  rm -f supervisor/watcher.pid; \
else \
  echo "no watcher.pid"; \
fi`

For **status** (default):
!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
if [ -f supervisor/watcher.pid ]; then \
  PID=$(cat supervisor/watcher.pid); \
  if kill -0 "$PID" 2>/dev/null; then \
    echo "watcher alive PID=$PID"; \
    ps -p "$PID" -o etime,pcpu,pmem; \
  else \
    echo "watcher DEAD (stale PID $PID)"; \
  fi; \
else \
  echo "watcher not running (no pid file)"; \
fi && \
echo "--- last 5 journal entries ---" && \
tail -5 supervisor/raw_journal.jsonl 2>/dev/null || echo "no journal yet"`

Choose the right block based on $1, then summarise the output.
