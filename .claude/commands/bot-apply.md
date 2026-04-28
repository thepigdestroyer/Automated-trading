---
description: Apply numbered proposals from the latest review (e.g. /bot-apply 1,3)
agent: bot-supervisor
---

Args: $1 = comma/dash list of proposal IDs, or "all" (e.g. `1,3` or `1-3` or `all`).

Project root: `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/`

For each approved proposal:

1. Locate the proposal in the latest review file (newest by mtime):

!`ls -1t "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/reviews/"*.md 2>/dev/null | head -1`

2. Read it:

!`cat "$(ls -1t '/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/reviews/'*.md 2>/dev/null | head -1)" 2>/dev/null`

3. For each ID in $1 (parse the list, expand ranges, "all" = every proposal):
   - Determine the change type (parameter / prompt / code / symbol / news) from its metadata
   - Edit the named files using the `edit` tool with absolute paths starting with the project root
   - Run tests: `cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && .venv/bin/pytest tests/ -q`
   - If tests fail, abort that change and report
   - If green:
     `cd "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading" && git add <paths> && git commit -m "<change> (per review YYYY-MM-DD #N)"`
   - Append a row to `/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/insights.md` "Improvements applied" table with Review-on date = today + 7 days

Report each change applied, each test result, and each commit hash.
