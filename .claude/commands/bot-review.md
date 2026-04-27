---
description: Render today's (or specified date) daily review file
argument-hint: [YYYY-MM-DD]
---

Use the `bot-supervisor` subagent. If $1 is provided, load `supervisor/reviews/$1.md`. Otherwise pick the most recent file in `supervisor/reviews/`. Display its contents. Tell the user how to apply or reject proposals (`/bot-apply 1,3` / `/bot-reject 2`).
