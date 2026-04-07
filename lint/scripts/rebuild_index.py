#!/usr/bin/env python3
"""Rebuild wiki index.md and MEMORY.md from actual files on disk.

Scans all .md files, reads frontmatter, regenerates indexes with accurate
counts, categories, labels, and recent updates.

Usage:
  python3 rebuild_index.py --dry-run     # preview
  python3 rebuild_index.py               # apply
  python3 rebuild_index.py --scope wiki   # only wiki index.md
  python3 rebuild_index.py --scope memory # only MEMORY.md
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config_loader import load_config
_cfg = load_config()
MEMORY_DIR = _cfg["memory_dir"]
WIKI_DIR = _cfg["wiki_dir"]
SKIP_DIRS = {".git", "node_modules", "__pycache__", "sessions"}
SKIP_FILES = {"index.md", "_index.md", "librarian-log.md", "story-drafts.md",
              "migrate.py", "last_filed", "migration-manifest.json"}


def parse_frontmatter(text):
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
                val = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            fm[key.strip()] = val
    return fm, m.group(2)


def get_category_from_path(fpath, base_dir):
    """Derive category from directory structure."""
    rel = os.path.relpath(fpath, base_dir)
    parts = Path(rel).parts
    if len(parts) <= 1:
        return "Uncategorized"
    # Use directory path as category
    cat_parts = parts[:-1]  # exclude filename
    return " > ".join(p.replace("-", " ").replace("_", " ").title() for p in cat_parts)


def collect_wiki_articles():
    """Collect all wiki articles with metadata."""
    articles = []
    for root, dirnames, filenames in os.walk(WIKI_DIR):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".md") or fname in SKIP_FILES:
                continue
            fpath = Path(root) / fname
            try:
                text = fpath.read_text()
                fm, body = parse_frontmatter(text)
                title = None
                tags = []
                updated = None
                if fm:
                    title = fm.get("title") or fm.get("name")
                    tags = fm.get("tags", []) or []
                    if isinstance(tags, str):
                        tags = [t.strip() for t in tags.split(",")]
                    updated = fm.get("updated") or fm.get("created")
                if not title:
                    title = fpath.stem.replace("-", " ").replace("_", " ").title()
                category = get_category_from_path(fpath, WIKI_DIR)
                rel_path = os.path.relpath(fpath, WIKI_DIR)
                # First paragraph as description
                desc_lines = [l.strip() for l in body.strip().splitlines() if l.strip() and not l.startswith("#")]
                desc = desc_lines[0][:100] if desc_lines else ""
                articles.append({
                    "title": title,
                    "path": rel_path,
                    "category": category,
                    "tags": tags,
                    "updated": updated,
                    "desc": desc,
                })
            except Exception:
                pass
    return articles


def rebuild_wiki_index(articles, dry_run=False):
    """Rebuild wiki/index.md from articles."""
    # Category counts
    cats = defaultdict(int)
    for a in articles:
        cats[a["category"]] += 1

    # Label counts
    label_counts = defaultdict(int)
    for a in articles:
        for tag in a["tags"]:
            if tag:
                label_counts[tag.lower()] += 1

    # Recent updates (top 10 by updated date)
    dated = [a for a in articles if a["updated"]]
    dated.sort(key=lambda x: x["updated"] or "", reverse=True)
    recent = dated[:10]

    total = len(articles)
    num_cats = len(cats)

    lines = [
        "# wiki",
        "",
        "Personal knowledge wiki -- auto-maintained by the librarian.",
        "",
        f"**{total} articles** across **{num_cats} categories**.",
        "",
        "## Categories",
    ]
    for cat in sorted(cats.keys()):
        count = cats[cat]
        lines.append(f"- **{cat}** -- {count} article{'s' if count != 1 else ''}")

    lines.append("")
    lines.append("## Recent updates")
    for a in recent:
        slug = Path(a["path"]).stem
        lines.append(f"- [[{slug}]] -- {a['desc'][:80]} ({a['updated']})")

    lines.append("")
    lines.append("## Labels")
    sorted_labels = sorted(label_counts.items(), key=lambda x: (-x[1], x[0]))
    label_strs = [f"{k}({v})" for k, v in sorted_labels]
    lines.append(", ".join(label_strs))
    lines.append("")

    content = "\n".join(lines)

    if dry_run:
        print("=== WIKI INDEX (preview) ===")
        print(content[:500])
        print(f"... ({len(content)} chars total)")
        print(f"\nWould write {total} articles, {num_cats} categories, {len(label_counts)} labels")
    else:
        (WIKI_DIR / "index.md").write_text(content)
        print(f"Rebuilt wiki/index.md: {total} articles, {num_cats} categories, {len(label_counts)} labels")

    return total


def collect_memory_files():
    """Collect all memory files with metadata."""
    files = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md" or f.name == "active_rules.md":
            continue
        try:
            text = f.read_text()
            fm, body = parse_frontmatter(text)
            title = None
            ftype = None
            if fm:
                title = fm.get("title") or fm.get("name")
                ftype = fm.get("type")
            if not title:
                title = f.stem.replace("_", " ").replace("-", " ").title()
            if not ftype:
                # Infer from filename
                if f.name.startswith("convo_"):
                    ftype = "convo"
                elif f.name.startswith("feedback_"):
                    ftype = "feedback"
                elif f.name.startswith("project_"):
                    ftype = "project"
                elif f.name.startswith("reference_"):
                    ftype = "reference"
                elif f.name.startswith("user_"):
                    ftype = "user"
                elif f.name.startswith("research_"):
                    ftype = "research"
                elif f.name.startswith("bug_"):
                    ftype = "bug"
                else:
                    ftype = "other"
            # One-line description
            desc_lines = [l.strip() for l in body.strip().splitlines() if l.strip() and not l.startswith("#") and not l.startswith("---")]
            desc = desc_lines[0][:120] if desc_lines else title
            files.append({
                "name": f.name,
                "title": title,
                "type": ftype,
                "desc": desc,
            })
        except Exception:
            pass
    return files


def rebuild_memory_index(files, dry_run=False):
    """Rebuild MEMORY.md from memory files."""
    # Read existing MEMORY.md to preserve the header sections
    existing = ""
    if (MEMORY_DIR / "MEMORY.md").exists():
        existing = (MEMORY_DIR / "MEMORY.md").read_text()

    # Preserve everything before "## Conversations" or build fresh
    # Keep: header, User, Systems, Feedback, References, Projects sections
    # These are manually curated. Only rebuild the file-pointer sections.

    # Extract the manual sections (everything up to conversation pointers)
    # Find where the auto-generated section starts
    manual_end = existing.find("## Conversations")
    if manual_end == -1:
        manual_end = len(existing)
    manual_section = existing[:manual_end].rstrip()

    # Group files by type
    by_type = defaultdict(list)
    for f in files:
        by_type[f["type"]].append(f)

    lines = [manual_section, ""]

    # Conversations (recent 15)
    convos = sorted(by_type.get("convo", []), key=lambda x: x["name"], reverse=True)[:15]
    if convos:
        lines.append("## Conversations (recent)")
        for c in convos:
            hook = c["desc"][:100] if len(c["desc"]) > 5 else c["title"]
            lines.append(f"- [{c['title']}]({c['name']}) -- {hook}")
        lines.append("")

    content = "\n".join(lines) + "\n"

    if dry_run:
        print("=== MEMORY INDEX (preview) ===")
        # Show just the conversations section
        conv_start = content.find("## Conversations")
        if conv_start >= 0:
            print(content[conv_start:conv_start + 500])
        print(f"\nWould write {len(files)} files indexed, {len(convos)} recent convos")
    else:
        (MEMORY_DIR / "MEMORY.md").write_text(content)
        print(f"Rebuilt MEMORY.md: {len(files)} files, {len(convos)} recent convos shown")


def main():
    parser = argparse.ArgumentParser(description="Rebuild wiki/memory indexes")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scope", choices=["wiki", "memory", "all"], default="all")
    args = parser.parse_args()

    if args.scope in ("wiki", "all"):
        articles = collect_wiki_articles()
        rebuild_wiki_index(articles, args.dry_run)

    if args.scope in ("memory", "all"):
        files = collect_memory_files()
        rebuild_memory_index(files, args.dry_run)


if __name__ == "__main__":
    main()
