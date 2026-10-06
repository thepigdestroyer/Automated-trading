---
description: Mark proposals from latest review as rejected (local + push to OCI)
agent: bot-supervisor
---

Args: $1 = ids (e.g. `2` or `2,4`), $2 = optional reason.

Project root — resolve it, never hardcode:
!`git rev-parse --show-toplevel`

All paths below are relative to that root.

Latest review file:
!`ls -1t "$(git rev-parse --show-toplevel)/supervisor/reviews/"*.md 2>/dev/null | head -1`

Read it:
!`cat "$(ls -1t "$(git rev-parse --show-toplevel)/supervisor/reviews/"*.md 2>/dev/null | head -1)" 2>/dev/null`

For each rejected proposal in $1:
1. Find the proposal text from the latest review
2. Append a one-line entry to the "Rejected ideas" section of `supervisor/insights.md`
   Format: `- YYYY-MM-DD: <proposal summary> — <reason if provided, else "rejected by user">`

After insights.md is updated, push it back to OCI so future `/bot-improve` runs see the rejection:
!`"$(git rev-parse --show-toplevel)/scripts/push-bot-data" 2>&1`

Do NOT modify any code or config.
