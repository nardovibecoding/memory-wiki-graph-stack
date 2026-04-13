#!/usr/bin/env python3
"""lint_cron.py — run wiki lint and save a memo if issues found.

Designed for cron (no interactivity). Saves to ~/telegram-claude-bot/memo/pending/
so memo_display.py picks it up at next Claude session.
"""
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MEMO_DIR = Path.home() / "telegram-claude-bot/memo/pending"
LOG = Path("/tmp/lint_cron.log")


def run_lint():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "wiki_lint.py"), "--json"],
        capture_output=True, text=True, timeout=120,
        cwd=str(SCRIPT_DIR)
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"wiki_lint.py failed: {result.stderr[:200]}")
    return json.loads(result.stdout)


def summarise(data):
    stats = data.get("stats", {})
    issues = data.get("issues", [])

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    dups = [i for i in issues if "Near-duplicate" in i.get("message", "")]

    lines = [f"[LINT] {stats.get('files_scanned', '?')} files scanned — "
             f"{len(errors)} errors, {len(warnings)} warnings, {stats.get('auto_fixable', 0)} auto-fixable"]

    if errors:
        lines.append(f"\nErrors ({len(errors)}):")
        cat_counts = Counter(i["category"] for i in errors)
        for cat, n in cat_counts.most_common(5):
            lines.append(f"  • {cat}: {n}")

    if warnings:
        lines.append(f"\nWarnings ({len(warnings)}):")
        cat_counts = Counter(i["category"] for i in warnings)
        for cat, n in cat_counts.most_common(5):
            lines.append(f"  • {cat}: {n}")

    if dups:
        lines.append(f"\nNear-duplicates ({len(dups)}) — run /lint to merge:")
        for d in dups[:5]:
            lines.append(f"  • {d['message']}")

    if not errors and not warnings and not dups:
        return None  # clean — no memo needed

    lines.append("\nRun /lint to review + fix.")
    return "\n".join(lines)


def save_memo(text):
    MEMO_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    filename = ts.strftime("lint_%Y-%m-%d_%H%M%S.md")
    path = MEMO_DIR / filename
    content = (
        f"---\n"
        f"from: lint_cron\n"
        f"type: general\n"
        f"created: {ts.strftime('%Y-%m-%d %H:%M')}\n"
        f"status: pending\n"
        f"---\n"
        f"{text}\n"
    )
    path.write_text(content)
    return path


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        data = run_lint()
        summary = summarise(data)
        if summary:
            path = save_memo(summary)
            msg = f"{ts} — memo saved: {path.name}"
        else:
            msg = f"{ts} — clean, no memo"
    except Exception as e:
        msg = f"{ts} — ERROR: {e}"

    with LOG.open("a") as f:
        f.write(msg + "\n")
    print(msg)


if __name__ == "__main__":
    main()
