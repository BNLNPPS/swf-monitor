"""OSG throttle incidents: a pool our OSG pilots run in stops giving them
new slots for a limit on its own side (docs/OSG_SUBMIT_REPORTER.md,
Fair-share limit).

The submit host reporter's record carries each pool negotiator's last
cycle and whether it held our submitter at its fair-share limit. On each
record this compares the reading with the state kept in PersistentState
and records one action-stream incident per change: ``osg_throttle`` at
warning level when a pool first holds us, and its lift once the pool has
released us for LIFT_READINGS consecutive records, so a limit that flips
between negotiation cycles is one incident rather than one per cycle.
The epicprod-live subscription and a capcom subscription deliver it
(docs/NOTICE_ROUTING.md).
"""
import logging

from monitor_app.short_names import short_name

logger = logging.getLogger(__name__)

ACTION = 'osg_throttle'
STATE_KEY = 'osg_throttle'
LIFT_READINGS = 3


def readings(record):
    """{pool: {'held', 'submitters', 'cycle_at'}} for the pools whose
    negotiators the record read, by short pool name; None when the record
    carries no negotiator reading."""
    negs = (record or {}).get('negotiators')
    if not isinstance(negs, dict) or not negs.get('negotiators'):
        return None
    out = {}
    for n in negs['negotiators']:
        pool = short_name(n.get('pool'))
        entry = out.setdefault(pool, {'held': False, 'submitters': [], 'cycle_at': None})
        if n.get('share_limited'):
            entry['held'] = True
            entry['submitters'] = sorted(set(entry['submitters']) | set(n['share_limited']))
            entry['cycle_at'] = max(filter(None, (entry['cycle_at'], n.get('cycle_at'))),
                                    default=None)
    return out


def observe(record, username=''):
    """Record the incidents this record's reading implies and keep the
    state they are judged against."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import PersistentState

    read = readings(record)
    if read is None:
        return
    state = dict(PersistentState.get_state().get(STATE_KEY) or {})
    changed = False
    for pool, now in sorted(read.items()):
        before = state.get(pool) or {'state': 'clear', 'clear_readings': 0}
        if now['held']:
            if before['state'] != 'held':
                who = ', '.join(now['submitters'])
                text = (f'{pool} negotiator holds our submitter {who} at its fair-share '
                        f'limit: no new OSG slots in {pool} until our usage share falls; '
                        f'running pilots keep theirs.')
                log_epicprod_action(
                    'report', ACTION, subject_type='osg_pool', subject_key=pool,
                    subject_label=pool, username=username, outcome='held',
                    sublevel='high', live_default=True, level=logging.WARNING,
                    message=text, summary=text, severity='warning', state='held',
                    kind='fair_share', submitters=now['submitters'],
                    cycle_at=now['cycle_at'])
                state[pool] = {'state': 'held', 'since': now['cycle_at'], 'clear_readings': 0}
                changed = True
            elif before.get('clear_readings'):
                state[pool] = dict(before, clear_readings=0)
                changed = True
        elif before['state'] == 'held':
            count = int(before.get('clear_readings') or 0) + 1
            if count >= LIFT_READINGS:
                text = (f'{pool} negotiator released our submitter from its fair-share '
                        f'limit (held since {before.get("since") or "unknown"}, clear for '
                        f'{count} consecutive readings).')
                log_epicprod_action(
                    'report', ACTION, subject_type='osg_pool', subject_key=pool,
                    subject_label=pool, username=username, outcome='ok',
                    sublevel='high', live_default=True, message=text, summary=text,
                    severity='info', state='lifted', kind='fair_share',
                    held_since=before.get('since'))
                state[pool] = {'state': 'clear', 'clear_readings': 0}
            else:
                state[pool] = dict(before, clear_readings=count)
            changed = True
    if changed:
        PersistentState.update_state({STATE_KEY: state})
