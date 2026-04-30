# Bot supervisor quickstart

## Starting both processes
```bash
# bot — log defaults to logs/bot.log (per review 2026-04-30-adhoc-1 #5,
# persistent across OCI VM reboots, OneDrive-synced).
nohup .venv/bin/python -m trading_bot --log-level INFO > /dev/null 2>&1 &
echo $! > bot.pid

# supervisor watcher
nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 &
echo $! > supervisor/watcher.pid
```

> The bot's log path is `logs/bot.log` by default. Override via the
> `BOT_LOG_FILE` env var or the `--log-file <path>` flag (use `--log-file ""`
> to disable file logging entirely). The legacy path `/tmp/cfd_bot.log`
> still works if you pass it explicitly.

## Slash commands
- `/bot-status` — quick health check
- `/bot-logs [N]` — tail the bot log
- `/bot-review [date]` — read the day's review
- `/bot-apply <ids>` — apply approved proposals from the latest review (e.g. `/bot-apply 1,3`)
- `/bot-reject <ids>` — mark proposals as rejected
- `/bot-improve` — run ad-hoc analysis right now
- `/bot-stop` `/bot-resume` `/bot-restart` — process control
- `/bot-blacklist <SYM> [hours]` — manual symbol skip (default 24h, 0 = clear)
- `/bot-watcher [start|stop|status]` — manage watcher process
- `/bot-insights` — render the curated insights file

## Daily review schedule

The daily review runs via **local cron** at 21:30 UTC weekdays. It uses `claude -p` headlessly against the local checkout (a remote scheduled agent can't be used because the runtime data — raw_journal, decision logs, runtime_state — is gitignored).

Crontab entry (already installed):
```
CRON_TZ=UTC
30 21 * * 1-5 /mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated\ trading/supervisor/run_daily_review.sh
```

The wrapper script logs to `/tmp/cfd_daily_review.log`. To check it ran:
```bash
tail -50 /tmp/cfd_daily_review.log
ls -la "supervisor/reviews/" | tail
```

## Architecture
- **Watcher** (`supervisor.watcher`) — always-on, no LLM, runs rules + mechanical remediations (kill switch, restart, blacklist, log rotate)
- **Daily review** — local cron + `claude -p`, LLM analyses runtime journal, writes numbered improvement proposals to `supervisor/reviews/YYYY-MM-DD.md`
- **Interactive `bot-supervisor` agent** — driven by `/bot-*` commands, applies user-approved proposals only

Spec: `docs/superpowers/specs/2026-04-27-bot-supervisor-design.md`
Plan: `docs/superpowers/plans/2026-04-27-bot-supervisor-plan.md`
