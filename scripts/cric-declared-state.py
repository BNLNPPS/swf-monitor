#!/usr/bin/env python3
"""cric-declared-state.py — the declared record from CRIC.
The doer of the ops agent's ``cric_declared_state`` (swf-epicprod
docs/CONTINUOUS_PRODUCTION.md, Declared downtime): reads the PanDA queue
status rules, the DDM endpoint status rules and the downtime objects of
the CRIC ePIC's PanDA reads (datalake-cric.cern.ch) with the production
proxy, keeps them as the declared record in the entry store
(monitor_app/declared.py), and logs one ``declared_state_sync`` action
naming what appeared, changed, expired and cleared. The scope is the
EIC queues (vo eic), the endpoints those queues name, and the resource
centres behind them.
Credentials: ``X509_USER_PROXY`` (the ops agent's Rucio proxy, accepted
by CRIC) and the grid CA directory (``X509_CERT_DIR``, default
/etc/grid-security/certificates).
Django-bootstrap standalone script, by hand::
    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/cric-declared-state.py [--dry-run] [--created-by NAME]
Prints one JSON line: the counts read and the changes. Exit status 1 when
a CRIC read failed; nothing is written then.
"""
import argparse
import json
import logging
import os
import sys
import time

import requests

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402
django.setup()
from django.utils import timezone  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app import declared  # noqa: E402

log = logging.getLogger('cric-declared-state')
TIMEOUT = 60


def cric_get(url, proxy, ca_dir):
    """One CRIC read with the proxy as client certificate; raises on
    anything but a 200 with JSON."""
    r = requests.get(url, cert=(proxy, proxy), verify=ca_dir, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'{url}: HTTP {r.status_code}: {r.text[:200]}')
    return r.json()


def scope(queues_doc):
    """The EIC queues, the endpoints they name (astorages) and the
    resource centres behind them, from the pandaqueue document."""
    queues = set(queues_doc or {})
    endpoints, rcsites = set(), set()
    for q in (queues_doc or {}).values():
        for names in ((q or {}).get('astorages') or {}).values():
            endpoints.update(names or [])
        if (q or {}).get('rc_site'):
            rcsites.add(q['rc_site'])
    return queues, endpoints, rcsites


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='read CRIC and print the records, write nothing')
    ap.add_argument('--created-by', default='cric_declared_state')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    t0 = time.monotonic()
    proxy = os.environ.get('X509_USER_PROXY') or ''
    ca_dir = os.environ.get('X509_CERT_DIR') or '/etc/grid-security/certificates'
    if not proxy or not os.path.exists(proxy):
        msg = f'no proxy at X509_USER_PROXY={proxy!r}; CRIC refuses anonymous reads'
        log.error(msg)
        log_epicprod_action('prodops-agent', 'declared_state_sync', outcome='error',
                            username=args.created_by, sublevel='normal', live_default=True,
                            level=logging.ERROR, message=f'declared state sync failed: {msg}')
        print(json.dumps({'error': msg}))
        return 1
    try:
        queues_doc = cric_get(declared.CRIC_QUEUES, proxy, ca_dir)
        queues, endpoints, rcsites = scope(queues_doc)
        found = []
        found += declared.rules_from_pandaqueuestatus(
            cric_get(declared.CRIC_QUEUE_STATUS, proxy, ca_dir), queues)
        found += declared.rules_from_ddmendpointstatus(
            cric_get(declared.CRIC_ENDPOINT_STATUS, proxy, ca_dir), endpoints)
        found += declared.windows_from_downtime(
            cric_get(declared.CRIC_DOWNTIME, proxy, ca_dir), rcsites)
    except Exception as e:                                    # noqa: BLE001
        log.exception('CRIC read failed')
        log_epicprod_action('prodops-agent', 'declared_state_sync', outcome='error',
                            username=args.created_by, sublevel='normal', live_default=True,
                            level=logging.ERROR,
                            duration_ms=int((time.monotonic() - t0) * 1000),
                            message=f'declared state sync failed: {str(e)[:300]}')
        print(json.dumps({'error': str(e)[:300]}))
        return 1
    now = timezone.now()
    counts = {'queues': len(queues), 'endpoints': len(endpoints), 'rcsites': len(rcsites),
              'records': len(found),
              'in_force': sum(1 for r in found if declared.standing_of(r, now) == 'active'),
              'future': sum(1 for r in found if declared.standing_of(r, now) == 'future')}
    if args.dry_run:
        print(json.dumps({'dry_run': True, **counts, 'records': found}, default=str))
        return 0
    changes = declared.sync(found, now, changed_by=args.created_by)
    moved = {k: v for k, v in changes.items() if k != 'unchanged' and v}
    log_epicprod_action(
        'prodops-agent', 'declared_state_sync',
        outcome='ok', username=args.created_by,
        sublevel='normal' if moved else 'low', live_default=bool(moved),
        duration_ms=int((time.monotonic() - t0) * 1000),
        message=('declared state from CRIC: '
                 + f"{counts['in_force']} in force, {counts['future']} future"
                 + ''.join(f"; {k} {', '.join(v)}" for k, v in moved.items())),
        **counts, **{k: len(v) for k, v in changes.items() if k != 'unchanged'},
        unchanged=changes['unchanged'])
    print(json.dumps({**counts, 'changes': changes}, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
