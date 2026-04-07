#!/usr/bin/env python3
"""Memory lifecycle management: recency decay, maturity promotion, staleness detection, convo pruning."""
import re
import sys
from datetime import date, datetime
from pathlib import Path

from lint.scripts.config_loader import load_config
_cfg = load_config()
MEMORY_DIR = _cfg["memory_dir"]
INDEX = MEMORY_DIR / "MEMORY.md"
TODAY = date.today()

# Thresholds
DECAY_MILD_DAYS = 7       # -2 importance after 7 days
DECAY_HEAVY_DAYS = 30     # -5 importance after 30 days
PROMOTE_DRAFT_ACCESS = 10       # draft -> validated
PROMOTE_VALIDATED_ACCESS = 25   # validated -> core
STALE_IMPORTANCE = 10
STALE_DAYS = 60
CONVO_MAX_AGE_DAYS = 14
CONVO_MIN_IMPORTANCE = 20


def parse_frontmatter(text):
    """Return (frontmatter_dict, body, raw_fm_text) or (None, text, None)."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not m:
        return None, text, None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            val = val.strip()
            if val == "null" or val == "":
                val = None
            elif key.strip() in ("importance", "access_count") and val.replace(".", "").replace("-", "").isdigit():
                val = float(val) if "." in val else int(val)
            fm[key.strip()] = val
    return fm, m.group(2), m.group(1)


def days_since(date_str):
    """Days since a YYYY-MM-DD date string."""
    if not date_str:
        return 999
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        return (TODAY - d).days
    except (ValueError, TypeError):
        return 999


def update_frontmatter_field(text, field, value):
    """Update a field in the frontmatter section of a markdown file."""
    pattern = re.compile(rf"^({field}:).*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(rf"\1 {value}", text)
    # Add field before closing ---
    return text.replace("\n---\n", f"\n{field}: {value}\n---\n", 1)


def remove_from_index(filename):
    """Remove a file entry from MEMORY.md."""
    if not INDEX.exists():
        return
    lines = INDEX.read_text().splitlines()
    new_lines = [l for l in lines if filename not in l]
    if len(new_lines) < len(lines):
        INDEX.write_text("\n".join(new_lines) + "\n")


def process_file(filepath, dry_run=False):
    """Process a single memory file. Returns list of actions taken."""
    text = filepath.read_text()
    fm, body, raw_fm = parse_frontmatter(text)
    if fm is None:
        return []

    actions = []
    importance = fm.get("importance", 50)
    if not isinstance(importance, (int, float)):
        importance = 50
    access_count = fm.get("access_count", 0)
    if not isinstance(access_count, (int, float)):
        access_count = 0
    maturity = fm.get("maturity", "validated")
    last_accessed = fm.get("last_accessed")
    days = days_since(last_accessed)

    new_importance = importance
    new_maturity = maturity
    should_delete = False

    # 1. Recency decay
    if days > DECAY_HEAVY_DAYS:
        new_importance = max(0, new_importance - 5)
        if new_importance != importance:
            actions.append(f"decay -5 (last accessed {days}d ago)")
    elif days > DECAY_MILD_DAYS:
        new_importance = max(0, new_importance - 2)
        if new_importance != importance:
            actions.append(f"decay -2 (last accessed {days}d ago)")

    # 2. Maturity promotion
    if maturity == "draft" and access_count >= PROMOTE_DRAFT_ACCESS:
        new_maturity = "validated"
        actions.append(f"promote draft->validated (access_count={access_count})")
    elif maturity == "validated" and access_count >= PROMOTE_VALIDATED_ACCESS:
        new_maturity = "core"
        actions.append(f"promote validated->core (access_count={access_count})")

    # 3. Auto-prune old convos
    if filepath.name.startswith("convo_"):
        created = fm.get("created")
        created_days = days_since(created)
        if created_days > CONVO_MAX_AGE_DAYS and new_importance < CONVO_MIN_IMPORTANCE:
            should_delete = True
            actions.append(f"prune convo (created {created_days}d ago, importance={new_importance})")

    # 4. Staleness alert (draft files only)
    if maturity == "draft" and new_importance < STALE_IMPORTANCE and days > STALE_DAYS:
        actions.append(f"STALE: draft, importance={new_importance}, {days}d since access")

    if not actions:
        return []

    if dry_run:
        return actions

    if should_delete:
        filepath.unlink()
        remove_from_index(filepath.name)
        return actions

    # Apply changes
    new_text = text
    if new_importance != importance:
        new_text = update_frontmatter_field(new_text, "importance", new_importance)
    if new_maturity != maturity:
        new_text = update_frontmatter_field(new_text, "maturity", new_maturity)

    if new_text != text:
        tmp = filepath.with_suffix(".tmp")
        tmp.write_text(new_text)
        tmp.rename(filepath)

    return actions


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN ===\n")

    files = sorted(MEMORY_DIR.glob("*.md"))
    files = [f for f in files if f.name != "MEMORY.md"]

    total_actions = 0
    for f in files:
        actions = process_file(f, dry_run=dry_run)
        if actions:
            total_actions += len(actions)
            for a in actions:
                prefix = "WOULD" if dry_run else "DID"
                print(f"  {prefix}: {f.name} -> {a}")

    print(f"\n{'Would take' if dry_run else 'Took'} {total_actions} actions across {len(files)} files")


if __name__ == "__main__":
    main()
