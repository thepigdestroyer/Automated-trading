---
description: Restart the bot + watcher systemd services on the OCI London server
agent: bot-supervisor
---

Restart the bot and watcher cleanly via systemd. Lock release + new instance acquisition is automatic.

!`ssh trading-bot "sudo systemctl restart trading-bot.service trading-bot-watcher.service && \
sleep 5 && \
systemctl is-active trading-bot.service trading-bot-watcher.service | paste -sd ' / ' && \
BPID=\$(systemctl show trading-bot.service -p MainPID --value) && \
WPID=\$(systemctl show trading-bot-watcher.service -p MainPID --value) && \
echo 'bot PID='\$BPID' watcher PID='\$WPID"`

Confirm both services are `active running`.
