"""The error-label correction root (docs/ERROR_ATTRIBUTION.md).

Label-reliability rules mark untrustworthy job error labels; every
error-presentation reader applies corrections on the way out through
this module, so a rule added once corrects every surface at the next
read. Recorded history stays raw; the corrected reading leads in
presentation with the original label preserved.

The corrected reading is refined from the matched jobs' payload exit
codes (grade: pilot mechanical fields) — 128+N is termination by
signal N, and the campaign payload's coded exits carry their own
documented meanings.
"""
import json
import logging
import time

logger = logging.getLogger(__name__)


def exit_counts_of(value):
    """The exit-code histogram as a dict. The panda connection returns
    jsonb columns as JSON text; a malformed value reads as empty and
    is logged, never raised."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError as e:
            logger.error('exit-counts JSON parse failed: %s', e)
    return {}

# Payload exit codes with documented meanings (the campaign run.sh
# vocabulary plus the signal convention). Anything above 128 reads as
# a signal termination even without an entry here.
EXIT_READINGS = {
    139: 'payload segfault',
    134: 'payload abort',
    137: 'payload killed',
    143: 'payload terminated',
    78: 'payload Rucio output registration failure',
    65: 'payload validation failure',
}

_RULES_TTL_SECONDS = 60.0
_cache = {'rules': None, 'at': 0.0}


def _rules():
    now = time.monotonic()
    if _cache['rules'] is None or now - _cache['at'] > _RULES_TTL_SECONDS:
        from .models import ErrorCorrectionRule
        try:
            _cache['rules'] = list(
                ErrorCorrectionRule.objects.filter(active=True))
        except Exception as e:
            logger.error('error-correction rules load failed: %s', e)
            return _cache['rules'] or []
        _cache['at'] = now
    return _cache['rules']


def match(component, code, diag):
    """The first active rule matching this label, else None."""
    for rule in _rules():
        try:
            if rule.component != str(component or ''):
                continue
            if int(rule.code) != int(code):
                continue
        except (TypeError, ValueError):
            continue
        if rule.diag_substring and rule.diag_substring not in str(diag or ''):
            continue
        return rule
    return None


def exit_reading(exitcode):
    """The documented reading of one payload exit code, else None."""
    try:
        value = int(exitcode)
    except (TypeError, ValueError):
        return None
    if value in EXIT_READINGS:
        return EXIT_READINGS[value]
    if value > 128:
        return f'payload terminated by signal {value - 128}'
    if value > 0:
        return f'payload failure, exit code {value}'
    return None


def correction(rule, exit_counts=None):
    """The corrected reading for one matched pattern.

    ``exit_counts`` maps the matched jobs' transformation exit codes to
    their counts; the readings of those codes, largest first, are the
    corrected modes. With no readable exit profile the rule's fallback
    label stands.
    """
    profile = []
    for exitcode, value in exit_counts_of(exit_counts).items():
        if isinstance(value, dict):
            n, rep = int(value.get('n') or 0), value.get('rep')
        else:
            n, rep = int(value or 0), None
        profile.append((str(exitcode), n, rep))
    profile.sort(key=lambda t: (-t[1], t[0]))
    modes = []
    for exitcode, n, rep in profile:
        reading = exit_reading(exitcode)
        if reading:
            modes.append({'reading': reading, 'exit_code': exitcode,
                          'count': n, 'rep_pandaid': rep})
    label = (modes[0]['reading'] if modes
             else (rule.corrected_label
                   or 'unreliable label; payload failure of '
                      'undetermined mode'))
    return {
        'label': label,
        'modes': modes,
        'unreliable_label': True,
        'grade': 'pilot mechanical fields (payload exit codes)',
        'note': rule.note or '',
        'evidence_url': rule.evidence_url or '',
    }


def apply_to_summary(entries):
    """Attach ``correction`` to each error-summary entry whose label a
    rule marks unreliable. Entries carry error_source, error_code,
    error_diag, and optionally exit_counts. Decoration must never take
    a page down: a failure on one entry is logged and that entry keeps
    its raw label."""
    for entry in entries:
        try:
            rule = match(entry.get('error_source'),
                         entry.get('error_code'),
                         entry.get('error_diag'))
            if rule is not None:
                entry['correction'] = correction(
                    rule, entry.get('exit_counts') or {})
        except Exception as e:
            logger.error('error-correction decoration failed for '
                         '%s:%s: %s', entry.get('error_source'),
                         entry.get('error_code'), e)
    return entries
