#!/usr/bin/env python3
"""Refresh the retained efficiency history; hourly, with monthly --full reconciliation."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402
django.setup()
from monitor_app.compute_efficiency import product  # noqa: E402
from monitor_app.productive_cores import product as productive_cores_product  # noqa: E402

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--full', action='store_true')
    args = ap.parse_args()
    result = product(refresh=True, full=args.full)
    if not result.get('value'):
        raise RuntimeError('Efficiency history did not produce a stored result')
    daily_cores = productive_cores_product(refresh=True)
    if not daily_cores.get('value'):
        raise RuntimeError('Daily production core history did not produce a stored result')
    print(json.dumps({'built_at': str(result['built_at']),
                      'daily_rows': len(result['value']['daily']),
                      'reread_from': result['value']['reread_from'],
                      'production_core_days': len(daily_cores['value']['daily'])}))
