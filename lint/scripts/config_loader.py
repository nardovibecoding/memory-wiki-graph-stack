"""Shared config loader + file write utilities for llm-wiki-stack scripts."""

import json
import os
import re
from datetime import date
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def load_config():
    """Load config from env var, ~/.config, or defaults."""
    config_path = os.environ.get("LLM_WIKI_CONFIG")
    if not config_path:
        config_path = Path.home() / ".config" / "llm-wiki-stack" / "config.yaml"

    config = {}
    if HAS_YAML and Path(config_path).exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

    home = Path.home()

    def expand(p):
        if not p:
            return None
        return Path(os.path.expanduser(str(p)))

    return {
        "wiki_dir": expand(config.get("wiki_dir", "~/wiki")),
        "memory_dir": expand(config.get("memory_dir",
            home / ".claude" / "projects" / "default" / "memory")),
        "memo_dir": expand(config.get("memo_dir", "~/memo")),
        "graph_input": expand(config.get("graph", {}).get("input", "~/graph/graph.json")),
        "graph_output": expand(config.get("graph", {}).get("output", "~/graph/graph_unified.json")),
        "graph_merge_script": Path(__file__).parent.parent.parent / "graph" / "graph_merge.py",
        "lint": config.get("lint", {}),
        "lifecycle": config.get("lifecycle", {}),
    }


# ── Write utilities ───────────────────────────────────────────────────────────

def write_md_file(path: "Path | str", content: str) -> None:
    """Write a markdown file, ensuring frontmatter updated: is stamped to today.

    Works for both wiki (NardoWorld) and memory files.
    If frontmatter has an updated: field, replaces it. Otherwise inserts after title:.
    Creates parent dirs if needed.
    """
    path = Path(path)
    today = date.today().isoformat()
    # Stamp updated: if frontmatter present
    if content.startswith("---"):
        if re.search(r"^updated:.*$", content, re.MULTILINE):
            content = re.sub(r"^updated:.*$", f"updated: {today}", content, flags=re.MULTILINE)
        else:
            # Insert after first --- block's last field (before closing ---)
            content = re.sub(r"(\n---\n)", f"\nupdated: {today}\\1", content, count=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def write_graph(path: "Path | str", data: dict) -> None:
    """Write graph JSON, stamping data['meta']['updated_at'] to today's ISO date."""
    path = Path(path)
    if "meta" not in data:
        data["meta"] = {}
    data["meta"]["updated_at"] = date.today().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
