#!/bin/bash
# Cron wrapper for the campaign-assessment trigger
# (swf-epicprod docs/EPICPROD_ASSESSMENTS_V1.md). Loads production.env —
# the single source the agent and web tier read — and runs the deployed
# trigger module. All args pass through (--kind nightly|weekly, ...).
#
# Install (wenauseic), after the 02:15 catalog_sync chain:
#   45 3 * * *  /opt/swf-monitor/current/scripts/assessment-trigger-cron.sh --kind nightly
#    0 6 * * 1  /opt/swf-monitor/current/scripts/assessment-trigger-cron.sh --kind weekly
set -eo pipefail

ENV_FILE="${EPICPROD_ENV_FILE:-/opt/swf-monitor/config/env/production.env}"
PYTHON="${EPICPROD_PYTHON:-/opt/swf-monitor/current/.venv/bin/python}"

# production.env is a Django/decouple env file, NOT a bash script: values may
# contain $ & ( ) etc. unquoted (e.g. SECRET_KEY), so `source` chokes on them.
# Parse it the way systemd's EnvironmentFile does — literal KEY=VALUE, no
# shell evaluation — and export each for the trigger.
if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key val || [[ -n "$key" ]]; do
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue   # skip comments/blanks
        val="${val%\"}"; val="${val#\"}"                       # strip optional "quotes"
        export "$key=$val"
    done < "$ENV_FILE"
fi

exec "$PYTHON" -m swf_epicprod.assessment.trigger "$@"
