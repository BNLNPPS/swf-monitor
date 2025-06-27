#!/usr/bin/env python3
"""
Script to load fake AppLog data into the swf-monitor database for UI/demo purposes.
Adds a few entries for each app, instance, and log level to exercise the log summary and drill-down views.

Usage:
    python scripts/load_fake_logs.py

Notes:
- Timestamps are timezone-aware (Django's timezone.now()).
- Run only in a virtual environment. Script will exit if not.
- To avoid accidental duplicate logs, you can clear AppLog table before running (see code comments).
"""
import os
import sys
import django
import random
from datetime import timedelta
from django.utils import timezone

# Robustness: Check for active virtual environment
if sys.prefix == sys.base_prefix:
    print("\n[ERROR] This script should be run inside your project's Python virtual environment.")
    print("Activate your venv with:")
    print("  source ../swf-testbed/.venv/bin/activate   # (bash/zsh, macOS/Linux)")
    print("  ..\\swf-testbed\\.venv\\Scripts\\activate   # (Windows cmd)")
    print("Then run:\n  python scripts/load_fake_logs.py\n")
    sys.exit(1)

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
django.setup()

from monitor_app.models import AppLog

APPS = ['app1', 'app2', 'app3']
INSTANCES = ['inst1', 'inst2', 'inst3']
LEVELS = [
    (10, 'DEBUG'),
    (20, 'INFO'),
    (30, 'WARNING'),
    (40, 'ERROR'),
    (50, 'CRITICAL'),
]

now = timezone.now()

# Uncomment the following lines to clear all AppLog entries before loading fake logs:
# print("[INFO] Deleting all existing AppLog entries...")
# AppLog.objects.all().delete()

created = 0
for app in APPS:
    for inst in INSTANCES:
        for level, level_name in LEVELS:
            for i in range(2):  # 2 logs per combination
                # Add a long message for one of the logs
                if i == 1 and level_name == 'INFO' and app == 'app1' and inst == 'inst1':
                    long_msg = (
                        f"Message {i+1} for {app}/{inst} - "
                        "This is a very long log message intended to test word wrapping in the UI. "
                        "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
                        "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. "
                        "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur. "
                        "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum."
                    )
                    message = long_msg
                else:
                    message = f"Message {i+1} for {app}/{inst}"
                log = AppLog(
                    app_name=app,
                    instance_name=inst,
                    timestamp=now - timedelta(minutes=random.randint(0, 120)),
                    level=level,
                    level_name=level_name,
                    message=message,
                    module="demo_module",
                    func_name="demo_func",
                    line_no=random.randint(1, 100),
                    process=random.randint(1000, 2000),
                    thread=random.randint(1, 10),
                )
                log.save()
                print(f"Created log: {log.app_name} {log.instance_name} {log.level_name} {log.message}")
                created += 1

print(f"Fake logs loaded. Total created: {created}")
