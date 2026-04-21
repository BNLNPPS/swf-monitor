#!/bin/bash
# Nightly update of PanDA bot MCP servers.
# For each server repo: git pull, if new commits found run rebuild, track changes.
# If any server updated, restart the bot so it picks up the new code.
# Run via cron: 30 3 * * * /data/wenauseic/github/swf-monitor/scripts/update_mcp_servers.sh

set -uo pipefail

GITHUB_DIR="/data/wenauseic/github"
NODE_PATH="/eic/u/wenauseic/.nvm/versions/node/v22.17.0/bin"
VENV_BIN="$GITHUB_DIR/swf-testbed/.venv/bin"
LOG="/tmp/mcp_servers_update.log"
BOT_SERVICE="swf-panda-bot"

exec > "$LOG" 2>&1
echo "=== MCP server update $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

CHANGED=0

pull_and_check() {
    local name="$1"
    local dir="$2"

    if [ ! -d "$dir/.git" ]; then
        echo "--- $name: $dir not found, skipping"
        return 1
    fi

    echo "--- $name"
    local before
    before=$(git -C "$dir" rev-parse HEAD)
    # Stash any local changes (e.g. package-lock.json from npm install)
    local stashed=0
    if ! git -C "$dir" diff --quiet 2>/dev/null; then
        git -C "$dir" stash -q 2>&1 && stashed=1
    fi
    git -C "$dir" pull --ff-only 2>&1 || { echo "  WARN: pull failed"; [ "$stashed" -eq 1 ] && git -C "$dir" stash pop -q 2>/dev/null; return 1; }
    [ "$stashed" -eq 1 ] && git -C "$dir" stash pop -q 2>/dev/null || true
    local after
    after=$(git -C "$dir" rev-parse HEAD)

    if [ "$before" = "$after" ]; then
        echo "  no changes"
        return 1
    else
        echo "  updated: ${before:0:8} → ${after:0:8}"
        return 0
    fi
}

# --- xrootd (Node.js) ---
if pull_and_check "xrootd" "$GITHUB_DIR/xrootd-mcp-server"; then
    echo "  rebuilding..."
    export PATH="$NODE_PATH:$PATH"
    (cd "$GITHUB_DIR/xrootd-mcp-server" && npm install && npm run build) 2>&1
    if [ $? -eq 0 ]; then
        echo "  rebuild OK"
        CHANGED=1
    else
        echo "  ERROR: rebuild failed"
    fi
fi

# --- github (Go) ---
if pull_and_check "github" "$GITHUB_DIR/github-mcp-server"; then
    echo "  rebuilding..."
    (cd "$GITHUB_DIR/github-mcp-server" && PATH=$PATH:/usr/local/go/bin go build -o github-mcp-server ./cmd/github-mcp-server) 2>&1
    if [ $? -eq 0 ]; then
        echo "  rebuild OK"
        CHANGED=1
    else
        echo "  ERROR: rebuild failed"
    fi
fi

# --- zenodo (Node.js) ---
if pull_and_check "zenodo" "$GITHUB_DIR/zenodo-mcp-server"; then
    echo "  rebuilding..."
    export PATH="$NODE_PATH:$PATH"
    (cd "$GITHUB_DIR/zenodo-mcp-server" && npm install && npm run build) 2>&1
    if [ $? -eq 0 ]; then
        echo "  rebuild OK"
        CHANGED=1
    else
        echo "  ERROR: rebuild failed"
    fi
fi

# --- lxr (Python, no build needed) ---
if pull_and_check "lxr" "$GITHUB_DIR/lxr-mcp-server"; then
    echo "  no build step needed"
    CHANGED=1
fi

# --- uproot (Python, pip install) ---
if pull_and_check "uproot" "$GITHUB_DIR/uproot-mcp-server"; then
    echo "  rebuilding..."
    ("$VENV_BIN/pip" install -e "$GITHUB_DIR/uproot-mcp-server[xrootd]") 2>&1
    if [ $? -eq 0 ]; then
        echo "  rebuild OK"
        CHANGED=1
    else
        echo "  ERROR: rebuild failed"
    fi
fi

# --- rucio-eic (Python, pip install) ---
if pull_and_check "rucio-eic" "$GITHUB_DIR/rucio-eic-mcp-server"; then
    echo "  rebuilding..."
    ("$VENV_BIN/pip" install -e "$GITHUB_DIR/rucio-eic-mcp-server") 2>&1
    if [ $? -eq 0 ]; then
        echo "  rebuild OK"
        CHANGED=1
    else
        echo "  ERROR: rebuild failed"
    fi
fi

# --- Restart bot if anything changed ---
if [ "$CHANGED" -eq 1 ]; then
    echo ""
    echo "=== Restarting $BOT_SERVICE ==="
    sudo systemctl restart "$BOT_SERVICE" 2>&1
    sleep 5
    if systemctl is-active --quiet "$BOT_SERVICE"; then
        echo "  bot restarted OK"
    else
        echo "  ERROR: bot failed to start"
        sudo journalctl -u "$BOT_SERVICE" --since "30 sec ago" --no-pager 2>&1 | tail -10
    fi
else
    echo ""
    echo "=== No changes — bot not restarted ==="
fi

echo "=== done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
