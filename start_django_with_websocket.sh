#!/bin/bash
#
# Note: websockets are not in used at present (9/2025).
# start_django_dual.sh is the script in use for standalone dev django.
#
# Require bash (fail fast if invoked under another shell)
if [ -z "${BASH_VERSION:-}" ]; then
	echo "This script must be run with bash. Try: bash $0 \"$@\"" >&2
	exit 1
fi
# Script to start Django with Daphne for WebSocket support

echo "Starting Django with Daphne for WebSocket support..."

# Navigate to swf-monitor source directory
cd /eic/u/wenauseic/github/swf-monitor/src

# Activate virtual environment
source /eic/u/wenauseic/github/swf-testbed/.venv/bin/activate

# Kill existing Django server if running
echo "Stopping existing Django server..."
pkill -f "manage.py runserver"
sleep 2

# Start with Daphne
echo "Starting Daphne ASGI server on port 8002..."
daphne -p 8002 -b 0.0.0.0 swf_monitor_project.asgi:application