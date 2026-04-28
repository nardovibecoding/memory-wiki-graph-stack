#!/bin/bash
# Daily memory/wiki maintenance — thin wrapper, delegates all logic to /lint --unattended.
# One engine, two surfaces: cron fires this, humans fire /lint interactively.
set -euo pipefail

LOG="/tmp/claude-memory-cron.log"
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "START unattended lint"

if ! command -v claude &>/dev/null; then
    log "ERROR: claude CLI not found"
    exit 1
fi

if $DRY_RUN; then
    log "DRY RUN: would invoke: claude -p \"/lint --unattended\" --output-format text --allowedTools \"Read,Write,Edit,Glob,Grep,Bash\""
    log "DONE (dry-run)"
    exit 0
fi

# Single source of truth: all phases, memo writing, and git commits live in SKILL.md
claude -p "/lint --unattended" \
    --output-format text \
    --allowedTools "Read,Write,Edit,Glob,Grep,Bash" \
    >> "$LOG" 2>&1 \
    || log "WARN: lint --unattended exited non-zero (check log above)"

log "DONE"
