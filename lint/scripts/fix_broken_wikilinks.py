#!/usr/bin/env python3
"""Strip broken [[wikilinks]] from memory + wiki files.

Converts [[nonexistent-target]] to plain text "nonexistent-target".
Only removes links whose targets don't resolve to any file.

Usage:
  python3 fix_broken_wikilinks.py --dry-run   # preview
  python3 fix_broken_wikilinks.py              # apply
"""

import os
import re
import sys
from pathlib import Path

from config_loader import load_config
_cfg = load_config()
MEMORY_DIR = _cfg["memory_dir"]
WIKI_DIR = _cfg["wiki_dir"]
SKIP_DIRS = {"archive", ".git", "node_modules", "__pycache__", "sessions"}
SKIP_FILES = {"MEMORY.md"}


def collect_files(dirs):
    files = []
    for base in dirs:
        if not base.exists():
            continue
        for root, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                if f.endswith(".md") and f not in SKIP_FILES:
                    files.append(Path(root) / f)
    return files


def build_title_map(files):
    """Build set of known titles/stems for wikilink resolution."""
    titles = set()
    for path in files:
        # Add filename stem variants
        stem = path.stem.lower()
        titles.add(stem)
        titles.add(stem.replace("-", " "))
        titles.add(stem.replace("_", " "))

        # Add frontmatter title
        try:
            text = path.read_text()
            m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
            if m:
                for line in m.group(1).splitlines():
                    if line.startswith("title:") or line.startswith("name:"):
                        val = line.split(":", 1)[1].strip().strip("'\"")
                        if val:
                            titles.add(val.lower())
        except Exception:
            pass
    return titles


def fix_file(path, titles, dry_run=False):
    """Remove broken wikilinks from a file. Returns count of fixes."""
    try:
        text = path.read_text()
    except Exception:
        return 0

    def replacer(match):
        link = match.group(1)
        # Check if target exists
        link_lower = link.lower()
        if link_lower in titles:
            return match.group(0)  # keep valid link
        return link  # strip brackets, keep text

    new_text = re.sub(r"\[\[([^\]]+)\]\]", replacer, text)
    if new_text == text:
        return 0

    count = len(re.findall(r"\[\[([^\]]+)\]\]", text)) - len(re.findall(r"\[\[([^\]]+)\]\]", new_text))

    if dry_run:
        rel = os.path.relpath(path, Path.home())
        print(f"  {rel}: {count} broken wikilinks")
    else:
        path.write_text(new_text)

    return count


def main():
    dry_run = "--dry-run" in sys.argv

    dirs = [MEMORY_DIR, WIKI_DIR]
    files = collect_files(dirs)
    titles = build_title_map(files)

    if dry_run:
        print("=== DRY RUN ===\n")

    total = 0
    touched = 0
    for f in files:
        count = fix_file(f, titles, dry_run)
        if count > 0:
            total += count
            touched += 1

    action = "Would strip" if dry_run else "Stripped"
    print(f"\n{action} {total} broken wikilinks across {touched} files")


if __name__ == "__main__":
    main()
