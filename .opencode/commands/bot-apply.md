---
description: Apply numbered proposals from the latest review (e.g. /bot-apply 1,3)
agent: bot-supervisor
---

Args: $1 = comma/dash list of proposal IDs, or "all" (e.g. `1,3` or `1-3` or `all`).

For each approved proposal:

1. Locate the proposal in the latest `supervisor/reviews/*.md` file (newest by mtime).
2. Determine the change type (parameter / prompt / code / symbol / news) from its metadata.
3. Edit the named files using the `edit` tool.
4. Run `.venv/bin/pytest tests/ -q`. If anything fails, abort the change and report.
5. If green, `git add` only the modified files and `git commit -m "<change> (per review YYYY-MM-DD #N)"`.
6. Append a row to `supervisor/insights.md` "Improvements applied" table with Review-on date = today + 7 days.

Report each change applied, each test result, and each commit hash.

Latest review:
!ls -1t supervisor/reviews/*.md | head -1
