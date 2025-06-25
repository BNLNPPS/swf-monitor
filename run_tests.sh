#!/usr/bin/env bash
set -e

# This script runs tests for swf-monitor.
# It assumes that the unified project environment has already been set up and
# activated by the master setup.sh script in the swf-testbed directory.
# For running all project tests, use the master script in swf-testbed/run_tests.sh

# Attempt to activate the environment if it's not already active.
if [ -z "$VIRTUAL_ENV" ]; then
    echo "No active virtual environment. Attempting to source setup_env.sh..."
    # Assuming this script is in swf-monitor and setup_env.sh is in the sibling swf-testbed dir
    SETUP_ENV_PATH="$(dirname "$0")/../swf-testbed/setup_env.sh"
    if [ -f "$SETUP_ENV_PATH" ]; then
        source "$SETUP_ENV_PATH"
    else
        echo "ERROR: Could not find setup_env.sh at $SETUP_ENV_PATH"
        echo "Please run the master setup script from the swf-testbed directory: ./setup.sh"
        exit 1
    fi
fi

echo "--- Running tests for swf-monitor ---"
pytest
