"""Shared config loader for llm-wiki-stack scripts."""

import os
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
