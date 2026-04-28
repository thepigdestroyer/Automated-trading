---
description: Render today's (or specified date) daily review file
agent: bot-supervisor
---

Args: $1 = optional YYYY-MM-DD date.

If $1 is provided, load `supervisor/reviews/$1.md`. Otherwise pick the most recent file in `supervisor/reviews/` via:
`!ls -1t supervisor/reviews/*.md | head -1`

Display its contents. Tell the user how to apply or reject proposals (`/bot-apply 1,3` / `/bot-reject 2`).
