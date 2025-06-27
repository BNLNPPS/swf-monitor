"""
This conftest.py ensures that if pytest is run directly on a test file (not via the test script or project root),
it will gently fail with a clear message instructing the user to use the correct test runner for robustness.
"""
import os
import pytest

def pytest_configure(config):
    # Only check for direct invocation, not when run via the test script or project root
    # If DJANGO_SETTINGS_MODULE is not set, this is almost always a direct or improper invocation
    if not os.environ.get("DJANGO_SETTINGS_MODULE"):
        pytest.exit(
            "\n[SWF-MONITOR TEST SUITE]\n\n"
            "You are running pytest in a way that does not configure Django settings.\n"
            "For robust and reliable results, always run tests using:\n"
            "  ./run_tests.sh   (from this repo root)\n"
            "or\n"
            "  ./run_all_tests.sh   (from the umbrella/testbed repo root)\n\n"
            "Direct invocation of pytest on a test file or from the wrong directory is not supported.\n",
            returncode=4
        )
