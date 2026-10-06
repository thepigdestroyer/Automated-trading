#!/usr/bin/env bash
# Shared project-root resolver, sourced by the bot helper scripts.
#
# Resolves the repo root without hardcoding a machine-specific path, so the
# same checkout works on macOS, the OCI London VM, and any fresh clone.
# Order: explicit BOT_DIR override -> git worktree root -> script location.

if [[ -n "${BOT_DIR:-}" ]]; then
    :
elif BOT_DIR="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)"; then
    :
else
    BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
export BOT_DIR

# Remote deployment target. Override via environment if the host moves.
export BOT_REMOTE="${BOT_REMOTE:-trading-bot}"
export BOT_REMOTE_DIR="${BOT_REMOTE_DIR:-/home/hostgbr/Automated-trading}"
