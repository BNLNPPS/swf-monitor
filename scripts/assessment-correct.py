#!/usr/bin/env python
"""Correct a registered AI assessment's text in place, keeping its history.

A scheduled assessment states what was true when it ran; a statement
overtaken later the same day (a fix "still waiting" that was installed an
hour later) stays on the report page and on the dashboard, which reads the
narration from the assessment's ``assessment_register`` record. This
applies exact text replacements to both, so the report reads correctly
where people read it:

- the corun-ai Page (``--group-id``) gets a new published version, the
  correction path corun keeps history for (``src/ai/corun_client.py``,
  ``create_version``): the Markdown content, ``data.narration`` and
  ``data.structured`` (its ``generation`` audit record excepted, which
  stays as the run left it), with ``--note`` inserted after the summary
  paragraph and the correction listed in ``data.corrections``;
- the ``assessment_register`` record (``--record-id``) gets the same
  replacements in its ``narration``, with ``corrected_at`` and
  ``corrected_by``.

Every replacement must match the content at least once, or nothing is
written. Replacements come from a JSON file: a list of ``[old, new]``
pairs. Assessments are otherwise append-only (swf-epicprod
docs/EPICPROD_OPS.md, AI assessments); this is for a report's own text,
on the operator's word.

By hand (docs/SCRIPTS.md):

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../scripts/assessment-correct.py \\
        --group-id <corun page group> --record-id <AppLog id> \\
        --replacements fixes.json --note '**Corrected ...**' --by swf-1 \\
        [--dry-run]
"""
import argparse
import copy
import json
import logging
import os
import sys
import time

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from ai.corun_client import CorunAPIError, CorunClient  # noqa: E402
from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.models import AppLog  # noqa: E402

log = logging.getLogger('assessment-correct')


def _apply(text, pairs, counts):
    for i, (old, new) in enumerate(pairs):
        n = text.count(old)
        if n:
            counts[i] += n
            text = text.replace(old, new)
    return text


def _apply_tree(value, pairs, counts):
    if isinstance(value, str):
        return _apply(value, pairs, counts)
    if isinstance(value, list):
        return [_apply_tree(v, pairs, counts) for v in value]
    if isinstance(value, dict):
        return {k: (v if k == 'generation' else _apply_tree(v, pairs, counts))
                for k, v in value.items()}
    return value


def _insert_note(content, note):
    """The note goes after the summary paragraph (its first blank line)."""
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if line.startswith('**Summary:**'):
            return '\n'.join(lines[:i + 1] + ['', note] + lines[i + 1:])
    raise ValueError('no **Summary:** line to place the note after')


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--group-id', required=True)
    p.add_argument('--record-id', type=int, required=True)
    p.add_argument('--replacements', required=True)
    p.add_argument('--note', required=True)
    p.add_argument('--by', required=True)
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    with open(a.replacements) as f:
        pairs = [tuple(x) for x in json.load(f)]
    client = CorunClient()
    page = client.get_page(a.group_id)
    rec = AppLog.objects.get(id=a.record_id)
    extra = dict(rec.extra_data or {})
    if extra.get('corun_page_group_id') != a.group_id:
        raise SystemExit(f'record {a.record_id} is not this page\'s registration')

    counts = [0] * len(pairs)
    content = _insert_note(_apply(page.get('content') or '', pairs, counts),
                           a.note)
    data = page.get('data') or {}
    changed = {k: _apply_tree(copy.deepcopy(data[k]), pairs, counts)
               for k in ('narration', 'structured') if k in data}
    narration = _apply(extra.get('narration') or '', pairs, counts)
    missing = [old for (old, _new), n in zip(pairs, counts) if not n]
    if missing:
        raise SystemExit(f'not found, nothing written: {missing}')
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    changed['corrections'] = list(data.get('corrections') or []) + [
        {'at': stamp, 'by': a.by, 'note': a.note,
         'replacements': [list(x) for x in pairs]}]

    summary = {'group_id': a.group_id, 'record_id': a.record_id,
               'matches': counts, 'dry_run': a.dry_run}
    if a.dry_run:
        log.info('content would read:\n%s', content)
        log.info('record narration would read: %s', narration)
        print(json.dumps(summary))
        return 0
    try:
        result = client.create_version(
            a.group_id, content=content, title=page.get('title', ''),
            status='published', data=changed)
    except CorunAPIError as exc:
        log_epicprod_action('mcp', 'assessment_corrected',
                            subject_type='campaign',
                            subject_key=extra.get('subject_key', ''),
                            username=a.by, outcome='error', sublevel='normal',
                            message=f'assessment correction failed: {exc}',
                            level=logging.ERROR)
        log.error('corun version failed: %s', exc)
        return 1
    extra['narration'] = narration
    extra['corrected_at'] = stamp
    extra['corrected_by'] = a.by
    rec.extra_data = extra
    rec.save(update_fields=['extra_data'])
    summary['version'] = result.get('version')
    log_epicprod_action('mcp', 'assessment_corrected',
                        subject_type='campaign',
                        subject_key=extra.get('subject_key', ''),
                        username=a.by, outcome='ok', sublevel='normal',
                        message=(f'corrected {extra.get("report_title", a.group_id)}'
                                 f' to version {result.get("version")}'),
                        corun_page_group_id=a.group_id,
                        record_id=a.record_id, note=a.note)
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
