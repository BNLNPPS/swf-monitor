"""Human-facing presentation of recorded log content; no record mutations."""
import json
import math


TECHNICAL_KEYS = {
    'action', 'outcome', 'verdict', 'severity', 'summary', 'narration', 'reason',
    'subject_type', 'subject_key', 'username', 'duration_ms', 'sublevel',
    'live_default', 'notify', 'registered', 'corun_page_group_id', 'title',
    'subject_label',
}
LABELS = {'noise': 'Channel noise', 'real_problems': 'Recurring problems'}


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _label(key):
    key = str(key)
    if key not in LABELS and key.endswith('_s'):
        key = key[:-2]
    return LABELS.get(key, key.replace('_', ' ').capitalize())


def _duration(seconds):
    """A count of seconds in words: 59 s, 14 min, 2 h 5 min."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f'{seconds} s'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes} min'
    return f'{minutes // 60} h' + (f' {minutes % 60} min' if minutes % 60 else '')


def _value(value, key=''):
    """Keep mappings and lists readable without Python repr or lost values."""
    if isinstance(value, dict):
        return {'kind': 'mapping', 'items': [
            {'label': _label(k), 'value': _value(v, k)} for k, v in value.items()
        ]}
    if isinstance(value, list):
        return {'kind': 'list', 'items': [_value(v) for v in value]}
    link = key if key in {'execution_id', 'run_id'} and value and not isinstance(value, bool) else ''
    if (str(key).endswith('_s') and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value)):
        value = _duration(value)
    elif isinstance(value, bool):
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
        # A record that carries its own title is shown under it, as on the
        # live screen (live_notices.board_text): what happened and to what.
        'title': _text(extra.get('title')) or (_label(action) if action else 'Log entry'),
        'summary': summary, 'passages': passages, 'fields': fields,
        'state': state, 'duration': duration,
        # The subject's own name when it carries one ("Pressure front"), else
        # its type and key ("Task: 41474").
        'subject': _text(extra.get('subject_label')) or _text(extra.get('subject_key')),
        'subject_type': ('' if _text(extra.get('subject_label'))
                         else _label(extra.get('subject_type', ''))),
        'username': _text(extra.get('username')),
        'raw_data': json.dumps(log.extra_data, indent=2, ensure_ascii=False, default=str),
    }
