---
description: Manage the supervisor watcher process (start | stop | status)
argument-hint: start|stop|status
---

Use the `bot-supervisor` subagent. Action: $1 (default `status`).

- start: Launch the watcher with `nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 &`. Save PID to `supervisor/watcher.pid`.
- stop: SIGTERM the PID in `supervisor/watcher.pid`, then remove the file.
- status: Report PID, alive?, recent journal activity (last 5 entries).
