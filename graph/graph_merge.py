#!/usr/bin/env python3
"""
Unified Graph Merger: Extracts nodes/edges from memory + wiki markdown,
merges into existing graphify code graph. No LLM calls.

Sources:
  1. Existing graph: configured via config.yaml
  2. Memory: configured via config.yaml
  3. Wiki: configured via config.yaml

Node extraction:
  - Each markdown file = 1 node
  - ID from filename (slugified)
  - Metadata from frontmatter (title, type, tags, status)

Edge extraction:
  - Wikilinks [[target]] -> links_to edge
  - Tag co-occurrence (same tag on 2+ files) -> shares_tag edge
  - Source field in frontmatter -> derived_from edge
  - H2/H3 section references to known node IDs -> references edge
"""

import json
import os
import re
from pathlib import Path
from collections import defaultdict

# Paths
HOME = Path.home()
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lint", "scripts"))
from config_loader import load_config
_cfg = load_config()
GRAPH_IN = _cfg["graph_input"]
GRAPH_OUT = _cfg["graph_output"]
MEMORY_DIR = _cfg["memory_dir"]
WIKI_DIR = _cfg["wiki_dir"]

def slugify(name: str) -> str:
    """Convert filename to node ID."""
    name = Path(name).stem
    name = re.sub(r'[^a-z0-9_]', '_', name.lower())
    name = re.sub(r'_+', '_', name).strip('_')
    return name

