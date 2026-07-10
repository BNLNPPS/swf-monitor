#!/usr/bin/env bash
#
# Lightweight swf-monitor deploy for UI and MCP development.
#
# This updates the active release in place. It is intentionally not a full
# production release deploy: no venv copy, dependency install, migrations,
# Apache config sync, current-symlink flip, ops-agent restart, or bot restart.

set -euo pipefail

DEPLOY_ROOT="${SWF_DEPLOY_ROOT:-/opt/swf-monitor}"
CURRENT_DIR="$DEPLOY_ROOT/current"
SHARED_STATIC="$DEPLOY_ROOT/shared/static"
MCP_SERVICE="${SWF_MCP_SERVICE:-swf-monitor-mcp-asgi.service}"

DO_UI=false
DO_MCP=false
DO_STATIC=false
DRY_RUN=false

usage() {
    cat <<'EOF'
Usage: deploy-lightweight-ui-mcp.sh [--ui] [--mcp] [--static] [--dry-run]

Fast in-place deploy for swf-monitor UI/template/view changes and MCP tool
changes only. Use the full deploy for migrations, requirements/venv changes,
Apache config, systemd units, ops-agent code, bot code, or release changes.
This follows the normal dev-area sync workflow: current checkout to active
/opt/swf-monitor/current release, without creating a release directory or
moving the current symlink.

Options:
  --ui       Sync UI/web code and templates; recycle mod_wsgi by touching wsgi.py
  --mcp      Sync MCP code/helpers; restart swf-monitor-mcp-asgi.service
  --static   Also collect and publish static assets
  --dry-run  Show rsync changes and planned process actions without applying
  --help     Show this help

Examples:
  sudo ./deploy-lightweight-ui-mcp.sh --ui
  sudo ./deploy-lightweight-ui-mcp.sh --mcp
  sudo ./deploy-lightweight-ui-mcp.sh --ui --mcp
  sudo ./deploy-lightweight-ui-mcp.sh --ui --static
EOF
}

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ui)
            DO_UI=true
            ;;
        --mcp)
            DO_MCP=true
            ;;
        --static)
            DO_STATIC=true
            ;;
        --dry-run)
            DRY_RUN=true
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$DO_UI" != true && "$DO_MCP" != true ]]; then
    echo "ERROR: choose --ui and/or --mcp; --static is an add-on" >&2
    usage >&2
    exit 2
fi

if [[ "$DRY_RUN" != true && "$EUID" -ne 0 ]]; then
    echo "ERROR: run with sudo so the active release and systemd service can be updated" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
SRC_DIR="$REPO_ROOT/src"
TARGET_SRC="$CURRENT_DIR/src"

if [[ ! -d "$SRC_DIR/monitor_app" || ! -f "$SRC_DIR/manage.py" ]]; then
    echo "ERROR: source tree not found at $SRC_DIR" >&2
    exit 1
fi

if [[ ! -d "$TARGET_SRC" || ! -f "$TARGET_SRC/swf_monitor_project/wsgi.py" ]]; then
    echo "ERROR: active deployment source tree not found at $TARGET_SRC" >&2
    exit 1
fi

if [[ -d "$REPO_ROOT/.git" ]]; then
    OUT_OF_SCOPE=$(git -C "$REPO_ROOT" status --short --untracked-files=all \
        | awk '{print $2}' \
        | grep -E '(^requirements|^pyproject\.toml$|^apache-swf-monitor\.conf$|\.service$|^config/apache/|/migrations/|^agents/|/panda/|/testbed_bot/)' \
        || true)
    if [[ -n "$OUT_OF_SCOPE" ]]; then
        echo "ERROR: working tree contains changes outside lightweight UI/MCP scope:" >&2
        echo "$OUT_OF_SCOPE" >&2
        echo "Use the full deploy for these changes, or commit/stash unrelated work before running this script." >&2
        exit 1
    fi
fi

RSYNC_ARGS=(-a --delete --exclude '__pycache__/' --exclude '*.pyc')
if [[ "$DRY_RUN" == true ]]; then
    RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

