---
description: Run an ad-hoc improvement analysis right now (do not wait for the daily schedule)
agent: bot-supervisor
---

Run the full daily-review analysis right now using the prompt template at `.claude/prompts/bot-daily-review.md` (also valid as `.opencode/prompts/bot-daily-review.md` if present).

Read these inputs:
- `supervisor/raw_journal.jsonl` (last 24h of records)
- `logs/*.json` (today's decision logs)
- `logs/events.jsonl` (recent events)
- `supervisor/insights.md` (current curated insights)
- `/tmp/cfd_bot.log` (raw bot stdout/stderr)

Write the output to `supervisor/reviews/YYYY-MM-DD-adhoc-N.md` where N is the next free integer for today (1, 2, 3...).

Required sections, in order:
1. **Summary** — N cycles, N trades, win rate, net pnl, key events
2. **Anomalies** — bullet list of unexpected things
3. **Proposed changes** — numbered, each with Description / Risk / Files / Type (parameter|prompt|code|symbol|news) / Rationale citing journal evidence
4. **Confirmed lessons (auto-merged into insights.md)** — bullets

After writing the review, update `supervisor/insights.md`:
- Append confirmed lessons under appropriate sections
- Append new pending suggestions linked as `[reviews/YYYY-MM-DD-adhoc-N.md#N]`

Do NOT modify code, `.env`, or any bot file. Approval flows through `/bot-apply`.

Today (UTC): !`date -u +%Y-%m-%d`
