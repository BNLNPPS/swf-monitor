#!/usr/bin/env python3
"""segfault-nightly-notice.py — the catalog tells the swf sessions what it
cannot decide by itself.

The nightly catalog_sync chain's last segfault step (swf-epicprod
docs/SEGFAULT_DIAGNOSIS.md, Reproduction): after the inventory and the
automatic dig, one TJAI peer message to the swf sessions on this host
(the host:swf-testbed group) carrying the census of the catalog and the
signatures that need a decision a person or an LLM makes: the ones with
no trace and a runnable row and no attempt in flight (a reproduction is
the only extraction left), and the traced ones no finding reads. A
session takes an item by requesting the reproduction on the record
(the signature page's Reproduce, or the MCP tool
panda_segfault_reproduce), which every session sees on the runs page,
so two never submit the same row. Nothing is submitted here.

The sender is a registered TJAI session of its own (client epicprod,
name epicprod-nightly), stable across runs. Registration and sending
use the tjai MCP service over HTTPS with TJAI_MCP_TOKEN from ~/.env of
the running account, the route tjrepo's scripts/mcp_call.py uses.

Django-bootstrap standalone script, by hand::

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/segfault-nightly-notice.py [--dry-run] [--limit N]

Prints one JSON line: the census, the listed keys and the message id
(or the message text with --dry-run). Exit 1 when the send failed.
"""
import argparse
import json
import os
import socket
import sys
import urllib.request
import uuid

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from monitor_app.models import CrashSignature  # noqa: E402
from monitor_app.reproductions import attempts  # noqa: E402
from monitor_app.segfaults import covering_finding, signature_summary  # noqa: E402

TJAI_MCP_URL = os.environ.get('TJAI_MCP_URL', 'https://etaverse.com/tjai/mcp/')
LOCATION = os.environ.get('TJAI_LOCATION_NAME', 'swf-testbed')
RESOURCE = f'host:{LOCATION}'
SENDER = {'client': 'epicprod', 'name': 'epicprod-nightly',
          'native_id': 'epicprod-segfault-nightly-notice'}
SETTLED = {'diagnosed', 'handed_off', 'fixed', 'accepted'}
CATALOG_URL = 'https://epic-devcloud.org/prod/panda/segfaults/'


