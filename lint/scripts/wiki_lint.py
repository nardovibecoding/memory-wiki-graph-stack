#!/usr/bin/env python3
"""Unified wiki lint — one-pass integrity audit for memory + wiki.

Checks:
  1. Schema conformance (missing/malformed frontmatter fields)
  2. Orphan detection (files not referenced by any index or wikilink)
  3. Dead links (references to nonexistent files)
  4. Stale refs (outdated dates, removed tools/repos)
  5. Missing cross-refs (same entity in multiple files, no link between them)
  6. MEMORY.md index health (dangling pointers, missing entries)
  7. Expired memos (general memos past TTL)

Usage:
  python3 wiki_lint.py                # full report
  python3 wiki_lint.py --fix          # auto-fix safe issues (dead index entries, schema gaps)
  python3 wiki_lint.py --json         # machine-readable output
  python3 wiki_lint.py --scope memory # only scan memory/
  python3 wiki_lint.py --scope wiki   # only scan wiki/
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from config_loader import load_config
_cfg = load_config()
MEMORY_DIR = _cfg["memory_dir"]
WIKI_DIR = _cfg["wiki_dir"]
MEMO_DIR = _cfg["memo_dir"]
INDEX_FILE = MEMORY_DIR / "MEMORY.md"
WIKI_INDEX = WIKI_DIR / "index.md"
TODAY = date.today()

SKIP_FILES = {"MEMORY.md", "memory_stats.json", ".story_state.json"}
SKIP_DIRS = {"archive", ".git", "node_modules", "__pycache__", "sessions"}

# Unified schema required fields
REQUIRED_FM = {"title", "type", "created", "updated"}
OPTIONAL_FM = {"tags", "status", "importance", "maturity", "source"}
VALID_TYPES = {"user", "feedback", "project", "reference", "convo", "research", "memo", "bug", "lesson", "article"}
VALID_STATUSES = {"draft", "validated", "core", "active", "archived", "expired", "promoted", "pending", None}
VALID_MATURITIES = {"draft", "validated", "core", None}

# Memo TTL (days)
MEMO_TTL_GENERAL = 7


def parse_frontmatter(text):
    """Return (dict, body) or (None, text)."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not m:
        return None, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            val = val.strip()
            if val in ("null", ""):
                val = None
            elif val.startswith("[") and val.endswith("]"):
                # Parse YAML-style list
                val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            fm[key.strip()] = val
    return fm, m.group(2)


def days_since(date_str):
    if not date_str:
        return 999
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        return (TODAY - d).days
    except (ValueError, TypeError):
        return 999


