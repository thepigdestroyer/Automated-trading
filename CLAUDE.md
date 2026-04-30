# Bot supervisor quickstart

## Deployment topology (since 2026-04-30)

The bot **runs on an OCI London VM**, not locally:
- SSH alias: `trading-bot` → `hostgbr@132.145.55.232` (key `~/.ssh/openvpn-ssh-key.pem`)
- Project root on server: `/home/hostgbr/Automated-trading/`
- Both services managed by systemd: `trading-bot.service` + `trading-bot-watcher.service`

The local repo at `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/` is the **source of truth for code only**. Runtime data (`raw_journal.jsonl`, `events.jsonl`, `runtime_state.json`, `blacklist.json`) lives on the OCI server.

### Workflow for code changes
1. Edit + test locally
2. `git commit && git push origin master`
3. `ssh trading-bot "cd Automated-trading && git pull --rebase && sudo systemctl restart trading-bot.service trading-bot-watcher.service"`

(The `/bot-apply` slash command does steps 1–3 automatically.)

### Workflow for review/inspection
- `sync-bot-data` — pulls journal/events/runtime_state from OCI → local
- `push-bot-data` — pushes insights.md + reviews/ from local → OCI
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

Local helper scripts:
- `bot-status` / `watcher-status` — quick CLI status (SSH-based, no LLM)
- `sync-bot-data` / `push-bot-data` — manual data sync helpers

## Daily review (manual for now)

The pre-OCI cron (`/etc/cron.d` running `claude -p` locally) was disabled at deployment. To run a review, just invoke `/bot-improve` interactively. A future improvement could install Claude CLI on the OCI server and run a remote cron there.

## Architecture
- **Bot** (`trading_bot`) — main trading loop, single-instance lock on `bot.pid`
- **Watcher** (`supervisor.watcher`) — always-on, no LLM, runs rules + mechanical remediations (kill switch, restart, blacklist, log rotate)
- **Interactive `bot-supervisor` agent** — driven by `/bot-*` commands, applies user-approved proposals only

Spec: `docs/superpowers/specs/2026-04-27-bot-supervisor-design.md`
Plan: `docs/superpowers/plans/2026-04-27-bot-supervisor-plan.md`
