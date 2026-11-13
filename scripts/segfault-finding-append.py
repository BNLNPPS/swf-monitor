#!/usr/bin/env python3
"""segfault-finding-append.py — add a reading to an existing segfault
finding without re-sending its text: a paragraph appended to its reading
(``what``), a dated line appended to its notes, signatures and sources
added (each once), a sentence of the reading replaced where it is, and
the title replaced. Writes through ``segfaults.set_finding``, so the
finding keeps its version history and the write is an action on the
stream (swf-epicprod docs/SEGFAULT_DIAGNOSIS.md, Findings). A new
finding is written with the MCP tool ``panda_segfault_finding_set``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ~/.env
    /data/wenauseic/github/swf-testbed/.venv/bin/python \\
        ../scripts/segfault-finding-append.py --finding f-23 \\
        --paragraph-file reading.txt --notes-file notes.txt \\
        --add-signatures memory:task38538 memory:task38558 \\
        --add-source 'Signature memory:task38538' https://... \\
        --sources-file sources.json \\
        --replace-file replace.json --title '...' --by swf-1 [--apply]

``--replace-file`` holds a JSON list of [old, new] pairs; each old text
must occur exactly once in the reading. Dry run by default: the changes
are printed and nothing is written.
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

from monitor_app import segfaults  # noqa: E402
from monitor_app.models import Entry  # noqa: E402


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read().strip()


def _entry(ref):
    """The finding named ``ref``, or numbered by it (f-23 or 23)."""
    ctx = segfaults._finding_context()
    qs = Entry.objects.filter(kind=segfaults.FINDING_KIND, context=ctx)
    entry = qs.filter(name=ref).first()
    if entry is not None:
        return entry
    serial = ref[2:] if ref.lower().startswith('f-') else ref
    if serial.isdigit():
        for e in qs.only('name', 'data', 'content', 'title'):
            if int((e.data or {}).get('serial') or 0) == int(serial):
                return e
    sys.exit(f'no finding {ref!r}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--finding', required=True, help='f-N, N or the finding name')
    ap.add_argument('--paragraph-file', help='a paragraph appended to the reading')
    ap.add_argument('--notes-file', help='a line appended to the notes')
    ap.add_argument('--add-signatures', nargs='*', default=[])
    ap.add_argument('--add-source', nargs=2, action='append', default=[],
                    metavar=('LABEL', 'URL'))
    ap.add_argument('--sources-file', help='JSON [{"label": ..., "url": ...}, ...] added')
    ap.add_argument('--replace-file', help='JSON [[old, new], ...] in the reading')
    ap.add_argument('--title')
    ap.add_argument('--by', required=True, help='who writes (the version stamp)')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    entry = _entry(args.finding)
    data = entry.data or {}
    fid = f"f-{data.get('serial')}"
    fields = {}

    what = entry.content or ''
    if args.replace_file:
        for old, new in json.loads(_read(args.replace_file)):
            n = what.count(old)
            if n != 1:
                sys.exit(f'{fid}: replace text occurs {n} times, not once: {old[:80]!r}')
            what = what.replace(old, new)
    if args.paragraph_file:
        what = what.rstrip() + '\n\n' + _read(args.paragraph_file)
    if what != (entry.content or ''):
        fields['what'] = what

    if args.notes_file:
        fields['notes'] = ((data.get('notes') or '').rstrip() + '\n\n' + _read(args.notes_file)).strip()

    sigs = list(data.get('signatures') or [entry.name])
    added = [k for k in args.add_signatures if k not in sigs]
    if added:
        fields['signatures'] = sigs + added

    sources = list(data.get('sources') or [])
    urls = {s.get('url') for s in sources}
    wanted = [{'label': label, 'url': url} for label, url in args.add_source]
    if args.sources_file:
        wanted += json.loads(_read(args.sources_file))
    new_sources = []
    for s in wanted:
        if s['url'] not in urls:
            urls.add(s['url'])
            new_sources.append({'label': s['label'], 'url': s['url']})
    if new_sources:
        fields['sources'] = sources + new_sources

    if args.title and args.title != entry.title:
        fields['title'] = args.title

    print(f'{fid} ({entry.name}): {", ".join(sorted(fields)) or "no change"}')
    if 'what' in fields:
        print(f'  reading {len(entry.content or "")} -> {len(fields["what"])} chars')
    if added:
        print(f'  signatures added: {added}')
    if new_sources:
        print(f'  sources added: {[s["label"] for s in new_sources]}')
    if 'title' in fields:
        print(f'  title: {fields["title"]}')
    if not fields:
        return 0
    if not args.apply:
        print('dry run: nothing written (--apply to write)')
        return 0
    segfaults.set_finding(entry.name, fields, changed_by=args.by)
    print('written')
    return 0


if __name__ == '__main__':
    sys.exit(main())