def collect_files(dirs):
    """Collect all .md files from given directories, respecting skips."""
    files = {}
    for base_dir in dirs:
        if not base_dir.exists():
            continue
        for root, dirnames, filenames in os.walk(base_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                if not fname.endswith(".md") or fname in SKIP_FILES:
                    continue
                fpath = Path(root) / fname
                files[str(fpath)] = fpath
    return files


def extract_wikilinks(text):
    """Extract [[wikilinks]] from text."""
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def extract_md_links(text):
    """Extract [title](path.md) links from text."""
    return re.findall(r"\[([^\]]*)\]\(([^)]+\.md[^)]*)\)", text)


def extract_entities(text):
    """Extract likely entity names (capitalized multi-word or known patterns)."""
    # Tools, products, people mentioned in text
    entities = set()
    # @bot_name patterns
    entities.update(re.findall(r"@(\w+_bot)", text))
    # Capitalized phrases (2+ words)
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        entities.add(m.group(1))
    return entities


class LintReport:
    def __init__(self):
        self.issues = []
        self.stats = {"files_scanned": 0, "issues": 0, "auto_fixable": 0}

    def add(self, severity, category, file_path, message, fixable=False):
        self.issues.append({
            "severity": severity,  # error, warning, info
            "category": category,
            "file": str(file_path),
            "message": message,
            "fixable": fixable,
        })
        self.stats["issues"] += 1
        if fixable:
            self.stats["auto_fixable"] += 1

    def print_report(self):
        if not self.issues:
            print(f"Clean. {self.stats['files_scanned']} files scanned, 0 issues.")
            return

        by_cat = defaultdict(list)
        for issue in self.issues:
            by_cat[issue["category"]].append(issue)

        print(f"\n{'='*60}")
        print(f"WIKI LINT REPORT — {TODAY}")
        print(f"{'='*60}")
        print(f"Files scanned: {self.stats['files_scanned']}")
        print(f"Issues found:  {self.stats['issues']}")
        print(f"Auto-fixable:  {self.stats['auto_fixable']}")
        print()

        for cat, items in sorted(by_cat.items()):
            print(f"\n## {cat} ({len(items)})")
            print("-" * 40)
            for item in items:
                icon = {"error": "E", "warning": "W", "info": "I"}[item["severity"]]
                fix = " [fixable]" if item["fixable"] else ""
                rel = os.path.relpath(item["file"], Path.home())
                print(f"  [{icon}] {rel}: {item['message']}{fix}")

    def to_json(self):
        return json.dumps({"stats": self.stats, "issues": self.issues}, indent=2)


def lint_schema(files, report):
    """Check frontmatter schema conformance."""
    for fpath, path in files.items():
        try:
            text = path.read_text()
        except Exception:
            report.add("error", "read_error", fpath, "Cannot read file")
            continue

        fm, _ = parse_frontmatter(text)
        if fm is None:
            report.add("warning", "schema", fpath, "No frontmatter found", fixable=True)
            continue

        # Check required fields
        for field in REQUIRED_FM:
            if field not in fm or fm[field] is None:
                # 'name' is old schema for 'title'
                if field == "title" and "name" in fm:
                    report.add("info", "schema", fpath, "Uses old 'name' field instead of 'title'", fixable=True)
                else:
                    report.add("warning", "schema", fpath, f"Missing required field: {field}", fixable=True)

        # Validate type
        ftype = fm.get("type")
        if ftype and ftype not in VALID_TYPES:
            report.add("info", "schema", fpath, f"Non-standard type: {ftype}")

        # Validate maturity
        mat = fm.get("maturity")
        if mat and mat not in VALID_MATURITIES:
            report.add("warning", "schema", fpath, f"Invalid maturity: {mat}")

        # Check stale dates
        updated = fm.get("updated")
        if updated:
            d = days_since(updated)
            if d is not None and d > 180:
                report.add("info", "staleness", fpath, f"Not updated in {d} days")

        # Check mtime newer than frontmatter updated: (file edited but frontmatter not bumped)
        if updated:
            try:
                fm_date = datetime.strptime(str(updated)[:10], "%Y-%m-%d").date()
                file_mtime = date.fromtimestamp(os.path.getmtime(path))
                if (file_mtime - fm_date).days > 0:
                    report.add("info", "staleness", fpath,
                               f"File edited {file_mtime} but updated: is {fm_date} — bump frontmatter?")
            except (ValueError, OSError):
                pass


def lint_index(files, report, fix=False):
    """Check MEMORY.md index health."""
    if not INDEX_FILE.exists():
        report.add("error", "index", str(INDEX_FILE), "MEMORY.md not found")
        return

    index_text = INDEX_FILE.read_text()
    index_links = extract_md_links(index_text)
    memory_files = {p.name for p in MEMORY_DIR.glob("*.md") if p.name not in SKIP_FILES}

    referenced_in_index = set()
    dangling = []
    for title, link in index_links:
        referenced_in_index.add(link)
        # Check if target exists
        target = MEMORY_DIR / link
        if not target.exists():
            dangling.append((title, link))
            report.add("error", "dead_link", str(INDEX_FILE), f"Dangling: [{title}]({link})", fixable=True)

    # Files not in index (orphans from memory dir, excluding convos older than 7 days)
    for fname in memory_files:
        if fname not in referenced_in_index and not fname.startswith("active_"):
            fpath = MEMORY_DIR / fname
            fm, _ = parse_frontmatter(fpath.read_text())
            # Skip if it's a recent file (< 2 days)
            if fm:
                created = fm.get("created")
                if created and days_since(created) < 2:
                    continue
            report.add("info", "orphan", str(fpath), "Not referenced in MEMORY.md")

    # Fix dangling entries
    if fix and dangling:
        lines = index_text.splitlines()
        new_lines = []
        for line in lines:
            skip = False
            for title, link in dangling:
                if link in line:
                    skip = True
                    break
            if not skip:
                new_lines.append(line)
        INDEX_FILE.write_text("\n".join(new_lines) + "\n")
        print(f"  Fixed: removed {len(dangling)} dangling entries from MEMORY.md")


def lint_wiki_index(report):
    """Check wiki index.md health."""
    if not WIKI_INDEX.exists():
        return

    index_text = WIKI_INDEX.read_text()

    # Check for files mentioned in index that don't exist
    for title, link in extract_md_links(index_text):
        target = WIKI_DIR / link
        if not target.exists():
            report.add("error", "dead_link", str(WIKI_INDEX), f"Dangling: [{title}]({link})")


def lint_wikilinks(files, report):
    """Check [[wikilinks]] resolve to actual files."""
    # Build title->path map
    title_map = {}
    for fpath, path in files.items():
        try:
            fm, _ = parse_frontmatter(path.read_text())
            if fm:
                title = fm.get("title") or fm.get("name") or path.stem
                title_map[title.lower()] = fpath
                # Also map filename stem variants
                stem = path.stem.lower()
                title_map[stem] = fpath
                title_map[stem.replace("-", " ")] = fpath
                title_map[stem.replace("_", " ")] = fpath
                title_map[stem.replace("-", " ").replace("_", " ")] = fpath
        except Exception:
            pass

    # Skip index/catalog files (they use wikilinks as catalog entries, not cross-refs)
    skip_names = {"index.md", "_index.md", "librarian-log.md"}
    for fpath, path in files.items():
        if path.name in skip_names:
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        for link in extract_wikilinks(text):
            if link.lower() not in title_map:
                report.add("info", "broken_wikilink", fpath, f"[[{link}]] target not found")


def lint_cross_refs(files, report):
    """Find entities mentioned in multiple files but never cross-linked."""
    entity_files = defaultdict(set)

    for fpath, path in files.items():
        try:
            text = path.read_text()
        except Exception:
            continue
        for entity in extract_entities(text):
            entity_files[entity].add(fpath)

    # Find entities in 3+ files with no wikilinks between them
    for entity, fpaths in entity_files.items():
        if len(fpaths) >= 3:
            # Check if any of these files link to each other
            has_link = False
            for fp in fpaths:
                try:
                    text = Path(fp).read_text()
                    links = extract_wikilinks(text)
                    link_targets = {l.lower() for l in links}
                    for other_fp in fpaths:
                        if other_fp != fp:
                            other_stem = Path(other_fp).stem.lower().replace("-", " ").replace("_", " ")
                            if other_stem in link_targets or entity.lower() in link_targets:
                                has_link = True
                                break
                except Exception:
                    pass
                if has_link:
                    break

            if not has_link:
                sample = [os.path.relpath(f, Path.home()) for f in list(fpaths)[:3]]
                report.add("info", "missing_crossref", str(list(fpaths)[0]),
                           f"'{entity}' in {len(fpaths)} files, no cross-links. e.g. {', '.join(sample)}")


def lint_memos(report):
    """Check for expired memos."""
    if not MEMO_DIR.exists():
        return

    for path in MEMO_DIR.glob("*.md"):
        try:
            text = path.read_text()
            fm, _ = parse_frontmatter(text)
            if not fm:
                continue
            memo_type = fm.get("type", "general")
            created = fm.get("created")
            if memo_type == "general" and created:
                age = days_since(created)
                if age > MEMO_TTL_GENERAL:
                    report.add("warning", "expired_memo", str(path),
                               f"General memo expired ({age}d old, TTL={MEMO_TTL_GENERAL}d)", fixable=True)
        except Exception:
            pass


def lint_stale_claims(files, report):
    """Flag files referencing potentially stale external resources."""
    for fpath, path in files.items():
        try:
            text = path.read_text()
            fm, _ = parse_frontmatter(text)
            if not fm:
                continue
            # Only flag old files with URLs
            updated = fm.get("updated")
            if updated and days_since(updated) > 90:
                urls = re.findall(r"https?://[^\s\)\]]+", text)
                if len(urls) > 3:
                    report.add("info", "stale_refs", fpath,
                               f"{len(urls)} URLs in file not updated for {days_since(updated)}d")
        except Exception:
            pass


GRAPH_JSON = _cfg["graph_output"]
GRAPH_MERGE_SCRIPT = _cfg["graph_merge_script"]
LESSONS_DIR = WIKI_DIR / "lessons"


def lint_graph_sync(files, report):
    """Check if unified graph is in sync with wiki/memory files."""
    if not GRAPH_JSON.exists():
        report.add("warning", "graph_sync", str(GRAPH_JSON), "graph_unified.json not found")
        return

    # Check graph freshness
    import stat as stat_mod
    graph_mtime = os.path.getmtime(GRAPH_JSON)
    graph_age_hours = (datetime.now().timestamp() - graph_mtime) / 3600

    if graph_age_hours > 24:
        report.add("warning", "graph_sync", str(GRAPH_JSON),
                   f"Graph is {graph_age_hours:.0f}h old (>24h). Run graph_merge.py to refresh.")

    # Load graph and check node coverage
    try:
        with open(GRAPH_JSON) as f:
            graph = json.load(f)
        graph_nodes = {n.get("id", "") for n in graph.get("nodes", [])}

        # Check what % of wiki/memory files have graph nodes
        missing_from_graph = []
        for fpath, path in files.items():
            slug = path.stem.lower()
            slug = re.sub(r'[^a-z0-9_]', '_', slug)
            slug = re.sub(r'_+', '_', slug).strip('_')
            if slug not in graph_nodes and not path.name.startswith("convo_"):
                missing_from_graph.append(fpath)

        if missing_from_graph:
            pct = len(missing_from_graph) / len(files) * 100
            report.add("info", "graph_sync", str(GRAPH_JSON),
                       f"{len(missing_from_graph)} files ({pct:.0f}%) missing from graph. "
                       f"Run graph_merge.py to sync.")
        # Check meta.updated_at exists and is not stale
        meta = graph.get("meta", {})
        updated_at = meta.get("updated_at")
        if not updated_at:
            report.add("warning", "graph_sync", str(GRAPH_JSON),
                       "Graph missing meta.updated_at — use write_graph() to write it.")
        else:
            age_days = days_since(updated_at)
            if age_days is not None and age_days > 1:
                report.add("info", "graph_sync", str(GRAPH_JSON),
                           f"meta.updated_at is {age_days}d old — run graph_merge.py to refresh.")

    except Exception as e:
        report.add("error", "graph_sync", str(GRAPH_JSON), f"Cannot parse graph: {e}")


def _word_set(text):
    """Lowercase word set (4+ chars) for Jaccard comparison."""
    return set(re.findall(r"\b\w{4,}\b", text.lower()))


def lint_semantic_dedup(files, report, threshold=0.75):
    """Find near-duplicate wiki files via Jaccard word-set similarity.

    Only runs on wiki files (memory convos excluded — too noisy).
    Opt-in via --semantic flag due to O(n²) cost.
    """
    wiki_files = [
        (fp, path)
        for fp, path in files.items()
        if str(WIKI_DIR) in fp and not path.name.startswith("convo_")
    ]

    texts = {}
    for fp, path in wiki_files:
        try:
            _, body = parse_frontmatter(path.read_text())
            words = _word_set(body)
            if words:
                texts[fp] = (path, words)
        except Exception:
            pass

    items = list(texts.items())
    flagged = set()
    for i in range(len(items)):
        fp_a, (_, words_a) = items[i]
        for j in range(i + 1, len(items)):
            fp_b, (_, words_b) = items[j]
            union = words_a | words_b
            if not union:
                continue
            sim = len(words_a & words_b) / len(union)
            if sim >= threshold:
                pair_key = tuple(sorted([fp_a, fp_b]))
                if pair_key not in flagged:
                    flagged.add(pair_key)
                    rel_a = os.path.relpath(fp_a, Path.home())
                    rel_b = os.path.relpath(fp_b, Path.home())
                    report.add("warning", "semantic_dedup", fp_a,
                               f"Near-duplicate ({sim:.0%}): {rel_a} ↔ {rel_b}")


def lint_lesson_election(report):
    """Audit NardoWorld/lessons/ for missing or pending promotion status.

    Each lesson file should have:
      status: promoted  — rule is encoded in build_system_prompt.py
      status: pending   — lesson exists but not yet in system prompt

    Files missing 'status' are flagged as schema gaps (fixable).
    Files with 'status: pending' are flagged for promotion review.
    """
    if not LESSONS_DIR.exists():
        return

    missing_status = []
    pending = []

    for lf in sorted(LESSONS_DIR.glob("*.md")):
        try:
            fm, _ = parse_frontmatter(lf.read_text())
        except Exception:
            continue
        if fm is None:
            missing_status.append(lf)
            continue
        status = fm.get("status")
        if status is None:
            missing_status.append(lf)
        elif status == "pending":
            pending.append(lf)

    if missing_status:
        sample = ", ".join(f.stem for f in missing_status[:5])
        suffix = "..." if len(missing_status) > 5 else ""
        report.add("warning", "lesson_missing_status", str(LESSONS_DIR),
                   f"{len(missing_status)} lesson(s) missing 'status' field: {sample}{suffix}",
                   fixable=False)

    if pending:
        sample = ", ".join(f.stem for f in pending[:5])
        suffix = "..." if len(pending) > 5 else ""
        report.add("info", "pending_election", str(LESSONS_DIR),
                   f"{len(pending)} lesson(s) pending promotion to system prompt: {sample}{suffix}")


def main():
    parser = argparse.ArgumentParser(description="Unified wiki lint")
    parser.add_argument("--fix", action="store_true", help="Auto-fix safe issues")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--scope", choices=["memory", "wiki", "all"], default="all")
    parser.add_argument("--no-semantic", action="store_true",
                        help="Skip semantic dedup check (slow, O(n²))")
    args = parser.parse_args()

    dirs = []
    if args.scope in ("memory", "all"):
        dirs.append(MEMORY_DIR)
    if args.scope in ("wiki", "all"):
        dirs.append(WIKI_DIR)
    if MEMO_DIR.exists():
        dirs.append(MEMO_DIR)

    files = collect_files(dirs)
    report = LintReport()
    report.stats["files_scanned"] = len(files)

    # Run all checks
    lint_schema(files, report)
    lint_index(files, report, fix=args.fix)
    lint_wiki_index(report)
    lint_wikilinks(files, report)
    lint_cross_refs(files, report)
    lint_memos(report)
    lint_stale_claims(files, report)
    lint_graph_sync(files, report)
    lint_lesson_election(report)
    if not args.no_semantic:
        lint_semantic_dedup(files, report)

    # Auto-fix: strip broken wikilinks + rebuild indexes
    if args.fix:
        # Strip broken wikilinks
        from fix_broken_wikilinks import build_title_map, fix_file, collect_files as bwl_collect
        all_paths = list(files.values())
        titles = build_title_map(all_paths)
        wl_fixed = 0
        for path in all_paths:
            wl_fixed += fix_file(path, titles)
        if wl_fixed:
            print(f"  Fixed: stripped {wl_fixed} broken wikilinks")

        # Rebuild indexes
        from rebuild_index import collect_wiki_articles, rebuild_wiki_index
        from rebuild_index import collect_memory_files, rebuild_memory_index
        if args.scope in ("wiki", "all"):
            articles = collect_wiki_articles()
            rebuild_wiki_index(articles)
        if args.scope in ("memory", "all"):
            mem_files = collect_memory_files()
            rebuild_memory_index(mem_files)

        # Rebuild graph if stale
        if GRAPH_MERGE_SCRIPT.exists():
            import subprocess
            result = subprocess.run(
                ["python3", str(GRAPH_MERGE_SCRIPT)],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                print("  Fixed: rebuilt unified graph")
            else:
                print(f"  WARN: graph rebuild failed: {result.stderr[:100]}")

    if args.json:
        print(report.to_json())
    else:
        report.print_report()

    # Exit code: 1 if errors, 0 otherwise
    errors = [i for i in report.issues if i["severity"] == "error"]
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
