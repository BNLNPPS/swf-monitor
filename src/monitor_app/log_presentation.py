"""Human-facing presentation of recorded log content; no record mutations."""
import json
import math


TECHNICAL_KEYS = {
    'action', 'outcome', 'verdict', 'severity', 'summary', 'narration', 'reason',
    'subject_type', 'subject_key', 'username', 'duration_ms', 'sublevel',
    'live_default', 'notify', 'registered', 'corun_page_group_id',
}
LABELS = {'noise': 'Channel noise', 'real_problems': 'Recurring problems'}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _label(key):
    return LABELS.get(key, str(key).replace('_', ' ').capitalize())


def _value(value, key=''):
    """Keep mappings and lists readable without Python repr or lost values."""
    if isinstance(value, dict):
        return {'kind': 'mapping', 'items': [
            {'label': _label(k), 'value': _value(v, k)} for k, v in value.items()
        ]}
    if isinstance(value, list):
        return {'kind': 'list', 'items': [_value(v) for v in value]}
    link = key if key in {'execution_id', 'run_id'} and value and not isinstance(value, bool) else ''
    if isinstance(value, bool):
        value = 'Yes' if value else 'No'
    elif value is None:
        value = 'Not recorded'
    return {'kind': 'text', 'text': value, 'link': link}


def present_log(log):
    extra = log.extra_data if isinstance(log.extra_data, dict) else {}
    # Old interrupted cycles recorded only a signal/timeout. Explain that
    # gap without attributing them to a door from the current cache.
    if (extra.get('action') == 'storage_door_cycle'
            and extra.get('outcome') in ('error', 'timeout')
            and (extra.get('outcome') == 'timeout'
                 or _text(extra.get('reason')).startswith('killed by signal '))
            and not _text(extra.get('summary'))):
        from .panda.storage_doors import failure_context
        extra = {**failure_context('', _text(extra.get('reason')) or 'cause not recorded'),
                 **extra}
    summary = (_text(extra.get('summary')) or _text(extra.get('narration'))
               or _text(log.message))
    passages = []
    seen = {' '.join(summary.split())}
    for key, label in [('reason', 'Reason'), ('narration', 'Details')]:
        text = _text(extra.get(key))
        normalized = ' '.join(text.split())
        if text and normalized not in seen:
            passages.append({'label': label, 'text': text})
            seen.add(normalized)
    fields = [
        {'label': _label(key), 'value': _value(value, key)}
        for key, value in extra.items()
        if key not in TECHNICAL_KEYS and value not in (None, '', [], {})
    ]
    duration = extra.get('duration_ms')
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration):
        duration = f'{duration:g} ms' if duration < 1000 else f'{duration / 1000:g} s'
    else:
        duration = ''
    action = _text(extra.get('action'))
    state = (_text(extra.get('verdict')) or _text(extra.get('severity'))
             or _text(extra.get('outcome')))
    return {
        'title': _label(action) if action else 'Log entry',
        'summary': summary, 'passages': passages, 'fields': fields,
        'state': state, 'duration': duration,
        'subject': _text(extra.get('subject_key')),
        'subject_type': _label(extra.get('subject_type', '')),
        'username': _text(extra.get('username')),
        'raw_data': json.dumps(log.extra_data, indent=2, ensure_ascii=False, default=str),
    }
