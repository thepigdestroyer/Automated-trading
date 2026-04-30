---
description: Render the curated supervisor insights file (sync from OCI first)
agent: bot-supervisor
---

## Sync insights from OCI first
!`sync-bot-data 2>&1 | tail -2`

Read and display the curated insights file. If it doesn't exist, say "no insights yet — run `/bot-improve` to generate the first review."

!`cat "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/insights.md" 2>/dev/null || echo "MISSING"`