MONITOR_EXCLUDES=(
    --exclude 'migrations/'
    --exclude 'management/'
    --exclude 'panda/'
    --exclude 'testbed_bot/'
    --exclude 'tests/'
)
PCS_EXCLUDES=(
    --exclude 'migrations/'
    --exclude 'management/'
    --exclude 'tests/'
)
AI_EXCLUDES=(
    --exclude 'migrations/'
    --exclude 'tests/'
)

if [[ "$DO_MCP" != true ]]; then
    MONITOR_EXCLUDES+=(--exclude 'mcp/')
fi

if [[ "$DO_UI" != true ]]; then
    MONITOR_EXCLUDES+=(--exclude 'templates/')
    PCS_EXCLUDES+=(--exclude 'templates/')
    AI_EXCLUDES+=(--exclude 'templates/')
fi

if [[ "$DO_STATIC" != true ]]; then
    MONITOR_EXCLUDES+=(--exclude 'static/')
    PCS_EXCLUDES+=(--exclude 'static/')
fi

log "Lightweight deploy source: $REPO_ROOT"
log "Active target: $CURRENT_DIR"

log "Syncing monitor_app lightweight paths..."
rsync "${RSYNC_ARGS[@]}" "${MONITOR_EXCLUDES[@]}" "$SRC_DIR/monitor_app/" "$TARGET_SRC/monitor_app/"

# pcs ships from swf-epicprod as an installed package; lightweight-sync the
# swf-epicprod dev tree onto the deployed venv's installed copy (migrations
# and management commands still ride the full deploy only).
EPICPROD_ROOT="/data/wenauseic/github/swf-epicprod"
TARGET_PCS=$("$CURRENT_DIR/.venv/bin/python" -c "import pcs, os; print(os.path.dirname(pcs.__file__))")
case "$TARGET_PCS" in
    "$CURRENT_DIR"/.venv/*) ;;
    *)
        echo "ERROR: deployed pcs resolves outside the deployed venv: $TARGET_PCS" >&2
        echo "Run the full deploy to freeze swf-epicprod, then retry." >&2
        exit 1
        ;;
esac
log "Syncing pcs lightweight paths (swf-epicprod -> deployed venv)..."
rsync "${RSYNC_ARGS[@]}" "${PCS_EXCLUDES[@]}" "$EPICPROD_ROOT/pcs/" "$TARGET_PCS/"

log "Syncing ai lightweight paths..."
rsync "${RSYNC_ARGS[@]}" "${AI_EXCLUDES[@]}" "$SRC_DIR/ai/" "$TARGET_SRC/ai/"

if [[ "$DO_UI" == true ]]; then
    log "Syncing project-level templates and URL routing..."
    rsync "${RSYNC_ARGS[@]}" "$SRC_DIR/templates/" "$TARGET_SRC/templates/"
    rsync "${RSYNC_ARGS[@]}" "$SRC_DIR/swf_monitor_project/urls.py" "$TARGET_SRC/swf_monitor_project/urls.py"
fi

if [[ "$DO_MCP" == true ]]; then
    log "Syncing MCP ASGI entrypoint..."
    rsync "${RSYNC_ARGS[@]}" "$SRC_DIR/swf_monitor_project/mcp_asgi.py" "$TARGET_SRC/swf_monitor_project/mcp_asgi.py"
fi

if [[ "$DO_STATIC" == true ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        log "Would collect static assets and sync them to $SHARED_STATIC"
    else
        log "Collecting static assets..."
        cd "$TARGET_SRC"
        "$CURRENT_DIR/.venv/bin/python" manage.py collectstatic --noinput --settings=swf_monitor_project.settings
        log "Syncing static assets to shared Apache static directory..."
        rsync -a --delete "$TARGET_SRC/staticfiles/" "$SHARED_STATIC/"
    fi
fi

if [[ "$DO_UI" == true ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        log "Would touch $TARGET_SRC/swf_monitor_project/wsgi.py to recycle mod_wsgi"
    else
        log "Recycling mod_wsgi app by touching wsgi.py..."
        touch "$TARGET_SRC/swf_monitor_project/wsgi.py"
    fi
fi

if [[ "$DO_MCP" == true ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        log "Would restart $MCP_SERVICE"
    else
        log "Restarting MCP ASGI worker ($MCP_SERVICE)..."
        systemctl restart "$MCP_SERVICE"
    fi
fi

log "Lightweight UI/MCP deploy complete"
