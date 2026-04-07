#!/bin/bash
# Daily wiki maintenance: lifecycle + lint + fix
# Runs via launchd/cron. No LLM calls by default (add --with-llm for LLM phases).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
LOG="/tmp/llm-wiki-stack-cron.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "START: daily maintenance"

# Phase 1: Lifecycle management (decay, promotion, pruning)
log "Phase 1: Lifecycle"
python3 "$ROOT/lifecycle/memory_lifecycle.py" >> "$LOG" 2>&1 || log "  WARN: lifecycle errors"

# Phase 2: Lint + auto-fix (deterministic)
log "Phase 2: Lint + fix"
cd "$ROOT/lint/scripts"
python3 wiki_lint.py --fix >> "$LOG" 2>&1 || log "  WARN: lint errors"

# Phase 3: Rebuild search index
log "Phase 3: Search index"
if command -v node &>/dev/null && [[ -f "$ROOT/search/build-index.mjs" ]]; then
    node "$ROOT/search/build-index.mjs" >> "$LOG" 2>&1 || log "  WARN: index build errors"
fi

# Phase 4 (optional): LLM-powered consolidation
if [[ "${1:-}" == "--with-llm" ]]; then
    LLM_CLI="${LLM_WIKI_CLI:-claude}"
    if command -v "$LLM_CLI" &>/dev/null; then
        log "Phase 4: LLM consolidation"

        # Get lint report for triage
        LINT_OUT=$(cd "$ROOT/lint/scripts" && python3 wiki_lint.py 2>&1) || true
        LINT_ERRORS=$(echo "$LINT_OUT" | grep -c '^\s*\[E\]' || true)
        LINT_WARNINGS=$(echo "$LINT_OUT" | grep -c '^\s*\[W\]' || true)

        if [[ "$LINT_ERRORS" -gt 0 || "$LINT_WARNINGS" -gt 5 ]]; then
            log "  LLM triage: $LINT_ERRORS errors, $LINT_WARNINGS warnings"
            $LLM_CLI -p "Wiki lint found issues:

$LINT_OUT

Fix dead links, schema issues, and obvious orphans. Be conservative. Report what you fixed in 3-5 bullets." \
                --allowedTools "Read,Write,Edit,Glob,Grep" \
                >> "$LOG" 2>&1 || log "  WARN: LLM triage errors"
        fi
    fi
fi

log "DONE"
