#!/usr/bin/env python3
"""backfill-produced-events.py — event counts for produced FULL and RECO
files registered before the payload counted them
(swf-epicprod docs/RUCIO_REGISTRATION_CONTRACT.md, the produced rows'
backfill).

The produced datasets are the RECO and FULL datasets of the campaign
snapshots (``$SWF_TMP_DIR/rucio-snapshots/current-*.json``). For each,
the files are listed from JLab Rucio with their ``events`` attribute;
a file without one is read at its disk replica (BNL-XRD, epicxrd1
inside SCDF, per Rucio's replica record; a tape-only file is left
out) as the entry count of the podio ``events`` tree (the tree header
carries it; no event bytes move), and the count is written as the
file's ``events`` attribute, as the payload writes it for new files. A
dataset's derived total is read back once every file of it is counted,
and held to the sum. A file that cannot be counted is reported by name
and left as it is; nothing is invented.

Modes:
  --scan            list the files without a count and stop (Rucio reads only)
  (default)         count and report, write nothing (door reads)
  --apply           count and write the attribute

The pass is resumable: its state file records every file done, and a
later run skips them; ``--limit-datasets N`` bounds a run. Runs under the
production operations agent's environment (``EVGEN_X509_PROXY``,
``EVGEN_XRD_DOOR``); ``--env-file`` loads a systemd-style KEY=VALUE file
when run by hand. Progress and errors go to stderr; the last stdout
line is a JSON summary; each run logs one ``produced_events_backfill``
action.

Usage:
    backfill-produced-events.py --scan [--env-file /opt/swf-monitor/config/env/production.env]
    backfill-produced-events.py --apply [--limit-datasets N] [--workers 4] [--env-file ...]
"""
import argparse
import glob
import importlib.util
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SWF_TMP_DIR = os.environ.get('SWF_TMP_DIR', '/data/swf-tmp')
SNAPSHOT_GLOB = os.path.join(SWF_TMP_DIR, 'rucio-snapshots', 'current-*.json')
STATE_PATH = os.path.join(SWF_TMP_DIR, 'produced-events-backfill', 'state.json')
EVENTS_TREE = 'events'
PRODUCED_PREFIXES = ('/RECO/', '/FULL/')

log = logging.getLogger('backfill-produced-events')


