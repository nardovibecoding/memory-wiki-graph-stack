# memory-wiki-graph-stack

A complete, production-grade personal knowledge base powered by LLMs. Inspired by [Karpathy's LLM Wiki concept](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), but built as a working system.

**You read it; the LLM writes it. The stack maintains it.**

## What is this?

A modular toolkit that turns a folder of markdown files into a self-maintaining knowledge wiki with:

- **Hybrid search** (BM25 + vector + recency + graph traversal)
- **Knowledge graph** (auto-extracted from wikilinks, tags, and references)
- **Integrity auditing** (lint: detect broken links, orphans, schema drift, stale claims)
- **Auto-fix** (rebuild indexes, strip broken links, sync graph)
- **Lifecycle management** (importance decay, maturity promotion, auto-pruning)
- **Daily maintenance cron** (consolidation, archival, promotion, lint)

## Architecture

```
┌─────────────────────────────────────────────────┐
│                    Schema Layer                  │
│         config.yaml + CLAUDE.md rules            │
├─────────────────────────────────────────────────┤
│                     Wiki Layer                   │
│   markdown files + index.md + MEMORY.md          │
│   ┌──────────┐ ┌──────────┐ ┌──────────┐       │
│   │  Search   │ │  Graph   │ │   Lint   │       │
│   │ BM25+Vec  │ │ Unified  │ │ Audit+Fix│       │
│   └──────────┘ └──────────┘ └──────────┘       │
├─────────────────────────────────────────────────┤
│                  Raw Sources Layer               │
│      memory files, code, notes, imports          │
└─────────────────────────────────────────────────┘
```

## Components

| Component | What it does | Language |
|-----------|-------------|----------|
| **lint/** | Detect issues, fix them, rebuild indexes, sync graph | Python |
| **search/** | Hybrid BM25 + vector search with RRF fusion | Node.js |
| **graph/** | Extract knowledge graph from markdown, merge with code graph | Python |
| **lifecycle/** | Importance decay, maturity promotion, auto-pruning | Python |
| **hooks/** | Session hooks for auto-filing, sync monitoring | Python |
| **cron/** | Daily maintenance: consolidate, archive, promote, lint | Bash |
| **wiki-scaffold/** | Starter wiki structure with index template | Markdown |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/nardovibecoding/memory-wiki-graph-stack.git
cd memory-wiki-graph-stack
cp config.yaml ~/.config/memory-wiki-graph-stack/config.yaml
# Edit config.yaml with your paths
```

### 2. Initialize your wiki

```bash
# Create wiki directory with starter structure
./scripts/init.sh ~/my-wiki

# Creates:
# ~/my-wiki/index.md        (auto-generated catalog)
# ~/my-wiki/meta/            (librarian logs, migration manifests)
# ~/my-wiki/uncategorized/   (landing zone for new articles)
```

### 3. Install search dependencies

```bash
cd search
npm install
# Downloads all-MiniLM-L6-v2 locally (~22MB, no API key needed)
```

### 4. Run your first lint

```bash
cd lint/scripts
python3 wiki_lint.py --scope wiki          # scan only
python3 wiki_lint.py --scope wiki --fix    # scan + fix + rebuild index
```

### 5. Build search index

```bash
node search/build-index.mjs    # builds tag index
node search/search.mjs "your query here"   # hybrid search
```

### 6. Set up daily cron (optional)

```bash
# Install the launchd plist (macOS) or crontab entry (Linux)
./scripts/install-cron.sh
```

## Operations

### Ingest
New files added to your wiki or memory directory are automatically picked up by search indexing, graph merging, and lint scanning.

### Query
```bash
# Hybrid search (BM25 + vector + recency + graph)
node search/search.mjs "how does the auth flow work"

# Tag-based search
node search/search.mjs "#trading"

# JSON output for programmatic use
node search/search.mjs "query" --json
```

### Lint
```bash
# Full audit
python3 lint/scripts/wiki_lint.py

# Auto-fix (strip broken links, rebuild indexes, sync graph)
python3 lint/scripts/wiki_lint.py --fix

# Memory only / wiki only
python3 lint/scripts/wiki_lint.py --scope memory
python3 lint/scripts/wiki_lint.py --scope wiki

# Machine-readable
python3 lint/scripts/wiki_lint.py --json
```

Lint checks:
- Schema conformance (missing frontmatter fields)
- Orphan detection (files not in any index)
- Dead links (broken `[[wikilinks]]` and `[title](path.md)` links)
- Stale references (90+ day old files with many URLs)
- Missing cross-references (same entity in 3+ files, no links between them)
- Expired memos (past TTL)
- Graph sync (stale graph, missing nodes)

### Graph
```bash
# Merge wiki + memory into unified knowledge graph
python3 graph/graph_merge.py

# Output: graph_unified.json (compatible with graphify visualization)
```

## Frontmatter Schema

Every markdown file should have:

```yaml
---
title: Article Title
type: article          # article, user, feedback, project, reference, convo, research, memo, bug, lesson
tags: [tag1, tag2]
created: 2025-01-15
updated: 2025-01-20
---
```

Optional fields: `importance` (0-100), `maturity` (draft/validated/core), `source`, `status`.

## Integration with Claude Code

This stack works standalone, but is designed to integrate with [Claude Code](https://claude.ai/code):

- **Memory**: Claude Code's auto-memory system produces the markdown files this stack indexes
- **Skills**: The `/lint` and `/recall` skills wrap these tools for interactive use
- **Hooks**: Session hooks auto-file knowledge, monitor sync, and trigger graph rebuilds
- **CLAUDE.md**: Schema rules live in your CLAUDE.md so the LLM follows them

### Skills

The `/lint` and `/recall` skills live in claude-skills. Install them by cloning that repo and copying to `~/.claude/skills/`.

**`/lint`** — unified audit + fix + promote, three phases:
1. Deterministic scan (`wiki_lint.py`) — schema, orphans, dead links, stale refs, cross-refs, expired memos, graph sync
2. LLM deep audit — contradictions, gap detection, stale claims, unused skills
3. Promote chain — runs `promote/promote_lessons.py` on pending lessons, shows votes + rule text, asks for confirmation before applying

```
/lint          # full run (Phase 1 + 2 + 3)
/lint --quick  # Phase 1 only (fast, no LLM)
/lint --fix    # Phase 1 with auto-fix + index rebuild + graph sync
```

**`/recall`** — hybrid search across memory + wiki (BM25 + vector + tag filtering):
```
/recall authentication flow
/recall #trading
```

## Compared to Karpathy's LLM Wiki

| Feature | Karpathy's concept | memory-wiki-graph-stack |
|---------|-------------------|----------------|
| Architecture | 3 layers (raw/wiki/schema) | Same 3 layers, implemented |
| Ingest | Described | Auto via hooks + cron |
| Query | Described | BM25 + vector + graph hybrid search |
| Lint | Described | 7 checks + auto-fix + index rebuild |
| Graph | Not mentioned | Unified knowledge graph from wikilinks + tags |
| Search | qmd (mentioned) | Built-in hybrid with local embeddings |
| Lifecycle | Not mentioned | Importance decay, maturity promotion, auto-pruning |
| Maintenance | Manual | Daily cron, fully automated |

## License

MIT
