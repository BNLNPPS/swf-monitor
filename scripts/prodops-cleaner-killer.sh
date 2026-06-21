#!/bin/bash
# Cron wrapper for the prod-ops cleaner-killer.
#
# cron does not source the deploy's EnvironmentFile, but the liveness ping needs
# the ACTIVEMQ_* settings to reach the broker. Load production.env, then exec
# the standalone reaper. All args are passed through (e.g. --prune-days 30).
#
# Install (Torre, root) — see docs/EPICPROD_OPS.md:
#   */2 * * * *  /opt/swf-monitor/current/scripts/prodops-cleaner-killer.sh
#   30 3 * * *   /opt/swf-monitor/current/scripts/prodops-cleaner-killer.sh --no-liveness --prune-days 30
set -eo pipefail

ENV_FILE="${EPICPROD_ENV_FILE:-/opt/swf-monitor/config/env/production.env}"
PYTHON="${EPICPROD_PYTHON:-/opt/swf-monitor/current/.venv/bin/python}"
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prodops-cleaner-killer.py"

# production.env is a Django/decouple env file, NOT a bash script: values may
# contain $ & ( ) etc. unquoted (e.g. SECRET_KEY), so `source` chokes on them.
# Parse it the way systemd's EnvironmentFile does — literal KEY=VALUE, no shell
# evaluation — and export each for the python reaper.
if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key val || [[ -n "$key" ]]; do
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue   # skip comments/blanks
        val="${val%\"}"; val="${val#\"}"                       # strip optional "quotes"
        export "$key=$val"
    done < "$ENV_FILE"
fi

exec "$PYTHON" "$SCRIPT" "$@"