def _doer():
    """The EVGEN registration doer's module, for its Rucio client, scope
    and door constants (the same account, door and mapping)."""
    spec = importlib.util.spec_from_file_location(
        'register_evgen_rucio', os.path.join(THIS_DIR, 'register-evgen-rucio.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_env_file(path):
    for line in open(path):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key, value.strip().strip('"'))


def produced_datasets():
    """The produced dataset names across every campaign snapshot, newest
    snapshot of a dataset winning, sorted."""
    names = {}
    for path in sorted(glob.glob(SNAPSHOT_GLOB)):
        snap = json.load(open(path))
        for camp in (snap.get('campaigns') or {}).values():
            for ds in camp.get('datasets') or []:
                name = str(ds.get('did') or '').split(':', 1)[-1]
                if name.startswith(PRODUCED_PREFIXES):
                    names[name] = os.path.basename(path)
    return sorted(names)


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except (OSError, ValueError):
        return {'done': {}, 'failed': {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def files_without_count(client, scope, name):
    """(files lacking events, files in all) of one dataset from Rucio."""
    missing, total = [], 0
    for f in client.list_files(scope, name):
        total += 1
        if f.get('events') in (None, 0) and str(f.get('name', '')).endswith('.root'):
            missing.append(f['name'])
    return missing, total


def count_one(pfn, timeout):
    import uproot
    with uproot.open(pfn, timeout=timeout) as fh:
        if EVENTS_TREE not in fh:
            return None, f'no {EVENTS_TREE} tree'
        return int(fh[EVENTS_TREE].num_entries), ''


# Where a produced file is read from: its disk replica, never tape. The
# campaign's outputs sit on BNL-XRD (epicxrd1, inside SCDF, read directly
# from this host) with a tape copy at JLAB-TAPE-SE; the JLab door holds
# the EVGEN inputs, not these.
TAPE_RSES = {'JLAB-TAPE-SE'}
RSE_PREFERENCE = ('BNL-XRD', 'EIC-XRD')


def replica_pfns(client, scope, name, wanted):
    """{file name: pfn} for the wanted files of a dataset, from Rucio's
    replica record, a disk replica preferred in RSE_PREFERENCE order and
    a root:// PFN over any other; a file with only a tape replica is
    left out (reported by the caller)."""
    out = {}
    wanted = set(wanted)
    for rep in client.list_replicas([{'scope': scope, 'name': name}]):
        fname = rep.get('name')
        if fname not in wanted:
            continue
        rses = {rse: pfns for rse, pfns in (rep.get('rses') or {}).items()
                if pfns and rse not in TAPE_RSES}
        ordered = [r for r in RSE_PREFERENCE if r in rses] + [r for r in rses if r not in RSE_PREFERENCE]
        for rse in ordered:
            pfns = rses[rse]
            pfn = next((p for p in pfns if p.startswith('root://')), pfns[0])
            out[fname] = pfn
            break
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--scan', action='store_true', help='list files without a count; no door reads')
    ap.add_argument('--apply', action='store_true', help='write the counts to Rucio')
    ap.add_argument('--limit-datasets', type=int, default=0, help='datasets to count in this run (0 = all)')
    ap.add_argument('--campaign', help='count one campaign only, by version prefix, e.g. 26.07 (takes 26.07.0, .1 and .2)')
    ap.add_argument('--dataset', help='one dataset name only (a check run; skips the snapshot listing)')
    ap.add_argument('--workers', type=int, default=int(os.environ.get('EVGEN_EVENTS_WORKERS', '4')))
    ap.add_argument('--timeout', type=int, default=int(os.environ.get('EVGEN_EVENTS_TIMEOUT', '120')), help='seconds per file')
    ap.add_argument('--env-file', help='KEY=VALUE file to load (the agent environment)')
    ap.add_argument('--created-by', default='', help="the action record's username")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    if args.env_file:
        load_env_file(args.env_file)
    proxy = os.environ.get('EVGEN_X509_PROXY')
    if not proxy or not os.path.exists(proxy):
        print(json.dumps({'error': 'EVGEN_X509_PROXY is not set or missing'}))
        return 2
    doer = _doer()
    client = doer.rucio_client(proxy)
    os.environ['X509_USER_PROXY'] = proxy
    scope, door, base = doer.RUCIO_SCOPE, doer.XRD_DOOR, doer.XRD_BASE
    t0 = time.monotonic()
    state = load_state()
    names = [args.dataset] if args.dataset else produced_datasets()
    log.info('%d produced datasets across the snapshots', len(names))

    # The worklist: every file of a produced dataset without a count,
    # less what an earlier run already counted or gave up on.
    todo = {}
    files_total = 0
    for i, name in enumerate(names, 1):
        try:
            missing, total = files_without_count(client, scope, name)
        except Exception as e:                                # noqa: BLE001
            log.error('%s: listing failed: %s', name, e)
            continue
        files_total += total
        missing = [f for f in missing if f not in state['done']]
        if missing:
            todo[name] = missing
        if i % 100 == 0:
            log.info('  listed %d/%d datasets, %d files without a count so far',
                     i, len(names), sum(len(v) for v in todo.values()))
    n_missing = sum(len(v) for v in todo.values())
    by_campaign = {}
    for name, missing in todo.items():
        camp = name.split('/')[2] if name.count('/') >= 2 else '?'   # /RECO/<version>/...
        by_campaign[camp] = by_campaign.get(camp, 0) + len(missing)
    summary = {'datasets': len(names), 'files': files_total, 'datasets_without_counts': len(todo),
               'files_without_count': n_missing, 'by_campaign': dict(sorted(by_campaign.items())),
               'mode': 'scan' if args.scan else ('apply' if args.apply else 'dry')}
    log.info('%d files without a count in %d datasets (of %d files in %d datasets)',
             n_missing, len(todo), files_total, len(names))
    # The worklist on disk: what the scan found, for the counting runs and
    # for reading the size of the job without listing again.
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(os.path.join(os.path.dirname(STATE_PATH), 'worklist.json'), 'w') as fh:
        json.dump({'scanned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                   'by_campaign': summary['by_campaign'],
                   'datasets': {k: len(v) for k, v in todo.items()}}, fh)
    if args.scan:
        summary['seconds'] = round(time.monotonic() - t0, 1)
        print(json.dumps(summary))
        return 0
    if args.campaign:
        # A version prefix: 26.07 takes 26.07.0, 26.07.1 and 26.07.2.
        todo = {k: v for k, v in todo.items() if k.split('/')[2].startswith(args.campaign)}
        log.info('campaign %s: %d files in %d datasets', args.campaign, sum(len(v) for v in todo.values()), len(todo))

    counted = written = failed = 0
    datasets_done = 0
    for name, missing in todo.items():
        if args.limit_datasets and datasets_done >= args.limit_datasets:
            break
        counts, errors = {}, {}
        try:
            pfns = replica_pfns(client, scope, name, missing)
        except Exception as e:                                # noqa: BLE001
            log.error('%s: replica listing failed: %s', name, e)
            continue
        for f in missing:
            if f not in pfns:
                errors[f] = 'no disk replica (tape only, or none)'
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(count_one, pfns[f], args.timeout): f for f in missing if f in pfns}
            for fut, f in futures.items():
                try:
                    n, why = fut.result(timeout=args.timeout + 30)
                except FutureTimeout:
                    n, why = None, f'timed out after {args.timeout}s'
                except Exception as e:                        # noqa: BLE001
                    n, why = None, f'{type(e).__name__}: {e}'
                if n is None:
                    errors[f] = why
                else:
                    counts[f] = n
        counted += len(counts)
        failed += len(errors)
        for f, why in errors.items():
            state['failed'][f] = why
            log.warning('%s: %s', f, why)
        if args.apply:
            for f, n in counts.items():
                try:
                    client.set_metadata(scope, f, 'events', n)
                    state['done'][f] = n
                    written += 1
                except Exception as e:                        # noqa: BLE001
                    state['failed'][f] = f'set_metadata: {e}'
                    failed += 1
            # The dataset's derived total, read back once every file carries
            # a count, and held to the sum.
            if not errors and not any(f in state['failed'] for f in missing):
                try:
                    meta = client.get_metadata(scope, name)
                    total = sum(counts.values()) + sum(
                        int(f.get('events') or 0) for f in client.list_files(scope, name)
                        if f['name'] not in counts)
                    if meta.get('events') not in (None, total):
                        log.warning('%s: derived events %s differs from the files\' sum %s',
                                    name, meta.get('events'), total)
                except Exception as e:                        # noqa: BLE001
                    log.warning('%s: dataset read-back failed: %s', name, e)
            save_state(state)
        datasets_done += 1
        log.info('%s: %d counted, %d failed%s (%d/%d datasets)', name, len(counts), len(errors),
                 ', written' if args.apply else '', datasets_done, len(todo))
    summary.update({'datasets_done': datasets_done, 'counted': counted, 'written': written,
                    'failed': failed, 'remaining_files': n_missing - counted,
                    'seconds': round(time.monotonic() - t0, 1)})
    try:
        sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
        import django
        django.setup()
        from monitor_app.epicprod_logging import log_epicprod_action
        log_epicprod_action(
            args.created_by or 'backfill-produced-events', 'produced_events_backfill',
            outcome='ok' if not failed else 'warning', sublevel='normal', live_default=False,
            username=args.created_by, duration_ms=int((time.monotonic() - t0) * 1000),
            message=(f"produced event-count backfill ({summary['mode']}): {counted} files counted, "
                     f"{written} written, {failed} failed, {summary['remaining_files']} left "
                     f"of {n_missing} without a count in {len(todo)} datasets"),
            **{k: v for k, v in summary.items() if isinstance(v, int)})
    except Exception as e:                                    # noqa: BLE001
        log.error('action record failed: %s', e)
    print(json.dumps(summary))
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
