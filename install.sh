#!/bin/bash
# install.sh — bootstrap memory-wiki-graph-stack on a fresh machine.
# Idempotent: safe to re-run.
# Platform: macOS + Linux. Requires Python 3.10+.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_ROOT="${VAULT_ROOT:-$HOME/wiki}"

echo "==> memory-wiki-graph-stack install"
echo "    repo:        $REPO_DIR"
echo "    vault root:  $VAULT_ROOT"

# 0. Python preflight
if ! command -v python3 >/dev/null 2>&1; then
    echo "    ✗ python3 not found. Install Python 3.10+ and retry." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "    ✗ python3 is older than 3.10. Detected: $(python3 --version 2>&1)" >&2
    exit 1
fi
echo "    ✓ python3 $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"

# 1. Vault scaffold
mkdir -p "$VAULT_ROOT"/{atoms,hubs,projects,lessons,research,archive,meta}
echo "    ✓ vault structure ensured under $VAULT_ROOT"

# 2. Seed config (idempotent — won't overwrite existing)
if [ ! -f "$VAULT_ROOT/config.yaml" ] && [ -f "$REPO_DIR/config.yaml" ]; then
    cp "$REPO_DIR/config.yaml" "$VAULT_ROOT/config.yaml"
    echo "    ✓ seeded config.yaml — edit to taste"
fi

# 3. Symlink toolkit dirs into vault for runtime access
for dir in graph search lint promote lifecycle scripts; do
    if [ -d "$REPO_DIR/$dir" ]; then
        ln -sfn "$REPO_DIR/$dir" "$VAULT_ROOT/.$dir"
    fi
done
echo "    ✓ toolkit symlinks staged at $VAULT_ROOT/.{graph,search,lint,promote,lifecycle,scripts}"

# 4. Wiki scaffold (idempotent — only fills empty vault)
if [ -d "$REPO_DIR/wiki-scaffold" ] && [ -z "$(ls -A "$VAULT_ROOT/atoms" 2>/dev/null)" ]; then
    cp -R "$REPO_DIR/wiki-scaffold/." "$VAULT_ROOT/"
    echo "    ✓ scaffolded vault from wiki-scaffold/"
fi

# 5. Optional: install daily maintenance cron
if [ -d "$REPO_DIR/cron" ] && [ "${INSTALL_CRON:-0}" = "1" ]; then
    case "$(uname)" in
        Darwin)
            echo "==> macOS detected — see $REPO_DIR/cron/launchd/ for plist templates"
            ;;
        Linux)
            echo "==> Linux detected — see $REPO_DIR/cron/systemd/ for unit templates"
            ;;
    esac
fi

# 6. Next steps
cat <<EOF

==> install complete

Next steps:
  1. Edit vault config:       \$EDITOR $VAULT_ROOT/config.yaml
  2. Lint your wiki:          python3 $REPO_DIR/lint/wiki_lint.py --vault $VAULT_ROOT
  3. Build search index:      python3 $REPO_DIR/search/build_index.py --vault $VAULT_ROOT
  4. Run daily cron:          INSTALL_CRON=1 bash $REPO_DIR/install.sh
  5. Promote pattern:         python3 $REPO_DIR/promote/promote_lessons.py --vault $VAULT_ROOT

Docs: $REPO_DIR/README.md
EOF
