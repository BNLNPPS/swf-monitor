#!/usr/bin/env bash
set -e

# This script runs the pytest tests for the swf-monitor project.
# It ensures that the tests are run using the project's virtual environment.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
VENV_PATH="$SCRIPT_DIR/.venv"
REQS_TXT="$SCRIPT_DIR/requirements.txt"
PYPROJECT="$SCRIPT_DIR/pyproject.toml"

# If a virtual environment is already active, use it
if [ -n "$VIRTUAL_ENV" ]; then
    echo "Using already active Python environment: $VIRTUAL_ENV"
# Otherwise, try to activate the local venv if it exists
elif [ -d "$VENV_PATH" ]; then
    echo "Activating Python environment from $VENV_PATH"
    source "$VENV_PATH/bin/activate"
# If no environment is active and no venv exists, create a new venv
else
    echo "No active Python environment found. Creating venv at $VENV_PATH."
    python3 -m venv "$VENV_PATH"
    source "$VENV_PATH/bin/activate"
    # Install dependencies if requirements.txt or pyproject.toml is present
    if [ -f "$REQS_TXT" ]; then
        echo "Installing dependencies from requirements.txt..."
        pip install -r "$REQS_TXT"
    elif [ -f "$PYPROJECT" ]; then
        echo "Installing dependencies from pyproject.toml..."
        pip install .[test]
    else
        echo "No requirements.txt or pyproject.toml found. Skipping dependency install."
    fi
fi

python -m pytest "$@"
