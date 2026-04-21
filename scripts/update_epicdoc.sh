#!/bin/bash
# Nightly update of epicdoc: git pull all doc source repos, then re-ingest.
# Incremental mode — only re-embeds changed files.
# Run via cron: 0 3 * * * /data/wenauseic/github/swf-monitor/scripts/update_epicdoc.sh

set -euo pipefail

GITHUB_DIR="/data/wenauseic/github"
SCRIPT_DIR="$(dirname "$0")"
LOG="/tmp/epicdoc_update.log"

exec > "$LOG" 2>&1
echo "=== epicdoc update $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Repos referenced in ingest_docs.yaml
REPOS=(
    swf-testbed
    swf-monitor
    swf-common-lib
    bamboo-mcp
    panda-bigmon-core
    eic.github.io
    epic-prod
    tutorial-file-access
    EICrecon
    containers
    epic
    afterburner
    eic-shell
)

# Git pull each repo
for repo in "${REPOS[@]}"; do
    dir="$GITHUB_DIR/$repo"
    if [ -d "$dir/.git" ]; then
        echo "--- $repo"
        git -C "$dir" pull --ff-only 2>&1 || echo "  WARN: pull failed for $repo"
    else
        echo "--- $repo: not found, skipping"
    fi
done

# Activate venv and run incremental ingest
echo "--- ingesting docs"
source "$GITHUB_DIR/swf-testbed/.venv/bin/activate"
source ~/.env
python "$SCRIPT_DIR/ingest_docs.py" --config "$SCRIPT_DIR/ingest_docs.yaml"

echo "=== done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
