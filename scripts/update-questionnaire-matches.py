#!/usr/bin/env python
"""Rebuild the PCS task-local questionnaire match cache."""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--updated-by", default="questionnaire_match")
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")
    try:
        import django
        django.setup()
        from pcs.services import rebuild_questionnaire_match_cache
        summary = rebuild_questionnaire_match_cache(updated_by=args.updated_by)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
