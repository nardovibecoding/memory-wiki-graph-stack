#!/bin/bash
# Full daily maintenance with LLM-powered phases (Claude Code specific)
# Includes: research archival, librarian promotion, lifecycle, lint, consolidation
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$SCRIPT_DIR/.."
MEMORY_DIR="$HOME/.claude/projects/default/memory"
ARCHIVE_DIR="$MEMORY_DIR/archive"
WIKI_DIR="$HOME/wiki"
LOG="/tmp/claude-memory-cron.log"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# --- Gate: Skip if no changes ---
CHANGED=$(find -L "$MEMORY_DIR" -name '*.md' -mtime -1 -not -path '*/archive/*' 2>/dev/null | wc -l | tr -d ' ')
CUTOFF=$(date -v-2d '+%Y-%m-%d')
STALE_RESEARCH=0
for f in "$MEMORY_DIR"/research_*_????-??-??.md; do
    [[ ! -f "$f" ]] && continue
    FDATE=$(echo "$f" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | tail -1)
    [[ -n "$FDATE" && "$FDATE" < "$CUTOFF" ]] && STALE_RESEARCH=$((STALE_RESEARCH + 1))
done
OLD_ARCHIVE=$(find "$ARCHIVE_DIR" -name 'summary_*.md' -mtime +30 2>/dev/null | wc -l | tr -d ' ')

if [[ "$CHANGED" -eq 0 && "$STALE_RESEARCH" -eq 0 && "$OLD_ARCHIVE" -eq 0 ]]; then
    log "SKIP: No changes in 24h"
    exit 0
fi

log "START: $CHANGED files changed, $STALE_RESEARCH stale research, $OLD_ARCHIVE expired archives"
mkdir -p "$ARCHIVE_DIR"

if ! command -v claude &>/dev/null; then
    log "ERROR: claude CLI not found"
    exit 1
fi

if $DRY_RUN; then
    log "DRY RUN: Would process $CHANGED changed files"
    exit 0
fi

# --- Phase 1: Research archival ---
if [[ "$STALE_RESEARCH" -gt 0 || "$OLD_ARCHIVE" -gt 0 ]]; then
    log "PHASE 1: Research archival"
    find "$ARCHIVE_DIR" -name 'summary_*.md' -mtime +30 2>/dev/null | while read -r f; do
        log "  Expired: $(basename "$f")"
        rm "$f"
        sed -i '' "/$(basename "$f")/d" "$MEMORY_DIR/MEMORY.md" 2>/dev/null || true
    done

    for f in "$MEMORY_DIR"/research_*_????-??-??.md; do
        [[ ! -f "$f" ]] && continue
        FDATE=$(echo "$f" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | tail -1)
        [[ -z "$FDATE" || ! "$FDATE" < "$CUTOFF" ]] && continue
        BASENAME=$(basename "$f")
        SUMMARY_NAME="summary_${BASENAME}"
        log "  Archiving: $BASENAME"
        claude -p "Read $f, create condensed summary (10-15 lines). Write to $ARCHIVE_DIR/$SUMMARY_NAME. Update $MEMORY_DIR/MEMORY.md. Delete original $f." \
            --allowedTools "Read,Write,Edit,Bash(rm file)" \
            >> "$LOG" 2>&1 || log "  WARN: Failed to archive $BASENAME"
    done
fi

# --- Phase 1.5: Lifecycle ---
log "PHASE 1.5: Lifecycle"
python3 "$ROOT/lifecycle/memory_lifecycle.py" >> "$LOG" 2>&1 || log "  WARN: Lifecycle errors"

# --- Phase 1.7: Librarian promotion ---
log "PHASE 1.7: Librarian promotion"
if [[ -d "$WIKI_DIR" ]]; then
    claude -p "You are the wiki librarian. Scan $MEMORY_DIR for memory files worth promoting to $WIKI_DIR.

## What to promote
- Validated patterns describing HOW something works
- Architecture decisions with reasoning
- Tool/platform knowledge (gotchas, workarounds)
- Research summaries with actionable findings

## What NOT to promote
- convo_*.md, user_*.md, feedback_*.md, project status updates
- Anything already in wiki

## How to file
1. Glob $WIKI_DIR/**/*.md to see existing articles
2. Update existing or create new with proper frontmatter
3. Cross-link with [[wikilinks]] ONLY to articles that already exist (Glob first to verify)
4. Append to $WIKI_DIR/meta/librarian-log.md

Be conservative." \
        --allowedTools "Read,Write,Edit,Glob,Grep,Bash(ls directory)" \
        >> "$LOG" 2>&1 || log "  WARN: Librarian errors"
fi

# --- Phase 1.9: Lint + fix ---
log "PHASE 1.9: Lint"
cd "$ROOT/lint/scripts"
LINT_OUT=$(python3 wiki_lint.py --fix 2>&1) || true
echo "$LINT_OUT" >> "$LOG"
LINT_ERRORS=$(echo "$LINT_OUT" | grep -c '^\s*\[E\]' || true)
LINT_WARNINGS=$(echo "$LINT_OUT" | grep -c '^\s*\[W\]' || true)
log "  Lint: ${LINT_ERRORS} errors, ${LINT_WARNINGS} warnings"

if [[ "$LINT_ERRORS" -gt 0 || "$LINT_WARNINGS" -gt 5 ]]; then
    log "  LLM triage"
    claude -p "Wiki lint issues:
$LINT_OUT
Fix dead links, schema issues, obvious orphans. Be conservative. Report in 3-5 bullets." \
        --allowedTools "Read,Write,Edit,Glob,Grep,Bash(ls directory),Bash(rm file)" \
        >> "$LOG" 2>&1 || log "  WARN: LLM triage errors"
fi

# --- Phase 2: Consolidation ---
if [[ "$CHANGED" -gt 0 ]]; then
    log "PHASE 2: Consolidation"
    claude -p "Memory consolidation on $MEMORY_DIR.
1. List and skim files for overlaps/contradictions
2. Merge overlapping topics, convert relative dates, delete contradicted facts
3. Keep MEMORY.md under 200 lines, each entry under 150 chars
4. Take MAX importance, SUM access_counts when merging. Never downgrade core files.
Be conservative." \
        --allowedTools "Read,Write,Edit,Glob,Grep,Bash(ls directory),Bash(wc file)" \
        >> "$LOG" 2>&1 || log "  WARN: Consolidation errors"
fi

# --- Phase 3: Git ---
cd "$MEMORY_DIR"
if ! (git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]); then
    log "Committing memory changes"
    git add -A .
    git commit -m "memory-cron: daily consolidation $(date '+%Y-%m-%d')

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>" || true
    git push 2>/dev/null || log "  WARN: memory push failed"
fi

if [[ -d "$WIKI_DIR" ]] && cd "$WIKI_DIR" 2>/dev/null; then
    if ! (git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]); then
        log "Committing wiki changes"
        git add -A .
        git commit -m "librarian: daily promotion $(date '+%Y-%m-%d')

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>" || true
        git push 2>/dev/null || log "  WARN: wiki push failed"
    fi
fi

log "DONE"
