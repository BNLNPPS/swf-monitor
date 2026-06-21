#!/usr/bin/env bash
# swf-alarms install — creates venv, installs package, prepares log dir.
# Alarm state lives in swf-monitor's Postgres; schema is owned by
# swf-monitor's Django migrations and applied by its deploy script, not here.
#
# Usage:  bash deploy/install.sh
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY_ROOT="${SWF_MONITOR_DEPLOY_ROOT:-/opt/swf-monitor}"
VENV="${SWF_ALARMS_VENV:-$DEPLOY_ROOT/shared/alarms-venv}"
CONFIG_DIR="${SWF_ALARMS_CONFIG_DIR:-$DEPLOY_ROOT/config/alarms}"
CONFIG="${SWF_ALARMS_CONFIG:-$CONFIG_DIR/config.toml}"
LOG_DIR="${SWF_ALARMS_LOG_DIR:-$DEPLOY_ROOT/shared/logs/swf-alarms}"

echo "[swf-alarms install] repo dir:  $HERE"

if [ ! -d "$VENV" ]; then
    echo "[swf-alarms install] creating venv"
    python3 -m venv "$VENV"
fi
mkdir -p "$CONFIG_DIR"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -e "$HERE" >/dev/null

if [ ! -f "$CONFIG" ]; then
    echo "[swf-alarms install] no config.toml yet — copying config.toml.example"
    cp "$HERE/config.toml.example" "$CONFIG"
    echo "[swf-alarms install] edit $CONFIG before first run"
fi

mkdir -p "$LOG_DIR"
chmod 775 "$LOG_DIR"

echo "[swf-alarms install] done."
echo "  venv:    $VENV"
echo "  config:  $CONFIG"
echo "  logs:    $LOG_DIR/"
echo
echo "Schema: swf-monitor's migrations own entry / entry_context / entry_version."
echo "  Ensure they are applied by the normal swf-monitor deploy/migrate path."
echo
echo "Next:"
echo "  1. edit $CONFIG  (thresholds, recipients)"
echo "  2. one-shot test: $VENV/bin/swf-alarms-run --config $CONFIG --dry-run -v"
echo "  3. install cron:  see deploy/crontab.example"
