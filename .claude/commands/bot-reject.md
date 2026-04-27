---
description: Mark proposals from latest review as rejected
argument-hint: <ids> [reason]
---

Use the `bot-supervisor` subagent. Args: $1 = ids, $2 = optional reason.

For each rejected proposal:
1. Locate the proposal text in the latest `supervisor/reviews/*.md`.
2. Append to `supervisor/insights.md` "Rejected ideas" section a one-line entry with date, summary, and the reason if provided.

Do NOT modify any code or config.
