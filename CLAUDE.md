# Bot supervisor quickstart

## Starting both processes
```bash
# bot
nohup .venv/bin/python -m t212_cfd_bot --log-level INFO --log-file /tmp/cfd_bot.log > /dev/null 2>&1 &
echo $! > bot.pid

# supervisor watcher
nohup .venv/bin/python -m supervisor.watcher > /tmp/watcher.log 2>&1 &
echo $! > supervisor/watcher.pid
```

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

## Daily review schedule (one-time setup)

The daily review fires automatically at 21:30 UTC on weekdays. To register the routine, run this once in Claude Code:

> "/schedule create a recurring routine for weekday 21:30 UTC that runs the prompt from `.claude/prompts/bot-daily-review.md` against this repository."

The routine ID is then recorded in `supervisor/insights.md` under "Routines".

## Architecture
- **Watcher** (`supervisor.watcher`) — always-on, no LLM, runs rules + mechanical remediations (kill switch, restart, blacklist, log rotate)
- **Daily review agent** — scheduled remote, LLM, writes numbered improvement proposals to `supervisor/reviews/YYYY-MM-DD.md`
- **Interactive `bot-supervisor` agent** — driven by `/bot-*` commands, applies user-approved proposals only

Spec: `docs/superpowers/specs/2026-04-27-bot-supervisor-design.md`
Plan: `docs/superpowers/plans/2026-04-27-bot-supervisor-plan.md`
