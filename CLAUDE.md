# Bot supervisor quickstart

## Deployment topology (since 2026-04-30)

The bot **runs on an OCI London VM**, not locally:
- SSH alias: `trading-bot` (host, user and key live in your local `~/.ssh/config`, never in this repo)
- Project root on server: `~/Automated-trading/` (in the deploy user's home)
- Both services managed by systemd: `trading-bot.service` + `trading-bot-watcher.service`

The local repo is the **source of truth for code only**. Runtime data (`raw_journal.jsonl`, `events.jsonl`, `runtime_state.json`, `blacklist.json`) lives on the OCI server.

The checkout location differs per machine, so never hardcode it. Resolve the project root with `git rev-parse --show-toplevel` (or source `scripts/bot-dir.sh`, which exports `BOT_DIR`, `BOT_REMOTE` and `BOT_REMOTE_DIR`).

### Workflow for code changes
1. Edit + test locally
2. `git commit && git push origin master`
3. `ssh trading-bot "cd Automated-trading && git pull --rebase && sudo systemctl restart trading-bot.service trading-bot-watcher.service"`

(The `/bot-apply` slash command does steps 1–3 automatically.)

### Workflow for review/inspection
- `scripts/sync-bot-data` — pulls journal/events/runtime_state/reviews from OCI → local
- `scripts/push-bot-data` — pushes insights.md + reviews/ from local → OCI
- The slash commands `/bot-improve`, `/bot-status`, `/bot-logs`, etc. handle this transparently

### Direct server access
```bash
ssh trading-bot                            # interactive shell
ssh trading-bot 'systemctl status trading-bot.service'
ssh trading-bot 'journalctl -u trading-bot.service -f'
ssh trading-bot 'tail -f Automated-trading/logs/bot.log'
```

## Starting/stopping (on the OCI server, but typically via slash commands)
```bash
sudo systemctl start trading-bot.service trading-bot-watcher.service
sudo systemctl stop trading-bot.service trading-bot-watcher.service
sudo systemctl restart trading-bot.service trading-bot-watcher.service
```

systemd handles restart-on-failure (5 attempts in 300s), MemoryMax (400 MB bot, 80 MB watcher), and stdout/stderr redirection to `logs/systemd-bot-{stdout,stderr}.log` and `logs/systemd-watcher-{stdout,stderr}.log`. The bot's structured Python logger writes to `logs/bot.log` (per review 2026-04-30-adhoc-1 #5).

## Slash commands
All commands now talk to the OCI server transparently:
- `/bot-status` — quick health check (systemctl + runtime_state + last events)
- `/bot-logs [N]` — tail the bot log on remote
- `/bot-review [date]` — read the day's review (syncs from OCI first)
- `/bot-apply <ids>` — apply approved proposals (edit local → push → OCI git pull + restart)
- `/bot-reject <ids>` — mark proposals as rejected (push insights back to OCI)
- `/bot-improve` — run ad-hoc analysis right now (syncs from OCI, runs review locally, pushes back)
- `/bot-stop` `/bot-resume` `/bot-restart` — systemctl on remote
- `/bot-blacklist <SYM> [hours]` — manual symbol skip (writes blacklist.json on OCI directly)
- `/bot-watcher [start|stop|status|restart]` — manage watcher service
- `/bot-insights` — render the curated insights file (syncs from OCI first)

Local helper scripts (committed, in `scripts/`):
- `scripts/sync-bot-data` / `scripts/push-bot-data` — manual data sync helpers
- `scripts/bot-dir.sh` — sourced by both; resolves `BOT_DIR` without hardcoding a path

## Daily review (manual for now)

The pre-OCI cron (`/etc/cron.d` running `claude -p` locally) was disabled at deployment, and its wrapper `supervisor/run_daily_review.sh` plus the duplicated prompt under `.opencode/prompts/` were removed on 2026-09-28. To run a review, just invoke `/bot-improve` interactively. A future improvement could install the CLI on the OCI server and run a remote cron there.

## Architecture
- **Bot** (`trading_bot`) — main trading loop, single-instance lock on `bot.pid`
- **Watcher** (`supervisor.watcher`) — always-on, no LLM, runs rules + mechanical remediations (kill switch, restart, blacklist, log rotate)
- **Interactive `bot-supervisor` agent** — driven by `/bot-*` commands, applies user-approved proposals only

Spec: `docs/superpowers/specs/2026-04-27-bot-supervisor-design.md`
Plan: `docs/superpowers/plans/2026-04-27-bot-supervisor-plan.md`
