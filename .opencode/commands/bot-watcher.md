---
description: Manage the supervisor watcher systemd unit on OCI (start | stop | status | restart)
agent: bot-supervisor
---

Action: $1 (default `status`).

For **status** (default):
!`ssh trading-bot "systemctl --no-pager status trading-bot-watcher.service | head -12 && echo --- && tail -5 ~/Automated-trading/supervisor/raw_journal.jsonl 2>/dev/null"`

For **start**:
!`ssh trading-bot "sudo systemctl start trading-bot-watcher.service && systemctl is-active trading-bot-watcher.service"`

For **stop**:
!`ssh trading-bot "sudo systemctl stop trading-bot-watcher.service && systemctl is-active trading-bot-watcher.service"`

For **restart**:
!`ssh trading-bot "sudo systemctl restart trading-bot-watcher.service && sleep 3 && systemctl is-active trading-bot-watcher.service"`

Choose the right block based on $1.
