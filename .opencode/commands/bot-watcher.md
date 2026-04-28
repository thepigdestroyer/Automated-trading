---
description: Manage the supervisor watcher process (start | stop | status)
agent: bot-supervisor
---

Action: $1 (default `status`).

- **start**: Launch the watcher with `nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 &`. After spawning, find the actual python PID with `pgrep -f "python -m supervisor.watcher"` (filter out bash shells), write to `supervisor/watcher.pid`.
- **stop**: SIGTERM the PID in `supervisor/watcher.pid`, then remove the file.
- **status**: Report PID, alive?, recent journal activity (last 5 entries from `supervisor/raw_journal.jsonl`).
