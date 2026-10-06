---
description: Render today's (or specified date) review file (sync from OCI first)
agent: bot-supervisor
---

Args: $1 = optional YYYY-MM-DD date (or adhoc-suffix like "2026-04-28-adhoc-1").

## Sync reviews from OCI first
!`rsync -az --info=NAME0 trading-bot:/home/hostgbr/Automated-trading/supervisor/reviews/ "$(git rev-parse --show-toplevel)/supervisor/reviews/" 2>/dev/null && echo "  reviews synced"`

If $1 is provided, load that file. Otherwise pick the most recent file.

Latest review file:
!`ls -1t "$(git rev-parse --show-toplevel)/supervisor/reviews/"*.md 2>/dev/null | head -1`

Specified file (only used if $1 was supplied):
!`if [ -n "$1" ]; then cat "$(git rev-parse --show-toplevel)/supervisor/reviews/$1.md" 2>/dev/null || echo "review for $1 not found"; fi`

Latest contents:
!`if [ -z "$1" ]; then cat "$(ls -1t "$(git rev-parse --show-toplevel)/supervisor/reviews/"*.md 2>/dev/null | head -1)" 2>/dev/null || echo "no reviews yet — run /bot-improve to generate one"; fi`

Display the contents and remind the user how to apply or reject proposals (`/bot-apply 1,3` / `/bot-reject 2`).
