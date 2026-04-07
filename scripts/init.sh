#!/bin/bash
# Initialize a new wiki directory from the scaffold
set -euo pipefail

WIKI_DIR="${1:-$HOME/wiki}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCAFFOLD="$SCRIPT_DIR/../wiki-scaffold"

if [[ -d "$WIKI_DIR" && "$(ls -A "$WIKI_DIR" 2>/dev/null)" ]]; then
    echo "Error: $WIKI_DIR already exists and is not empty"
    exit 1
fi

echo "Initializing wiki at $WIKI_DIR..."
mkdir -p "$WIKI_DIR"
cp -r "$SCAFFOLD"/* "$WIKI_DIR"/
cp -r "$SCAFFOLD"/.* "$WIKI_DIR"/ 2>/dev/null || true

# Create config directory
CONFIG_DIR="$HOME/.config/llm-wiki-stack"
mkdir -p "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    cp "$SCRIPT_DIR/../config.yaml" "$CONFIG_DIR/config.yaml"
    # Update wiki_dir in config
    sed -i '' "s|wiki_dir:.*|wiki_dir: $WIKI_DIR|" "$CONFIG_DIR/config.yaml" 2>/dev/null || \
    sed -i "s|wiki_dir:.*|wiki_dir: $WIKI_DIR|" "$CONFIG_DIR/config.yaml"
    echo "Config written to $CONFIG_DIR/config.yaml"
fi

echo "Wiki initialized at $WIKI_DIR"
echo ""
echo "Next steps:"
echo "  1. Edit ~/.config/llm-wiki-stack/config.yaml with your paths"
echo "  2. cd $(dirname "$SCRIPT_DIR") && cd search && npm install"
echo "  3. python3 lint/scripts/wiki_lint.py --fix"
echo "  4. node search/build-index.mjs"
