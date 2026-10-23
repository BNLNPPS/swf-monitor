#!/usr/bin/env python3
"""Record fatal evidence from a preserved reproduction stage log.

For historical jobs predating the fatal watchdog. Default is read-only;
--apply attaches the evidence to the matching ProbeRun without inventing
a payload exit or changing PanDA status. Reconciliation remains separate.
See swf-epicprod/docs/SEGFAULT_DIAGNOSIS.md.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()
from django.db import transaction
from canary.store.models import ProbeRun
from swf_epicprod.payload.fatal_watch import fatal_signal, tail


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--job', type=int, required=True)
    ap.add_argument('--stage', choices=['simulation', 'reconstruction'], required=True)
    ap.add_argument('--log', required=True)
    ap.add_argument('--source', required=True, help='original source URL of this job stage log')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    text = tail(args.log)
    evidence = fatal_signal(text)
    if evidence is None:
        ap.error('no explicit fatal signal in the stage log tail')
    evidence.update(stage=args.stage, source=args.source, log=Path(args.log).name,
                    tail=text[-16000:], sha256=hashlib.sha256(Path(args.log).read_bytes()).hexdigest(),
                    observed_at=datetime.now(timezone.utc).isoformat())
    with transaction.atomic():
        runs = ProbeRun.objects.filter(data__kind='payload', data__pandaid=args.job)
        if args.apply:
            runs = runs.select_for_update()
        run = runs.get()
        if not (run.data or {}).get('signature'):
            ap.error('job is not a signature reproduction')
        if args.apply:
            run.data = dict(run.data or {}, fatal=evidence)
            run.save(update_fields=['data', 'modified_at'])
        print(json.dumps({'applied': args.apply, 'job': args.job, 'run': str(run.id),
                          'signal': evidence['signal_name'], 'stage': args.stage,
                          'source': args.source}))


if __name__ == '__main__':
    main()
