#!/usr/bin/env python3
"""segfault-study-auto.py — the nightly automatic study of traced signatures.

A traced signature that no finding reads and no study has been asked of
is studied once, largest first, at most --limit a night, without
waiting for a reproduction (swf-epicprod docs/SEGFAULT_DIAGNOSIS.md,
Diagnosis; a reproduced signature is queued by the reconciliation). A
frame a finding already reads is marked covered instead of studied. The
study itself is the Diagnose message to the ops agent, whose trigger
records the run on the signature.

Django-bootstrap standalone script, by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-study-auto.py [--limit N] [--dry-run]

Prints one JSON line: the keys queued and the keys marked covered.
"""
import argparse
import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.models import CrashSignature  # noqa: E402
from monitor_app.segfaults import covering_finding, queue_traced_studies  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--limit', type=int, default=3, help='studies queued per night')
    ap.add_argument('--dry-run', action='store_true', help='list the candidates, queue nothing')
    args = ap.parse_args()
    if args.dry_run:
        rows = []
        for sig in (CrashSignature.objects.filter(level='record')
                    .exclude(status__in=('diagnosed', 'handed_off', 'fixed', 'accepted'))
                    .order_by('-crashes')):
            if (sig.trace or {}).get('trace_status') != 'found' or (sig.data or {}).get('diagnosis'):
                continue
            f = covering_finding(sig)
            rows.append({'key': sig.key, 'crashes': sig.crashes,
                         'frame': (sig.trace or {}).get('frame', '')[:60],
                         'covered_by': f['fid'] if f else ''})
        print(json.dumps({'dry_run': True, 'candidates': rows, 'limit': args.limit}))
        return 0
    result = queue_traced_studies(args.limit)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
