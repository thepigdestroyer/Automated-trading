#!/usr/bin/env bash
# Wrapper for the daily review — invoked by cron at 21:30 UTC weekdays.
# Runs Claude Code in print mode against the local project; output goes to /tmp.
set -euo pipefail

PROJECT_ROOT="/mnt/c/Users/mrina/OneDrive/Documents/AI/Opencode/Automated trading"
PROMPT_FILE="${PROJECT_ROOT}/.claude/prompts/bot-daily-review.md"
LOG="/tmp/cfd_daily_review.log"
CLAUDE="/home/mj/.local/bin/claude"

cd "${PROJECT_ROOT}"

{
    echo "=== daily review run: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    if [[ ! -f "${PROMPT_FILE}" ]]; then
        echo "ERROR: prompt file missing at ${PROMPT_FILE}"
        exit 1
    fi
    "${CLAUDE}" \
        --print \
        --permission-mode acceptEdits \
        --add-dir "${PROJECT_ROOT}" \
        "$(cat "${PROMPT_FILE}")"
    echo "=== run complete: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
} >>"${LOG}" 2>&1
