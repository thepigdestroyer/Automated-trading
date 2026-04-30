---
description: Stop the bot + watcher systemd services on the OCI London server
agent: bot-supervisor
---

Send SIGTERM to both services. Bot's lock file releases on clean exit.

!`ssh trading-bot "sudo systemctl stop trading-bot.service trading-bot-watcher.service && \
sleep 2 && \
systemctl is-active trading-bot.service trading-bot-watcher.service | paste -sd ' / '"`

Confirm both report `inactive`.
