#!/usr/bin/env python3
"""register-evgen-rucio.py — register one EVGEN input directory in JLab Rucio.

The production-ops agent's doer for the EVGEN inputs page's "Register in
Rucio" action (swf-epicprod docs/EPICPROD_EVGEN_INPUTS.md § Registration).
Given an EVGEN path (``/EVGEN/<class>/...``) it lists the files under it on
the JLab production door, takes each file's size and adler32 from the door
(``xrdfs query checksum`` — the server computes it; no bytes are read), and
registers them as ``epic:/EVGEN/...`` datasets at RSE ``EIC-XRD`` under the
``eicprod`` account: one dataset per directory holding files, one replica
per file with its PFN on the door, files attached to their dataset. The
registration contract is the production team's reference
(eic/simulation_campaign_hepmc3 PR #100: ``scripts/calculate_checksum_xrd.sh``
and ``scripts/register_from_checksum_listing.py``). Re-running is safe:
existing datasets, replicas, and attachments are kept and counted.

Credential: the JLab ``eicprod`` x509 proxy. ``EVGEN_X509_PROXY`` names the
agent's private copy (mode 0600; the same file the submission doer ships in
the sandbox). ``EVGEN_X509_PROXY_SOURCE``, when set, names the renewal
source — the production team's proxy drop — and a source proxy that outlives
the private copy is copied over it before use, so a renewed proxy is picked
up at the next registration with no operator step.

No silent failures: a file whose checksum the door does not return makes the
run incomplete and nothing is registered (exit 4); a dataset whose Rucio file
list disagrees with the door listing after registration exits 7. The last
stdout line is a JSON summary the agent relays to the page and the action
stream; progress and errors go to stderr.

Usage::

    register-evgen-rucio.py --path /EVGEN/BACKGROUNDS/BEAMGAS/proton/pythia8.306-1.0/100GeV
    register-evgen-rucio.py --path ... --dry-run    # list + checksum only, no Rucio write

Exit codes: 0 ok · 2 bad path · 3 listing failed or empty · 4 checksum
incomplete · 5 proxy unusable · 6 Rucio error · 7 verification mismatch.
"""
import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# The JLab production door and the filesystem root the EVGEN tree hangs
# from. DID name = door path minus XRD_BASE; PFN = door + '/' + door path
# (the '//' after the port is the xrootd absolute-path form).
XRD_DOOR = os.environ.get('EVGEN_XRD_DOOR', 'root://dtn-rucio.jlab.org:1094')
XRD_BASE = '/volatile/eic/EPIC'
RUCIO_HOST = os.environ.get('JLAB_RUCIO_URL', 'https://rucio-server.jlab.org:443')
RUCIO_ACCOUNT = os.environ.get('EVGEN_RUCIO_ACCOUNT', 'eicprod')
RUCIO_SCOPE = 'epic'
RUCIO_RSE = os.environ.get('EVGEN_RUCIO_RSE', 'EIC-XRD')
CHECKSUM_WORKERS = int(os.environ.get('EVGEN_CHECKSUM_WORKERS', '4'))
LISTING_TIMEOUT = 600
CHECKSUM_TIMEOUT = 120
BATCH_SIZE = 500
PATH_OK = re.compile(r'^/EVGEN/[A-Za-z0-9._=+\-/]+$')

EXIT_BAD_PATH, EXIT_LISTING, EXIT_CHECKSUM, EXIT_PROXY, EXIT_RUCIO, EXIT_VERIFY = 2, 3, 4, 5, 6, 7


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class DoerError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# -- credential ---------------------------------------------------------------

def _x509_not_after(path):
    """Expiry epoch of a proxy file via openssl (the credential checker's
    method)."""
    out = subprocess.run(['openssl', 'x509', '-enddate', '-noout', '-in', path],
                         capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip())
    not_after = out.stdout.strip().split('=', 1)[1]
    return float(calendar.timegm(time.strptime(not_after, '%b %d %H:%M:%S %Y %Z')))


