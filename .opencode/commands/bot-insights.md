---
description: Render the curated supervisor insights file
agent: bot-supervisor
---

Read and display the curated insights file. If it doesn't exist, say "no insights yet — run `/bot-improve` to generate the first review."

!`cat "/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading/supervisor/insights.md" 2>/dev/null || echo "MISSING"`