def _token():
    tok = os.environ.get('TJAI_MCP_TOKEN')
    if tok:
        return tok
    path = os.path.expanduser('~/.env')
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('export '):
                line = line[7:]
            if line.startswith('TJAI_MCP_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f'TJAI_MCP_TOKEN not in the environment or {path}')


def mcp_call(tool, arguments, timeout=60):
    """One tjai MCP tool call; the text result, or RuntimeError."""
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}
    req = urllib.request.Request(
        TJAI_MCP_URL, data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': f'Bearer {_token()}',
                 'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    if 'error' in resp:
        raise RuntimeError(f"{tool}: MCP error {resp['error']}")
    res = resp['result']
    text = ''.join(c.get('text', '') for c in res.get('content', []))
    if res.get('isError'):
        raise RuntimeError(f'{tool}: tool error {text[:500]}')
    return text


def census():
    """The catalog read for the notice: counts and the two decision lists."""
    active = {}
    for a in attempts():
        if a.get('active'):
            active.setdefault(a['signature'], 0)
            active[a['signature']] += 1
    sigs = list(CrashSignature.objects.all())
    counts = {'signatures': len(sigs), 'crashes': 0, 'covered': 0, 'covered_crashes': 0,
              'traced_unread': 0, 'untraced_runnable': 0, 'untraced_unrunnable': 0,
              'in_flight': 0}
    need_run, need_read = [], []
    for sig in sigs:
        if sig.level == 'trace':
            continue
        s = signature_summary(sig)
        counts['crashes'] += s['crashes'] or 0
        # In flight is counted whatever the signature's reading: a covered
        # signature's verifying run is a run.
        if active.get(sig.key):
            counts['in_flight'] += 1
        fid = covering_finding(sig)
        if fid or sig.status in SETTLED:
            counts['covered'] += 1
            counts['covered_crashes'] += s['crashes'] or 0
            continue
        if active.get(sig.key):
            continue
        row = {'key': sig.key, 'class': s['class'], 'crashes': s['crashes'],
               'queue': ', '.join(s['site_names'][:2]), 'last_seen': (s['last_seen'] or '')[:10],
               'frame': s['frame'], 'trace_status': s['trace_status'],
               'runnable': s['runnable']}
        if s['trace_status'] == 'found':
            counts['traced_unread'] += 1
            need_read.append(row)
        elif s['runnable']:
            counts['untraced_runnable'] += 1
            need_run.append(row)
        else:
            counts['untraced_unrunnable'] += 1
    need_run.sort(key=lambda r: -(r['crashes'] or 0))
    need_read.sort(key=lambda r: -(r['crashes'] or 0))
    return counts, need_run, need_read


def compose(counts, need_run, need_read, limit):
    c = counts
    lines = [
        f"Segfault catalog, nightly: {c['signatures']} signatures, {c['crashes']:,} crashes; "
        f"{c['covered']} read by a finding ({c['covered_crashes']:,} crashes); "
        f"{c['in_flight']} with a reproduction in flight; "
        f"{c['untraced_runnable']} with no trace and a runnable row; "
        f"{c['traced_unread']} traced with no reading; "
        f"{c['untraced_unrunnable']} with no trace and no runnable row (manifest reconstruction).",
    ]
    if need_run:
        lines.append(f"Reproduction is the only extraction left for these (largest first, {min(len(need_run), limit)} of {len(need_run)}):")
        for r in need_run[:limit]:
            lines.append(f"  {r['key']}  {r['class']}  {r['crashes']:,} crashes  {r['queue']}  last {r['last_seen']}")
    if need_read:
        lines.append(f"Traced, no finding reads them ({min(len(need_read), limit)} of {len(need_read)}):")
        for r in need_read[:limit]:
            lines.append(f"  {r['key']}  {r['class']}  {r['crashes']:,} crashes  {r['frame'][:70]}")
    lines.append(
        "Someone swf take it: a reproduction is requested on the record with panda_segfault_reproduce(key) "
        "or the signature page's Reproduce; the runs page shows what is already requested, so check it "
        f"before submitting. {CATALOG_URL}")
    return '\n'.join(lines)


def send(text):
    reg = json.loads(mcp_call('register_session', dict(
        SENDER, host=LOCATION, cwd=os.getcwd(), delivery='pull', state='idle',
        model='', resources=[RESOURCE])))
    sender_id = reg.get('id') or reg.get('session_id')
    if not sender_id:
        raise RuntimeError(f'register_session returned no id: {str(reg)[:300]}')
    message_id = str(uuid.uuid4())
    mcp_call('send_message', {'sender_id': sender_id, 'resource': RESOURCE,
                              'message_id': message_id, 'content': text,
                              'reply_requested': False})
    return sender_id, message_id


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true', help='compose and print, send nothing')
    ap.add_argument('--limit', type=int, default=12, help='signatures listed per list')
    args = ap.parse_args()
    counts, need_run, need_read = census()
    text = compose(counts, need_run, need_read, args.limit)
    out = {'counts': counts, 'listed_run': [r['key'] for r in need_run[:args.limit]],
           'listed_read': [r['key'] for r in need_read[:args.limit]],
           'host': socket.gethostname()}
    if args.dry_run:
        out['message'] = text
        print(json.dumps(out))
        return 0
    try:
        sender_id, message_id = send(text)
    except Exception as exc:                                  # noqa: BLE001
        out['error'] = str(exc)[:500]
        print(json.dumps(out))
        return 1
    out.update(sender_id=sender_id, message_id=message_id, resource=RESOURCE)
    print(json.dumps(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