def resolve_proxy():
    """The proxy to use: the private copy, refreshed from the renewal source
    when the source outlives it. Returns (path, summary)."""
    home = os.environ.get('EVGEN_X509_PROXY', '')
    source = os.environ.get('EVGEN_X509_PROXY_SOURCE', '')
    if not home:
        raise DoerError(EXIT_PROXY, 'EVGEN_X509_PROXY is not set')
    home_exp = None
    if os.path.exists(home):
        try:
            home_exp = _x509_not_after(home)
        except Exception as e:  # noqa: BLE001
            _log(f'WARNING: private proxy {home} unreadable: {e}')
    refreshed = False
    if source:
        try:
            src_exp = _x509_not_after(source)
        except Exception as e:  # noqa: BLE001
            # The source is a convenience for renewal, not the credential of
            # record: report and carry on with the private copy.
            _log(f'WARNING: proxy source {source} unusable: {e}')
        else:
            if src_exp > time.time() and (home_exp is None or src_exp > home_exp + 60):
                data = open(source, 'rb').read()
                # Written in place: the credential directory is not writable
                # by the agent, the file is.
                fd = os.open(home, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                os.chmod(home, 0o600)
                _log(f'proxy refreshed from {source} '
                     f'(expires {time.strftime("%Y-%m-%d", time.gmtime(src_exp))})')
                home_exp = src_exp
                refreshed = True
    if home_exp is None:
        raise DoerError(EXIT_PROXY, f'no usable proxy at {home}')
    days_left = (home_exp - time.time()) / 86400.0
    if days_left <= 0:
        raise DoerError(EXIT_PROXY, f'proxy {home} expired '
                        f'{time.strftime("%Y-%m-%d", time.gmtime(home_exp))}')
    return home, {'path': home, 'days_left': round(days_left, 1),
                  'refreshed_from_source': refreshed}


# -- door ---------------------------------------------------------------------

def _xrdfs(args, proxy, timeout):
    env = dict(os.environ, X509_USER_PROXY=proxy)
    return subprocess.run(['xrdfs', XRD_DOOR] + args, capture_output=True,
                          text=True, timeout=timeout, env=env)


def list_files(evgen_path, proxy):
    """Every regular file under the path on the door: [(door_path, bytes)].
    NFS silly-rename remnants (``.nfs*``) are skipped and counted."""
    door_path = XRD_BASE + evgen_path
    try:
        p = _xrdfs(['ls', '-l', '-R', door_path], proxy, LISTING_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DoerError(EXIT_LISTING, f'listing timed out after {LISTING_TIMEOUT}s')
    if p.returncode != 0:
        reason = (p.stderr or p.stdout).strip().splitlines()
        raise DoerError(EXIT_LISTING, 'listing failed: '
                        + (reason[-1] if reason else f'rc={p.returncode}'))
    files, skipped = [], []
    for line in (p.stdout or '').splitlines():
        parts = line.split(maxsplit=6)
        if len(parts) < 7:
            continue
        flags, _owner, _group, size, _date, _time, path = parts
        if flags.startswith('d'):
            continue
        if os.path.basename(path).startswith('.nfs'):
            skipped.append(path)
            continue
        try:
            files.append((path, int(size)))
        except ValueError:
            raise DoerError(EXIT_LISTING, f'unparseable listing line: {line!r}')
    if not files:
        raise DoerError(EXIT_LISTING, f'no files under {door_path}')
    return files, skipped


def checksums(files, proxy):
    """adler32 per file from the door; returns ({path: adler32}, [(path, why)])."""
    def one(path):
        try:
            p = _xrdfs(['query', 'checksum', path], proxy, CHECKSUM_TIMEOUT)
        except subprocess.TimeoutExpired:
            return path, None, f'timed out after {CHECKSUM_TIMEOUT}s'
        parts = (p.stdout or '').split()
        if p.returncode == 0 and len(parts) == 2 and parts[0] == 'adler32':
            return path, parts[1], ''
        why = (p.stderr or p.stdout).strip().splitlines()
        return path, None, why[-1] if why else f'rc={p.returncode}'

    ok, failed = {}, []
    with ThreadPoolExecutor(max_workers=CHECKSUM_WORKERS) as pool:
        for path, adler, why in pool.map(one, [f[0] for f in files]):
            if adler:
                ok[path] = adler
            else:
                failed.append((path, why))
    return ok, failed


# -- Rucio --------------------------------------------------------------------

def plan_datasets(files, adlers):
    """Group files into datasets: one per directory holding files, named by
    the DID convention (door path minus XRD_BASE)."""
    datasets = defaultdict(list)
    for door_path, size in files:
        name = door_path[len(XRD_BASE):]
        datasets[os.path.dirname(name)].append({
            'scope': RUCIO_SCOPE, 'name': name, 'bytes': size,
            'adler32': adlers[door_path], 'pfn': f'{XRD_DOOR}/{door_path}'})
    return dict(sorted(datasets.items()))


def register(datasets, proxy):
    from rucio.client import Client
    from rucio.common.exception import (DataIdentifierAlreadyExists,
                                        DuplicateContent, FileAlreadyExists,
                                        RucioException)
    try:
        client = Client(rucio_host=RUCIO_HOST, auth_host=RUCIO_HOST,
                        account=RUCIO_ACCOUNT, auth_type='x509_proxy',
                        creds={'client_proxy': proxy})
        who = client.whoami()
    except RucioException as e:
        raise DoerError(EXIT_RUCIO, f'Rucio auth failed: {e}')
    if who.get('account') != RUCIO_ACCOUNT:
        raise DoerError(EXIT_RUCIO, f'proxy maps to account {who.get("account")!r}, '
                        f'not {RUCIO_ACCOUNT!r}')

    result = []
    for ds_name, entries in datasets.items():
        rec = {'did': f'{RUCIO_SCOPE}:{ds_name}', 'files': len(entries),
               'bytes': sum(e['bytes'] for e in entries),
               'created': False, 'replicas_new': 0, 'attached_new': 0}
        try:
            client.add_dataset(scope=RUCIO_SCOPE, name=ds_name)
            rec['created'] = True
        except DataIdentifierAlreadyExists:
            pass
        except RucioException as e:
            raise DoerError(EXIT_RUCIO, f'add_dataset {ds_name}: {e}')
        # Replicas, then attachment, in batches; a batch refused because one
        # member already exists is retried per file so the rest still land.
        for start in range(0, len(entries), BATCH_SIZE):
            batch = entries[start:start + BATCH_SIZE]
            try:
                client.add_replicas(rse=RUCIO_RSE, files=batch, ignore_availability=True)
                rec['replicas_new'] += len(batch)
            except FileAlreadyExists:
                for entry in batch:
                    try:
                        client.add_replicas(rse=RUCIO_RSE, files=[entry],
                                            ignore_availability=True)
                        rec['replicas_new'] += 1
                    except FileAlreadyExists:
                        pass
                    except RucioException as e:
                        raise DoerError(EXIT_RUCIO, f'add_replicas {entry["name"]}: {e}')
            except RucioException as e:
                raise DoerError(EXIT_RUCIO, f'add_replicas batch in {ds_name}: {e}')
        dids = [{'scope': e['scope'], 'name': e['name']} for e in entries]
        for start in range(0, len(dids), BATCH_SIZE):
            batch = dids[start:start + BATCH_SIZE]
            try:
                client.attach_dids(scope=RUCIO_SCOPE, name=ds_name, dids=batch)
                rec['attached_new'] += len(batch)
            except (DuplicateContent, FileAlreadyExists):
                for did in batch:
                    try:
                        client.attach_dids(scope=RUCIO_SCOPE, name=ds_name, dids=[did])
                        rec['attached_new'] += 1
                    except (DuplicateContent, FileAlreadyExists):
                        pass
                    except RucioException as e:
                        raise DoerError(EXIT_RUCIO, f'attach {did["name"]}: {e}')
            except RucioException as e:
                raise DoerError(EXIT_RUCIO, f'attach_dids batch to {ds_name}: {e}')
        # Verify against the catalog: every listed file is now content of
        # its dataset.
        try:
            in_rucio = {f['name'] for f in client.list_files(RUCIO_SCOPE, ds_name)}
        except RucioException as e:
            raise DoerError(EXIT_RUCIO, f'list_files {ds_name}: {e}')
        missing = [e['name'] for e in entries if e['name'] not in in_rucio]
        if missing:
            raise DoerError(EXIT_VERIFY, f'{len(missing)} of {len(entries)} files not '
                            f'in {rec["did"]} after registration, e.g. {missing[0]}')
        rec['rucio_files'] = len(in_rucio)
        _log(f'  {rec["did"]}: {rec["files"]} files, {rec["bytes"]} bytes, '
             f'{"created" if rec["created"] else "existed"}, '
             f'{rec["replicas_new"]} new replicas, {rec["attached_new"]} newly attached, '
             f'{rec["rucio_files"]} files in Rucio')
        result.append(rec)
    return result


# -- main ---------------------------------------------------------------------

def main(argv):
    ap = argparse.ArgumentParser(description='Register one EVGEN directory in JLab Rucio.')
    ap.add_argument('--path', required=True, help='EVGEN path, e.g. /EVGEN/DIS/...')
    ap.add_argument('--dry-run', action='store_true',
                    help='list and checksum only; write nothing to Rucio')
    args = ap.parse_args(argv[1:])

    evgen_path = args.path.strip().rstrip('/')
    summary = {'ok': False, 'path': evgen_path, 'dry_run': bool(args.dry_run)}
    t0 = time.monotonic()
    try:
        if not PATH_OK.match(evgen_path) or '..' in evgen_path or '//' in evgen_path \
                or len([s for s in evgen_path.split('/') if s]) < 3:
            raise DoerError(EXIT_BAD_PATH, f'not an EVGEN path: {evgen_path!r}')
        proxy, summary['proxy'] = resolve_proxy()
        _log(f'proxy {proxy}: {summary["proxy"]["days_left"]} days left')

        files, skipped = list_files(evgen_path, proxy)
        summary.update(files=len(files), bytes=sum(s for _, s in files),
                       skipped=len(skipped))
        _log(f'{len(files)} files, {summary["bytes"]} bytes under {XRD_BASE}{evgen_path}'
             + (f' ({len(skipped)} .nfs remnants skipped)' if skipped else ''))

        adlers, failed = checksums(files, proxy)
        if failed:
            summary['checksum_failed'] = [{'file': f, 'reason': why} for f, why in failed[:20]]
            for f, why in failed[:20]:
                _log(f'  CHECKSUM FAILED {f}: {why}')
            raise DoerError(EXIT_CHECKSUM, f'{len(failed)} of {len(files)} checksums '
                            f'not returned by the door; nothing registered')
        _log(f'{len(adlers)} checksums from the door in {time.monotonic() - t0:.0f}s')

        datasets = plan_datasets(files, adlers)
        summary['datasets'] = [{'did': f'{RUCIO_SCOPE}:{n}', 'files': len(e),
                                'bytes': sum(x['bytes'] for x in e)}
                               for n, e in datasets.items()]
        if args.dry_run:
            for d in summary['datasets']:
                _log(f'  would register {d["did"]}: {d["files"]} files, {d["bytes"]} bytes')
            summary['ok'] = True
        else:
            summary['datasets'] = register(datasets, proxy)
            summary['ok'] = True
    except DoerError as e:
        summary['error'] = str(e)
        summary['exit_code'] = e.code
        _log(f'ERROR: {e}')
    except Exception as e:  # noqa: BLE001 — the agent needs a reason, not a trace
        summary['error'] = f'{type(e).__name__}: {e}'
        summary['exit_code'] = 1
        _log(f'ERROR: {summary["error"]}')
    summary['seconds'] = round(time.monotonic() - t0, 1)
    print(json.dumps(summary), flush=True)
    return 0 if summary['ok'] else summary['exit_code']


if __name__ == '__main__':
    sys.exit(main(sys.argv))
