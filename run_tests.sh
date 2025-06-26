#!/usr/bin/env bash

# This script runs the pytest tests for the swf-monitor project.
# It ensures that the tests are run using the project's virtual environment.

# The directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
VENV_PATH="$SCRIPT_DIR/venv"

if [ ! -d "$VENV_PATH" ]; then
    echo "Virtual environment not found at $VENV_PATH"
    echo "Please run the setup script to create it."
    exit 1
fi

# Activate the virtual environment and run pytest
source "$VENV_PATH/bin/activate"
python -m pytest "$@"
