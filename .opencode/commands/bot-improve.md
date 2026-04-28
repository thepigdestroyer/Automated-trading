---
description: Run an ad-hoc improvement analysis right now (do not wait for the daily schedule)
agent: bot-supervisor
---

Run a full improvement analysis right now. Project root is `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/`.

## Inputs to read (all absolute paths)

- `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/raw_journal.jsonl` (last 24h)
- `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/logs/events.jsonl`
- `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/logs/` (today's `*.json` decision logs — use glob)
- `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/insights.md`
- `/tmp/cfd_bot.log`

## Output

Today's UTC date:
!`date -u +%Y-%m-%d`

Find the next free adhoc index for today:
!`d=$(date -u +%Y-%m-%d); ls "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/reviews/${d}-adhoc-"*.md 2>/dev/null | wc -l | awk '{print $1+1}'`

Write the review to:
`/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/reviews/<DATE>-adhoc-<N>.md`

Required sections, in order:
1. **Summary** — N cycles, N trades, win rate, net pnl, key events
2. **Anomalies** — bullet list of unexpected things from the journal/logs
3. **Proposed changes** — numbered list. For each: Description / Risk / Files (absolute paths) / Type (parameter|prompt|code|symbol|news) / Rationale citing log timestamps or journal entries
4. **Confirmed lessons (auto-merged into insights.md)** — bullets

After writing the review, update `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/insights.md`:
- Append confirmed lessons under appropriate sections
- Append new pending suggestions linked as `[reviews/<DATE>-adhoc-<N>.md#<id>]`

Do NOT modify code, `.env`, or any bot file. Approval flows through `/bot-apply`.
