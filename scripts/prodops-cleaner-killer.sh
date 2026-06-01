#!/bin/bash
# Cron wrapper for the prod-ops cleaner-killer.
#
# cron does not source the deploy's EnvironmentFile, but the liveness ping needs
# the ACTIVEMQ_* settings to reach the broker. Source production.env, then exec
# the standalone reaper. All args are passed through (e.g. --prune-days 30).
#
# Install (Torre, root) — see docs/EPICPROD_OPS.md:
#   */2 * * * *  /opt/swf-monitor/current/scripts/prodops-cleaner-killer.sh
#   30 3 * * *   /opt/swf-monitor/current/scripts/prodops-cleaner-killer.sh --no-liveness --prune-days 30
set -eo pipefail   # no -u: sourcing the EnvironmentFile must not trip on bare refs

ENV_FILE="${EPICPROD_ENV_FILE:-/opt/swf-monitor/config/env/production.env}"
PYTHON="${EPICPROD_PYTHON:-/opt/swf-monitor/current/.venv/bin/python}"
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prodops-cleaner-killer.py"

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

exec "$PYTHON" "$SCRIPT" "$@"
