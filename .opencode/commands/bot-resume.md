---
description: Start the bot + watcher systemd services on the OCI London server
agent: bot-supervisor
---

Start both services after a `/bot-stop`.

!`ssh trading-bot "sudo systemctl start trading-bot.service trading-bot-watcher.service && \
sleep 5 && \
systemctl is-active trading-bot.service trading-bot-watcher.service | paste -sd ' / '"`

Confirm both report `active`.
