---
description: Manually blacklist (or unblacklist) a symbol
argument-hint: <SYMBOL> [hours|0]
---

Use the `bot-supervisor` subagent. Args: $1 = symbol, $2 = TTL hours (default 24; 0 means clear).

If $2 is "0", call `clear_symbol` from `supervisor.blacklist`. Otherwise call `add_symbol` with reason "manual block by user".

Show the resulting blacklist contents.
