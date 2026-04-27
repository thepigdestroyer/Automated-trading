---
description: Run an ad-hoc improvement analysis right now (do not wait for the daily schedule)
---

Use the `bot-supervisor` subagent. Run the full daily-review analysis right now using the same prompt as the scheduled review (see `.claude/prompts/bot-daily-review.md`), but write the output to `supervisor/reviews/YYYY-MM-DD-adhoc-N.md` (where N is the next free integer for today). Display the proposals to the user.