def parse_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from markdown."""
    m = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
            key = key.strip()
            val = val.strip()
            # Parse list values [a, b, c]
            if val.startswith('[') and val.endswith(']'):
                val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(',') if v.strip()]
            fm[key] = val
    return fm

def extract_wikilinks(text: str) -> list[str]:
    """Extract [[wikilink]] targets."""
    return re.findall(r'\[\[([^\]]+)\]\]', text)

def extract_md_refs(text: str, known_ids: set) -> list[str]:
    """Find references to known node IDs in text body."""
    refs = []
    for nid in known_ids:
        # Match the slug as a word boundary pattern
        if len(nid) > 5 and re.search(r'\b' + re.escape(nid.replace('_', '[_ -]')) + r'\b', text, re.IGNORECASE):
            refs.append(nid)
    return refs

def scan_markdown_dir(dirpath: Path, source_prefix: str) -> tuple[list[dict], list[dict]]:
    """Scan a directory for markdown files, return (nodes, edges)."""
    nodes = []
    edges = []

    for md_file in sorted(dirpath.rglob("*.md")):
        # Skip index files and hidden
        if md_file.name.startswith('.') or md_file.name == '_index.md':
            continue
        if md_file.name == 'MEMORY.md' or md_file.name == 'CLAUDE.md':
            continue

        try:
            text = md_file.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue

        fm = parse_frontmatter(text)
        rel_path = str(md_file.relative_to(HOME))
        node_id = f"{source_prefix}_{slugify(md_file.name)}"

        # Determine file_type from frontmatter or directory
        file_type = fm.get('type', 'doc')
        if isinstance(file_type, list):
            file_type = file_type[0] if file_type else 'doc'

        node = {
            "label": fm.get('title', md_file.stem),
            "file_type": file_type,
            "source_file": rel_path,
            "source_location": "L1",
            "id": node_id,
            "community": -1,  # Will be assigned later
        }
        if fm.get('tags'):
            node['tags'] = fm['tags'] if isinstance(fm['tags'], list) else [fm['tags']]
        if fm.get('status'):
            node['status'] = fm['status']

        nodes.append(node)

        # Wikilink edges
        for target in extract_wikilinks(text):
            target_id_wiki = slugify(target)
            edges.append({
                "relation": "links_to",
                "confidence": "EXTRACTED",
                "source_file": rel_path,
                "source_location": "L1",
                "weight": 0.8,
                "_src": node_id,
                "_tgt_hint": target_id_wiki,  # Resolved later
                "source": node_id,
                "target": None,  # Resolved later
                "confidence_score": 0.8
            })

        # Source field edges
        if fm.get('source'):
            sources = fm['source'] if isinstance(fm['source'], list) else [fm['source']]
            for src in sources:
                src_id = slugify(src)
                edges.append({
                    "relation": "derived_from",
                    "confidence": "EXTRACTED",
                    "source_file": rel_path,
                    "source_location": "L1",
                    "weight": 0.9,
                    "_src": node_id,
                    "_tgt_hint": src_id,
                    "source": node_id,
                    "target": None,
                    "confidence_score": 0.9
                })

    return nodes, edges

def build_tag_edges(nodes: list[dict]) -> list[dict]:
    """Create edges between nodes sharing tags. Only top tags to avoid explosion."""
    tag_map = defaultdict(list)
    for n in nodes:
        for tag in n.get('tags', []):
            tag_map[tag].append(n['id'])

    edges = []
    for tag, nids in tag_map.items():
        if len(nids) < 2 or len(nids) > 20:  # Skip singleton or overly common tags
            continue
        # Connect pairs (limit to avoid O(n^2) explosion)
        for i in range(min(len(nids), 10)):
            for j in range(i + 1, min(len(nids), 10)):
                edges.append({
                    "relation": "shares_tag",
                    "confidence": "INFERRED",
                    "source_file": f"tag:{tag}",
                    "source_location": "L1",
                    "weight": 0.5,
                    "source": nids[i],
                    "target": nids[j],
                    "confidence_score": 0.5,
                    "_src": nids[i],
                    "_tgt": nids[j],
                })
    return edges

def resolve_edges(edges: list[dict], id_lookup: dict) -> list[dict]:
    """Resolve _tgt_hint to actual node IDs."""
    resolved = []
    unresolved = 0
    for e in edges:
        if e.get('target') is not None:
            resolved.append(e)
            continue
        hint = e.get('_tgt_hint', '')
        # Try exact match, then prefix matches
        target = None
        if hint in id_lookup:
            target = hint
        else:
            # Try with common prefixes
            for prefix in ['mem_', 'wiki_', '']:
                candidate = f"{prefix}{hint}" if prefix else hint
                if candidate in id_lookup:
                    target = candidate
                    break
            if not target:
                # Fuzzy: find any ID ending with the hint
                for nid in id_lookup:
                    if nid.endswith(f"_{hint}") or nid == hint:
                        target = nid
                        break

        if target:
            e['target'] = target
            e['_tgt'] = target
            if '_tgt_hint' in e:
                del e['_tgt_hint']
            resolved.append(e)
        else:
            unresolved += 1

    if unresolved:
        print(f"  {unresolved} edges unresolved (target not found)")
    return resolved

def assign_communities(nodes: list[dict], existing_max_community: int) -> None:
    """Assign community IDs to new nodes based on type/directory."""
    type_community = {}
    next_community = existing_max_community + 1

    for n in nodes:
        if n['community'] != -1:
            continue
        key = n.get('file_type', 'doc')
        # Also use directory for wiki
        src = n.get('source_file', '')
        if 'wiki/' in src:
            parts = src.split('wiki/')[-1].split('/')
            if len(parts) > 1:
                key = f"wiki_{parts[0]}"

        if key not in type_community:
            type_community[key] = next_community
            next_community += 1
        n['community'] = type_community[key]

    print(f"  Assigned {len(type_community)} new communities ({existing_max_community + 1} to {next_community - 1})")

def main():
    print("=== Unified Graph Merger ===\n")

    # 1. Load existing graph
    print("Loading existing graph...")
    with open(GRAPH_IN) as f:
        graph = json.load(f)

    existing_nodes = {n['id']: n for n in graph['nodes']}
    existing_links = graph['links']
    max_community = max((n.get('community', 0) for n in graph['nodes']), default=0)

    print(f"  Existing: {len(existing_nodes)} nodes, {len(existing_links)} links, {len(graph.get('hyperedges', []))} hyperedges")

    # 2. Scan memory
    print("\nScanning memory files...")
    mem_nodes, mem_edges = scan_markdown_dir(MEMORY_DIR, "mem")
    print(f"  Found: {len(mem_nodes)} nodes, {len(mem_edges)} edges")

    # 3. Scan wiki
    print("\nScanning wiki files...")
    wiki_nodes, wiki_edges = scan_markdown_dir(WIKI_DIR, "wiki")
    print(f"  Found: {len(wiki_nodes)} nodes, {len(wiki_edges)} edges")

    # 4. Merge nodes (skip duplicates)
    new_nodes = []
    for n in mem_nodes + wiki_nodes:
        if n['id'] not in existing_nodes:
            existing_nodes[n['id']] = n
            new_nodes.append(n)

    print(f"\n{len(new_nodes)} new nodes (after dedup)")

    # 5. Build tag edges
    print("\nBuilding tag edges...")
    tag_edges = build_tag_edges(list(existing_nodes.values()))
    print(f"  {len(tag_edges)} tag co-occurrence edges")

    # 6. Resolve edge targets
    print("\nResolving edges...")
    all_new_edges = mem_edges + wiki_edges
    resolved_edges = resolve_edges(all_new_edges, existing_nodes)
    print(f"  {len(resolved_edges)} link/source edges resolved")

    # 7. Assign communities to new nodes
    print("\nAssigning communities...")
    assign_communities(new_nodes, max_community)

    # 8. Build output
    all_nodes = list(existing_nodes.values())
    all_links = existing_links + resolved_edges + tag_edges

    # Deduplicate links
    seen_links = set()
    deduped_links = []
    for link in all_links:
        key = (link.get('source', link.get('_src')), link.get('target', link.get('_tgt')), link.get('relation'))
        if key not in seen_links and key[0] and key[1]:
            seen_links.add(key)
            deduped_links.append(link)

    output = {
        "directed": graph.get("directed", False),
        "multigraph": graph.get("multigraph", False),
        "graph": {
            "hyperedges": graph.get("graph", {}).get("hyperedges", [])
        },
        "nodes": all_nodes,
        "links": deduped_links
    }

    # 9. Write
    print(f"\nWriting unified graph to {GRAPH_OUT}...")
    with open(GRAPH_OUT, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    size_mb = os.path.getsize(GRAPH_OUT) / 1024 / 1024

    print(f"\n=== Done ===")
    print(f"  Nodes: {len(existing_nodes) - len(new_nodes)} existing + {len(new_nodes)} new = {len(all_nodes)}")
    print(f"  Links: {len(existing_links)} existing + {len(resolved_edges)} link/source + {len(tag_edges)} tag = {len(deduped_links)} (deduped)")
    print(f"  File: {size_mb:.1f} MB")

if __name__ == "__main__":
    main()
