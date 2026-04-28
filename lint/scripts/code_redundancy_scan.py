#!/usr/bin/env python3
"""Code-redundancy scanner — finds source-code dead modules + redundancy markers.

Scans common source dirs for Phase-N prototype markers, superseded/deprecated
comments, and config-disabled modules still wired. Reports findings; doesn't
auto-delete (human reviews first).

Output: text summary by default, --json for machine-readable.

Triggered by: lint Phase 7. Standalone usage:
  python3 code_redundancy_scan.py
  python3 code_redundancy_scan.py --scope ~/prediction-markets/packages
  python3 code_redundancy_scan.py --json > findings.json

Default scopes:
  ~/prediction-markets/packages/
  ~/.claude/hooks/
  ~/.claude/scripts/

Source: pm-london wedge 2026-04-25 — adversarial-detector.ts was a Phase 1
prototype superseded by Python pipeline, config-off, but ingestion still
wired. Caused production wedge. This scanner would have flagged it.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_SCOPES = [
    Path.home() / "prediction-markets" / "packages",
    Path.home() / ".claude" / "hooks",
    Path.home() / ".claude" / "scripts",
]

# (regex, severity, why) — case-insensitive match in comments
MARKERS = [
    (r"phase\s+\d+\s+(prototype|experimental|alpha)", "HIGH", "phase-N prototype marker"),
    (r"superseded\s+by", "HIGH", "explicitly superseded"),
    (r"replaced\s+by", "HIGH", "explicitly replaced"),
    (r"\bdeprecated\b", "MEDIUM", "deprecated marker"),
    (r"\blegacy\b", "MEDIUM", "legacy marker"),
    (r"//\s*(killed|removed|dead\s+code)", "MEDIUM", "kill marker (TS/JS)"),
    (r"#\s*(killed|removed|dead\s+code)", "MEDIUM", "kill marker (Py/sh)"),
    (r"todo[:\s]+(delete|remove|kill)", "LOW", "todo-delete"),
    (r"out\s+of\s+scope\s+for\s+this\s+module", "LOW", "self-flagged scope-creep"),
]

CODE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".sh", ".go", ".rs", ".swift"}
EXCLUDE_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv", ".bigd-worktrees"}


def scan_file(path: Path) -> list[dict]:
    findings = []
    try:
        text = path.read_text(errors="replace")
    except Exception as e:
        return [{"file": str(path), "line": 0, "severity": "WARN", "why": f"read failed: {e}", "match": ""}]
    for i, line in enumerate(text.splitlines(), 1):
        for pattern, severity, why in MARKERS:
            m = re.search(pattern, line, flags=re.IGNORECASE)
            if m:
                findings.append({
                    "file": str(path),
                    "line": i,
                    "severity": severity,
                    "why": why,
                    "match": line.strip()[:120],
                })
                break  # one finding per line
    return findings


def walk_scope(root: Path) -> list[Path]:
    if not root.exists():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in filenames:
            p = Path(dirpath) / f
            if p.suffix in CODE_EXTS:
                out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", action="append", default=None, help="Override default scopes (repeatable)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    ap.add_argument("--severity", default="LOW", choices=["LOW", "MEDIUM", "HIGH"], help="Minimum severity to report")
    args = ap.parse_args()

    scopes = [Path(s).expanduser() for s in args.scope] if args.scope else DEFAULT_SCOPES

    all_findings = []
    files_scanned = 0
    for scope in scopes:
        files = walk_scope(scope)
        files_scanned += len(files)
        for f in files:
            all_findings.extend(scan_file(f))

    sev_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    threshold = sev_order[args.severity]
    filtered = [f for f in all_findings if sev_order.get(f["severity"], 3) >= threshold]
    filtered.sort(key=lambda x: (-sev_order.get(x["severity"], 3), x["file"], x["line"]))

    if args.json:
        json.dump({
            "files_scanned": files_scanned,
            "findings_total": len(all_findings),
            "findings_filtered": len(filtered),
            "scopes": [str(s) for s in scopes],
            "findings": filtered,
        }, sys.stdout, indent=2)
        return 0

    # Text output
    print(f"# Code-redundancy scan")
    print(f"# scopes: {', '.join(str(s) for s in scopes)}")
    print(f"# files scanned: {files_scanned}")
    print(f"# findings: {len(filtered)} (severity >= {args.severity}, of {len(all_findings)} total)")
    print()
    if not filtered:
        print("No findings.")
        return 0

    by_sev: dict[str, list[dict]] = {}
    for f in filtered:
        by_sev.setdefault(f["severity"], []).append(f)
    for sev in ["HIGH", "MEDIUM", "LOW"]:
        if sev not in by_sev:
            continue
        print(f"## {sev} ({len(by_sev[sev])})")
        for f in by_sev[sev]:
            print(f"  {f['file']}:{f['line']}  [{f['why']}]  {f['match']}")
        print()
    print("Recommendation: review HIGH findings; if module is config-off + superseded, delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
