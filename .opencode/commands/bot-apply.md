---
description: Apply numbered proposals from the latest review (e.g. /bot-apply 1,3) — pushes to GitHub then updates OCI bot
agent: bot-supervisor
---

Args: $1 = comma/dash list of proposal IDs, or "all" (e.g. `1,3` or `1-3` or `all`).

Project root — resolve it, never hardcode:
!`git rev-parse --show-toplevel`

All paths below are relative to that root; in shell blocks use `R="$(git rev-parse --show-toplevel)"`.

The trading bot itself runs on the OCI London server (alias `trading-bot`). Workflow:
1. Edit + test + commit locally
2. Push to GitHub origin/master
3. SSH to OCI: git pull + restart systemd services so the bot picks up the new code

For each approved proposal:

1. Locate the proposal in the latest review file (newest by mtime):

!`ls -1t "$(git rev-parse --show-toplevel)/supervisor/reviews/"*.md 2>/dev/null | head -1`

2. Read it:

!`cat "$(ls -1t "$(git rev-parse --show-toplevel)/supervisor/reviews/"*.md 2>/dev/null | head -1)" 2>/dev/null`

3. For each ID in $1 (parse the list, expand ranges, "all" = every proposal):
   - Determine the change type (parameter / prompt / code / symbol / news)
   - Edit the named files using the `edit` tool, joining each relative path onto the resolved project root
   - Run tests: `cd "$(git rev-parse --show-toplevel)" && .venv/bin/pytest tests/ -q`
   - If RED → abort that change and report
   - If GREEN:
     `cd "$(git rev-parse --show-toplevel)" && git add <paths> && git commit -m "<change> (per review YYYY-MM-DD #N)"`
   - Append a row to `supervisor/insights.md` "Improvements applied" table with Review-on date = today + 7 days

4. After ALL approved proposals applied successfully:
   - `git push origin master`
   - SSH to OCI and update the live bot:
     `ssh trading-bot 'cd /home/hostgbr/Automated-trading && git pull --rebase && sudo systemctl restart trading-bot.service trading-bot-watcher.service && sleep 5 && systemctl is-active trading-bot.service trading-bot-watcher.service'`
   - Report final commit hashes, push result, and OCI restart confirmation.

If ANY proposal fails tests:
   - Do NOT push or restart. Report which proposal failed and stop.

Report:
- Each change applied (proposal id, files, test result, commit hash)
- git push result
- OCI restart result (both services back to `active`)
