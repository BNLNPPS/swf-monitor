#!/usr/bin/env bash
set -e

# This script runs the pytest tests for the swf-monitor project.
# It ensures that the tests are run using the project's virtual environment.

# The directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
VENV_PATH="$SCRIPT_DIR/venv"

# If a virtual environment is already active, use it
if [ -n "$VIRTUAL_ENV" ]; then
    echo "Using already active Python environment: $VIRTUAL_ENV"
# Otherwise, try to activate the local venv if it exists
elif [ -d "$VENV_PATH" ]; then
    echo "Activating Python environment from $VENV_PATH"
    source "$VENV_PATH/bin/activate"
else
    echo "Error: No active Python environment found and no local venv at $VENV_PATH."
    echo "This script must be run with an active Python environment."
    exit 1
fi

python -m pytest "$@"
