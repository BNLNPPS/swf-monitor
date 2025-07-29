#!/usr/bin/env python3
"""Run tests for swf-monitor repository."""
import os
import sys
import subprocess
from pathlib import Path

def print_separator():
    """Print the 100-character separator line."""
    print("\n" + "*" * 100 + "\n")

def main():
    """Main function."""
    print_separator()
    
    # Get the directory of this script
    script_dir = Path(__file__).resolve().parent
    
    # Check if we're in a virtual environment or use testbed's venv
    if "VIRTUAL_ENV" not in os.environ:
        # Look for swf-testbed's virtual environment
        swf_parent_dir = os.environ.get("SWF_PARENT_DIR", script_dir.parent)
        testbed_venv = Path(swf_parent_dir) / "swf-testbed" / ".venv"
        
        if testbed_venv.exists():
            print("🔧 Using swf-testbed virtual environment...")
            venv_python = testbed_venv / "bin" / "python"
            if venv_python.exists():
                os.environ["VIRTUAL_ENV"] = str(testbed_venv)
                os.environ["PATH"] = f"{testbed_venv}/bin:{os.environ['PATH']}"
                sys.executable = str(venv_python)
        else:
            print("❌ Error: No Python virtual environment found")
            print("   Please activate the swf-testbed virtual environment first:")
            print("   cd swf-testbed && source .venv/bin/activate")
            return 1
    
    print(f"Using Python environment: {os.environ.get('VIRTUAL_ENV', 'system')}")
    print("Running pytest for swf-monitor...")
    
    # Run pytest using the current Python interpreter
    result = subprocess.run([sys.executable, "-m", "pytest"], cwd=script_dir)
    return result.returncode

if __name__ == "__main__":
    sys.exit(main())