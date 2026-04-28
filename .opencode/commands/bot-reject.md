---
description: Mark proposals from latest review as rejected
agent: bot-supervisor
---

Args: $1 = ids (e.g. `2` or `2,4`), $2 = optional reason text.

For each rejected proposal:

1. Locate the proposal text in the latest `supervisor/reviews/*.md`:
   `!ls -1t supervisor/reviews/*.md | head -1`
2. Append to `supervisor/insights.md` "Rejected ideas" section a one-line entry with date, proposal summary, and the reason if provided.

Do NOT modify any code or config.
