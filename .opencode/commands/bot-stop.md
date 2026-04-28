---
description: Trigger the kill switch — bot flattens all positions and halts
agent: bot-supervisor
---

Touch the `kill_switch` file in the project root:

`!touch kill_switch && ls -la kill_switch`

Confirm to the user that the bot will flatten all open positions on its next cycle iteration. Tell them how to resume (`/bot-resume`).
