---
description: Manually blacklist (or unblacklist) a symbol
agent: bot-supervisor
---

Args: $1 = symbol (required), $2 = TTL hours (default 24; 0 means clear).

For TTL=0 (clear):
!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
.venv/bin/python -c "from pathlib import Path; from supervisor.blacklist import clear_symbol; clear_symbol(Path('supervisor/blacklist.json'), '$1'); print('cleared $1')"`

For TTL>0 (default 24h, override with $2):
!`cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && \
.venv/bin/python -c "from pathlib import Path; from supervisor.blacklist import add_symbol; add_symbol(Path('supervisor/blacklist.json'), '$1', reason='manual block by user', ttl_hours=${2:-24}); print('blacklisted $1 for ${2:-24}h')"`

Then show contents:
!`cat "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/blacklist.json" 2>/dev/null || echo "(empty)"`

Pick the right block based on $2 ("0" = clear, anything else = add).
