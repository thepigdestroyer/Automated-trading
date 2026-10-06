---
description: Run an ad-hoc improvement analysis right now (do not wait for the daily schedule)
agent: bot-supervisor
---

The bot runs on the OCI London server. Pull the latest runtime data first, then run a full improvement analysis against it locally.

## Step 1: sync runtime data from OCI → local
!`"$(git rev-parse --show-toplevel)/scripts/sync-bot-data" 2>&1`

## Step 2: agent task

Project root (local copy of OCI runtime data) — resolve it, never hardcode:
!`git rev-parse --show-toplevel`

All paths below are relative to that root.

Inputs to read (now mirrored from OCI):
- `supervisor/raw_journal.jsonl` (last 24h)
- `logs/events.jsonl`
- `logs/*.json` (today's decision logs — use glob)
- `supervisor/insights.md`
- `logs/bot.log` (renamed from /tmp/cfd_bot.log per fix #5 of 2026-04-30-adhoc-1)
- `runtime_state.json`

Today's UTC date:
!`date -u +%Y-%m-%d`

Find the next free adhoc index for today:
!`R="$(git rev-parse --show-toplevel)"; d=$(date -u +%Y-%m-%d); ls "$R/supervisor/reviews/${d}-adhoc-"*.md 2>/dev/null | wc -l | awk '{print $1+1}'`

Write the review to:
`supervisor/reviews/<DATE>-adhoc-<N>.md`

Required sections in order:
1. **Summary** — N cycles, N trades, win rate, net pnl, key events
2. **Anomalies** — bullets
3. **Proposed changes** — numbered. Each: Description / Risk / Files / Type (parameter|prompt|code|symbol|news) / Rationale citing evidence
4. **Confirmed lessons (auto-merged into insights.md)**

After writing the review, update `supervisor/insights.md`:
- Append confirmed lessons under appropriate sections
- Append pending suggestions linked as `[reviews/<DATE>-adhoc-<N>.md#<id>]`
- Update the `Outcome` column of any "Improvements applied" rows whose `Review-on` date is today or earlier and whose Outcome is still blank, by computing pre/post stats from the journal. (`/bot-apply` sets `Review-on` to today + 7 days; this step is what closes that loop.)

## Step 3: push review + insights back to OCI
!`"$(git rev-parse --show-toplevel)/scripts/push-bot-data" 2>&1`

Do NOT modify code, `.env`, or any bot file. Approval flows through `/bot-apply`.
