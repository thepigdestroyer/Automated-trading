---
description: Manually blacklist (or unblacklist) a symbol
agent: bot-supervisor
---

Args: $1 = symbol, $2 = TTL hours (default 24; 0 means clear).

If $2 is "0", run:
```bash
.venv/bin/python -c "from pathlib import Path; from supervisor.blacklist import clear_symbol; clear_symbol(Path('supervisor/blacklist.json'), '$1'); print('cleared $1')"
```

Otherwise (default 24h):
```bash
.venv/bin/python -c "from pathlib import Path; from supervisor.blacklist import add_symbol; add_symbol(Path('supervisor/blacklist.json'), '$1', reason='manual block by user', ttl_hours=${2:-24}); print('blacklisted $1 for ${2:-24}h')"
```

Then show the resulting blacklist contents:
`!cat supervisor/blacklist.json`
