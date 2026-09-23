"""The Storage doors page (site-canary docs/STORAGE_DOORS.md): what the
doors production writes through answered when the canary last used them
— a small write, a stat, a delete, and the date on the certificate they
serve — with the settings that govern the cycle.

Reads the cached product the cycle stored and computes nothing: it
touches no door, no catalog and no PanDA. Nothing on this page acts,
and the page says so: the canary reports and production operations act.
"""
import logging

from django.shortcuts import render

from monitor_app.panda.storage_doors import DEFAULTS, STATE_KEY

logger = logging.getLogger(__name__)

VERDICT_CLASS = {'up': 'finished_fill', 'down': 'failed_fill', 'unknown': ''}
REASON_LABELS = {
    'wrote': 'wrote, read back and deleted',
    'refused': 'the door refused the write',
    'certificate_expired': 'its certificate has expired',
    'no_answer': 'no answer in time',
    'not_formed': 'no probe could be made',
    'not_permitted': 'the door refused us, not the write',
    'stat_unconfirmed': 'written, but the stat did not confirm it',
    'canary_credential': "the canary's own credential, not the doors",
    'no_xrootd_write_door': 'the catalog gives it no xrootd write door',
}


def _row(rse, state):
    evidence = state.get('evidence') or {}
    days = evidence.get('certificate_days_left')
    return {
        'rse': rse,
        'verdict': state.get('verdict') or 'unknown',
        'verdict_class': VERDICT_CLASS.get(state.get('verdict'), ''),
        'reason': state.get('reason') or '',
        'reason_label': REASON_LABELS.get(state.get('reason'), state.get('reason') or ''),
        'door': state.get('door') or '',
        'path': state.get('path') or evidence.get('path') or '',
        'certificate': evidence.get('certificate') or '',
        'certificate_days': (round(days) if isinstance(days, (int, float)) else None),
        'expiring': bool(evidence.get('certificate_expiring')),
        'expired': isinstance(days, (int, float)) and days <= 0,
        'write_seconds': evidence.get('write_seconds'),
        'said': evidence.get('said') or '',
        'probed_at': state.get('probed_at') or '',
        'since': state.get('since') or '',
    }


def panda_storage_doors(request):
    """The doors, their verdicts and their certificates, as the last
    cycles left them."""
    from monitor_app.models import CachedProduct

    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    state = (row.value if row else None) or {}
    doors = [_row(rse, s) for rse, s in sorted((state.get('doors') or {}).items())]
    settings = state.get('settings') or {}
    return render(request, 'monitor_app/panda_storage_doors.html', {
        'never_run': not state,
        'cycle_at': state.get('cycle_at'),
        'valid_until': state.get('valid_until'),
        'mode': state.get('mode') or DEFAULTS['mode'],
        'enabled': bool(state.get('enabled')),
        'doors': doors,
        'down': state.get('down') or [],
        'expiring': state.get('expiring') or [],
        'probed': [d for d in doors if d['probed_at']],
        'errors': state.get('errors') or [],
        'settings': [{'key': f'storage_doors.{k}', 'value': settings.get(k, v)}
                     for k, v in DEFAULTS.items()],
        'active_nav': {'panda_storage_doors': True},
    })
