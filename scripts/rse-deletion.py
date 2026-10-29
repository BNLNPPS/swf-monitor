#!/usr/bin/env python3
"""rse-deletion.py — the prod-ops agent's doer for the ``rse_deletion``
message: preview or remove datasets matching a pattern on a disk RSE,
only where tape holds a complete copy.

Logic in ``swf_epicprod/rse_deletion.py``; design in swf-epicprod
``docs/STORAGE.md`` (Removing datasets from an RSE). The catalog is JLab
Rucio as ``eicprod`` by a PanDA-server token, else the proxy in
EVGEN_X509_PROXY. Prints one JSON object on stdout: the plan (preview)
or the per-dataset outcomes (execute). Exit 0 on success, 1 for a
refused request or unreachable catalog, 2 for a removal with failures.

Usage::

    python scripts/rse-deletion.py --phase preview --rse BNL-XRD --pattern '/RECO/26.03.0/*'
    python scripts/rse-deletion.py --phase execute --rse BNL-XRD --pattern '/RECO/26.03.0/*' \\
        --names-file names.json
"""
import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('preview', 'execute'), required=True)
    parser.add_argument('--rse', required=True)
    parser.add_argument('--pattern', required=True)
    parser.add_argument('--names-file', default='',
                        help='execute: JSON list of the dataset names to remove')
    args = parser.parse_args()

    from swf_epicprod import rse_deletion
    from swf_epicprod.jlab_auth import rucio_client

    def refuse(reason):
        print(json.dumps({'error': reason}))
        return 1

    try:
        rse_deletion.validate(args.rse, args.pattern)
        client = rucio_client(proxy=os.environ.get('EVGEN_X509_PROXY'), timeout=60)
        if args.phase == 'preview':
            out = rse_deletion.plan(client, args.rse, args.pattern)
            out['auth'] = getattr(client, 'jlab_auth', '')
            print(json.dumps(out))
            return 0
        with open(args.names_file) as f:
            names = json.load(f)
        out = rse_deletion.execute(client, args.rse, args.pattern, names)
        out['auth'] = getattr(client, 'jlab_auth', '')
        print(json.dumps(out))
        return 2 if out['totals']['failed'] else 0
    except rse_deletion.RseDeletionError as e:
        return refuse(str(e))
    except Exception as e:                                    # noqa: BLE001
        return refuse(f'{type(e).__name__}: {e}')


if __name__ == '__main__':
    sys.exit(main())
