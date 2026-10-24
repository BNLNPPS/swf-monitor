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
import re
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
    143: 'payload stopped by SIGTERM (15); reason not established',
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


def _like(pattern, value):
    """SQL LIKE semantics: % any run, _ one character, whole value."""
    regex = ''.join('.*' if ch == '%' else '.' if ch == '_' else re.escape(ch)
                    for ch in pattern)
    return re.fullmatch(regex, str(value or '')) is not None


def match(component, code, diag, sites=None):
    """The first active rule matching this label, else None. A rule with a
    queue_scope applies only when every site in ``sites`` (the job's
    computingsite, or a summary entry's sites) matches the scope."""
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
        if rule.queue_scope:
            in_scope = [s for s in (sites or []) if s]
            if not in_scope or not all(_like(rule.queue_scope, s) for s in in_scope):
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
            held = value.get('held')
        else:
            n, rep, held = int(value or 0), None, None
        profile.append((str(exitcode), n, rep, held))
    profile.sort(key=lambda t: (-t[1], t[0]))
    modes = []
    for exitcode, n, rep, held in profile:
        reading = exit_reading(exitcode)
        if reading:
            # held: the mode's core-seconds when the profile carries
            # them (the Snapper breakdown's patterns do), else None.
            modes.append({'reading': reading, 'exit_code': exitcode,
                          'count': n, 'rep_pandaid': rep,
                          'held': held})
    label = (modes[0]['reading'] if modes
             else (getattr(rule, 'corrected_label', '')
                   or 'unreliable label; payload failure of '
                      'undetermined mode'))
    return {
        'label': label,
        'modes': modes,
        'unreliable_label': True,
        'grade': 'pilot mechanical fields (payload exit codes)',
        'note': getattr(rule, 'note', '') or '',
        'evidence_url': getattr(rule, 'evidence_url', '') or '',
    }


def reading(component, code, diag, exit_counts=None, sites=None):
    """Shared, evidence-bounded reading of a reported error.

    Decode documented exits and explicit diagnostics. Unknown causes stay
    unknown; a warning copied into a pilot label is not a causal verdict.
    """
    rule = match(component, code, diag, sites=sites)
    if rule is not None:
        return correction(rule, exit_counts)
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    diag = str(diag or '')
    lower = diag.lower()
    label = ''
    if component in ('executor', 'transformation'):
        label = exit_reading(code) or ''
        if code == 1:
            label = 'Payload failed (exit 1); exit code alone does not identify the cause'
    elif component == 'pilot' and code in (1305, 1315):
        modes = correction(None, exit_counts)
        if modes['modes']:
            modes['note'] = 'Payload exit codes take precedence over incidental wrapper warnings.'
            return modes
        if 'unknown groupid' in lower or 'bind mount' in lower:
            label = 'Execution failure; container warning does not establish the payload cause'
    elif component == 'ddm' and 'bytes mismatch' in lower:
        kind = 'Log file' if '.log.tgz' in lower or '.log.tar' in lower else 'File'
        label = kind + ' catalog registration refused: registered and submitted byte sizes differ'
    elif component == 'pilot' and code == 1212:
        label = ('Payload process killed by its memory limit' if 'out of memory' in lower
                 else 'Pilot reported payload memory exhaustion')
    elif component == 'pilot' and code == 1213:
        label = 'Job stopped at its runtime limit'
    elif component == 'pilot' and code == 1326:
        label = 'Input staging failed: no allowed input replica found'
        if 'allow_wan=false' in lower:
            label += '; remote (WAN) access was disabled'
    elif component == 'pilot' and code == 1152 and 'stage-out' in lower:
        kind = 'Log' if '.log.tgz' in lower or '.log.tar' in lower else 'Output'
        label = kind + ' transfer timed out during stage-out'
    elif component == 'dispatcher' and code == 100 and 'lost heartbeat' in lower:
        label = 'PanDA lost the job heartbeat; the reason contact stopped is unknown'
    elif component in ('supervisor', 'taskbuffer') and 'disappeared from remote schedd' in lower:
        label = 'Worker disappeared from the remote batch scheduler; Harvester cancelled it'
        if 'while the job was starting' in lower:
            label += ' before the payload started'
        elif 'while the job was running' in lower:
            label += ' while the payload was running'
        label += '; underlying batch cause not reported'
    elif component == 'taskbuffer' and code == 300 and re.search(r':\s*None\s*$', diag):
        label = 'Batch worker ended while the job was running; batch reason was not recorded'
    if not label:
        return None
    return {'label': label, 'modes': [], 'unreliable_label': False,
            'grade': 'reported diagnostic or documented exit code',
            'note': '', 'evidence_url': ''}


def apply_to_summary(entries):
    """Attach ``correction`` to each error-summary entry whose label a
    rule marks unreliable. Entries carry error_source, error_code,
    error_diag, and optionally exit_counts. Decoration must never take
    a page down: a failure on one entry is logged and that entry keeps
    its raw label."""
    evidence = {}
    for entry in entries:
        try:
            interpreted = reading(entry.get('error_source'),
                                  entry.get('error_code'), entry.get('error_diag'),
                                  entry.get('exit_counts'), sites=entry.get('sites'))
            if interpreted is not None:
                entry['correction'] = interpreted
            # Only generic payload labels receive representative-log evidence.
            # A witness explains its own job; it does not relabel every job
            # sharing an exit code or assign a payload cause to worker losses.
            if (entry.get('error_source'), entry.get('error_code')) in {('executor', 1), ('pilot', 1305)}:
                from .epicprod_inventory import cached_payload_log_parts, diagnosis_from_log_texts
                for sample in (entry.get('site_task_counts') or [])[:3]:
                    job = sample.get('representative_pandaid')
                    task = sample.get('taskid')
                    if not job or not task:
                        continue
                    if job not in evidence:
                        parts = cached_payload_log_parts(task, job)
                        evidence[job] = diagnosis_from_log_texts([p['text'] for p in parts]) if parts else {}
                    diagnosis = evidence[job]
                    if diagnosis.get('operation') == 'invalid_event_record':
                        entry['payload_evidence'] = {'pandaid': job, 'taskid': task,
                            'summary': diagnosis['failure_summary'], 'grade': 'payload log'}
                        break
        except Exception as e:
            logger.error('error-correction decoration failed for '
                         '%s:%s: %s', entry.get('error_source'),
                         entry.get('error_code'), e)
    return entries
