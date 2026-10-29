#!/usr/bin/env python3
"""rse-deletion.py — the prod-ops agent's doer for the ``rse_deletion``
message: preview or remove datasets matching a pattern on a disk RSE,
only where tape holds a complete copy and the producing task is not
active.

Logic in ``swf_epicprod/rse_deletion.py``; design in swf-epicprod
``docs/STORAGE.md`` (Removing datasets from an RSE). The catalog is JLab
Rucio as ``eicprod`` by a PanDA-server token, else the proxy in
EVGEN_X509_PROXY; task state is the storage pass's reading of PCS and
PanDA. Django-bootstrap standalone script.

Prints JSON lines on stdout, each flushed as written: for a removal, one
``{"event": "outcome", ...}`` line per dataset as it is decided, so a
removal cut short leaves what it did; then one ``{"event": "result",
...}`` line with the plan (preview) or the outcomes and totals
(execute), or ``{"event": "error", "error": ...}``. Exit 0 on success, 1
for a refused request or unreachable catalog, 2 for a removal with
failures.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/rse-deletion.py --phase preview --rse BNL-XRD --pattern '/RECO/26.03.0/*'
    python ../scripts/rse-deletion.py --phase execute --rse BNL-XRD --pattern '/RECO/26.03.0/*' \\
        --names-file names.json
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

from swf_epicprod import rse_deletion  # noqa: E402
from swf_epicprod.analytics.storage import _task_states  # noqa: E402
from swf_epicprod.jlab_auth import rucio_client  # noqa: E402


def emit(event, **body):
    print(json.dumps(dict(body, event=event)), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('preview', 'execute'), required=True)
    parser.add_argument('--rse', required=True)
    parser.add_argument('--pattern', required=True)
    parser.add_argument('--names-file', default='',
                        help='execute: JSON list of the dataset names to remove')
    args = parser.parse_args()

    try:
        rse_deletion.validate(args.rse, args.pattern)
        client = rucio_client(proxy=os.environ.get('EVGEN_X509_PROXY'), timeout=60)
        auth = getattr(client, 'jlab_auth', '')
        if args.phase == 'preview':
            out = rse_deletion.plan(client, args.rse, args.pattern, _task_states)
            emit('result', auth=auth, **out)
            return 0
        with open(args.names_file) as f:
            names = json.load(f)
        out = rse_deletion.execute(
            client, args.rse, args.pattern, names, _task_states,
            on_outcome=lambda o: emit('outcome', **o))
        emit('result', auth=auth, **out)
        return 2 if out['totals']['failed'] else 0
    except rse_deletion.RseDeletionError as e:
        emit('error', error=str(e))
    except Exception as e:                                    # noqa: BLE001
        emit('error', error=f'{type(e).__name__}: {e}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
