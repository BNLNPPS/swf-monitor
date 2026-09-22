"""The storage door canary's cycle (site-canary docs/STORAGE_DOORS.md):
the doors production writes through are used — a small write, a stat, a
delete, and the date on the certificate they serve — and what they
answer becomes a record production operations can act on.

The cycle runs as the production-operations agent's
``storage_door_cycle`` doer (``scripts/storage-door-cycle.py``), by cron
enqueue every fifteen minutes; a door is probed when its last probe is
older than ``storage_doors.interval_h``, which starts at one hour, so the
cadence is a setting rather than a cron line. The doors come from the
catalog, not from a list here: each RSE's preferred xrootd write
protocol (``canary.doors.write_door``) is the door its writes go
through, so an RSE added to the catalog is probed the day it exists.

Nothing acts on the record automatically. The canary reports and
production operations act (STORAGE_DOORS.md, Nothing acts on it
automatically): a door that changes verdict raises its own action, at
alarm severity when it goes down, and the cycle leaves the cached
product ``storage_door_state`` for the page and the endpoint. The
payload is not tied to this record; it reads its own write door's
certificate itself.

Settings live in SysConfig under ``storage_doors.*``, seeded at their
defaults on first read so every knob is visible on the System page.
"""
import logging
import os
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

STATE_KEY = 'storage_door_state'
STATE_TTL_S = 24 * 3600
MODES = ('shadow', 'live')
DEFAULTS = {
    'enabled': False,
    'mode': 'shadow',
    'interval_h': 1,
    'validity_h': 3,
    'skip_rses': [],
    'probe_prefix': '/canary',
    'probe_name': 'storage-door-probe',
    'timeout_s': 60,
    'warn_days': 7,
    'catalog_url': 'https://rucio-server.jlab.org:443',
    'account': 'eicprod',
}


def _safe(label, fn, fallback):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.exception('storage doors: %s failed', label)
        return fallback(exc) if callable(fallback) else fallback


def setting(key, default):
    from monitor_app.models import SysConfig
    return SysConfig.get_setting(key, default)


def settings():
    out = {key: setting(f'storage_doors.{key}', default)
           for key, default in DEFAULTS.items()}
    skip = out['skip_rses']
    if not (isinstance(skip, (list, tuple)) and all(isinstance(r, str) for r in skip)):
        logger.error('storage_doors.skip_rses is not a list of RSE names: %r; '
                     'probing every RSE', skip)
        out['skip_rses'] = []
    out['interval_h'] = max(0.25, float(out['interval_h'] or 1))
    out['validity_h'] = max(2 * out['interval_h'], float(out['validity_h'] or 3))
    out['timeout_s'] = max(5, int(out['timeout_s'] or 60))
    if out['mode'] not in MODES:
        logger.error('storage_doors.mode is not one of %s: %r; running shadow',
                     MODES, out['mode'])
        out['mode'] = 'shadow'
    return out


def catalog_client(cfg):
    """The catalog, read as the production account through the proxy the
    ops agent holds. Raises when there is no proxy: a cycle that cannot
    read the catalog has no doors to probe and says so."""
    from rucio.client import Client
    proxy = os.environ.get('EVGEN_X509_PROXY', '')
    if not proxy or not os.path.exists(proxy):
        raise RuntimeError('no proxy (EVGEN_X509_PROXY) on this host; '
                           'the catalog was not read')
    base = str(cfg['catalog_url'])
    return Client(rucio_host=base, auth_host=base, account=str(cfg['account']),
                  auth_type='x509_proxy', creds={'client_proxy': proxy},
                  timeout=30)


def catalog_doors(cfg):
    """{RSE: the door its writes go through} from the catalog. An RSE
    whose write protocols are all of a scheme the probe does not speak is
    carried with no door and reported as not probed."""
    from canary import doors as canary_doors
    client = catalog_client(cfg)
    skip = {r.strip() for r in cfg['skip_rses'] if r.strip()}
    out = {}
    for record in client.list_rses():
        rse = record.get('rse') if isinstance(record, dict) else str(record)
        if not rse or rse in skip:
            continue
        door = _safe(f'{rse} protocols',
                     lambda rse=rse: canary_doors.write_door(client.get_protocols(rse)),
                     None)
        out[rse] = door
    return out


def previous_state():
    """The last cycle's record, or an empty one."""
    from monitor_app.models import CachedProduct
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    value = (row.value if row and isinstance(row.value, dict) else {}) or {}
    return value.get('doors') or {}


def is_due(entry, interval_h, now):
    """Whether a door is due a probe: never probed, or last probed longer
    ago than the interval. Pure."""
    if not entry or not entry.get('probed_at'):
        return True
    from django.utils.dateparse import parse_datetime
    last = parse_datetime(str(entry['probed_at']))
    if last is None:
        return True
    if timezone.is_naive(last):
        last = timezone.make_aware(last, timezone.utc)
    return (now - last) >= timedelta(hours=interval_h)


def run_cycle(*, dry_run=False, created_by='storage-doors', force=False,
              only=()):
    """One cycle: the due doors probed, the verdicts recorded, a changed
    verdict announced. Returns (doors, summary)."""
    from canary import doors as canary_doors
    from monitor_app.epicprod_logging import log_epicprod_action

    t0 = timezone.now()
    cfg = settings()
    errors = []

    def failed(label):
        def note(exc):
            errors.append(f'{label}: {type(exc).__name__}: {exc}')
            return None
        return note

    previous = previous_state()
    resolved = _safe('catalog', lambda: catalog_doors(cfg), failed('catalog')) or {}
    if only:
        # A look by hand at named RSEs; the cycle itself takes the catalog.
        wanted = {r.strip() for r in only if r.strip()}
        resolved = {rse: door for rse, door in resolved.items() if rse in wanted}

    readings, state, probed = [], {}, 0
    for rse, door in sorted(resolved.items()):
        was = previous.get(rse) or {}
        if not door:
            state[rse] = {**was, 'rse': rse, 'verdict': 'unknown',
                          'reason': 'no_xrootd_write_door',
                          'evidence': {}, 'door': '', 'probed_at': was.get('probed_at')}
            continue
        if not (force or cfg['enabled']) or not is_due(was, cfg['interval_h'], t0):
            state[rse] = {**was, 'rse': rse, 'door': door['door']}
            continue
        path = canary_doors.probe_path(door['prefix'], cfg['probe_prefix'],
                                       cfg['probe_name'])
        reading = _safe(f'{rse} probe', lambda door=door, path=path: canary_doors.probe_door(
            door['door'], path, timeout_s=cfg['timeout_s']), failed(f'{rse} probe'))
        if reading is None:
            state[rse] = {**was, 'rse': rse, 'door': door['door']}
            continue
        reading['rse'] = rse
        reading['expiry'] = _safe(
            f'{rse} certificate',
            lambda door=door: canary_doors.door_certificate(door['door'], cfg['timeout_s']),
            failed(f'{rse} certificate'))
        readings.append(reading)
        probed += 1

    verdicts = canary_doors.decide_doors(
        readings, {'warn_days': cfg['warn_days']}, t0) if readings else {}
    changes = []
    for reading in readings:
        rse = reading['rse']
        verdict = verdicts[reading['door']]
        was = previous.get(rse) or {}
        state[rse] = {
            'rse': rse, 'door': reading['door'], 'path': reading.get('path'),
            'verdict': verdict['verdict'], 'reason': verdict['reason'],
            'evidence': verdict['evidence'], 'probed_at': t0.isoformat(),
            'since': (was.get('since') if was.get('verdict') == verdict['verdict']
                      else t0.isoformat()),
        }
        if was.get('verdict') and was['verdict'] != verdict['verdict']:
            changes.append((rse, was['verdict'], verdict['verdict'], verdict))
        elif not was.get('verdict') and verdict['verdict'] != 'up':
            changes.append((rse, 'unseen', verdict['verdict'], verdict))

    if not dry_run:
        for rse, before, after, verdict in changes:
            evidence = verdict['evidence']
            severity = {'down': 'alarm', 'unknown': 'warning'}.get(after, 'info')
            _safe(f'{rse} verdict record', lambda rse=rse, before=before, after=after,
                  verdict=verdict, evidence=evidence, severity=severity: log_epicprod_action(
                'storage-doors', 'storage_door_verdict',
                subject_type='storage_rse', subject_key=rse, username=created_by,
                outcome='error' if after == 'down' else 'ok',
                sublevel='high' if after == 'down' else 'normal', live_default=True,
                level=logging.ERROR if after == 'down' else logging.INFO,
                message=(f'storage door {rse} {before} -> {after} '
                         f'({verdict["reason"]})'
                         + (f'; certificate {evidence["certificate"]}, '
                            f'{evidence["certificate_days_left"]} days'
                            if evidence.get('certificate') else '')
                         + (f'; the door said: {evidence["said"]}'
                            if evidence.get('said') else '')),
                severity=severity, verdict=after, was=before,
                reason=verdict['reason'], door=state[rse]['door'],
                **{k: v for k, v in evidence.items() if k != 'said'}),
                failed(f'{rse} verdict record'))

    down = sorted(r for r, s in state.items() if s.get('verdict') == 'down')
    expiring = sorted(r for r, s in state.items()
                      if (s.get('evidence') or {}).get('certificate_expiring'))
    summary = {'cycle_at': t0.isoformat(), 'mode': cfg['mode'], 'enabled': cfg['enabled'],
               'interval_h': cfg['interval_h'], 'rses': len(state), 'probed': probed,
               'down': down, 'expiring': expiring, 'changes': len(changes),
               'errors': errors,
               'duration_s': round((timezone.now() - t0).total_seconds(), 1)}

    if not dry_run:
        payload = {'cycle_at': summary['cycle_at'], 'mode': cfg['mode'],
                   'enabled': cfg['enabled'],
                   'valid_until': (t0 + timedelta(hours=cfg['validity_h'])).isoformat(),
                   'settings': {k: cfg[k] for k in DEFAULTS},
                   'doors': state, 'down': down, 'expiring': expiring,
                   'errors': errors, 'duration_s': summary['duration_s']}
        from monitor_app.cached_product import get_product
        _safe('state store', lambda: get_product(
            STATE_KEY, lambda: payload, ttl_seconds=STATE_TTL_S, refresh=True),
            failed('state store'))
        _safe('cycle record', lambda: log_epicprod_action(
            'storage-doors', 'storage_door_cycle', username=created_by,
            outcome='error' if errors else 'ok',
            duration_ms=int(summary['duration_s'] * 1000),
            sublevel='normal' if (errors or down) else 'low',
            live_default=bool(errors or down),
            level=logging.ERROR if errors else logging.INFO,
            message=(f'storage door cycle ({cfg["mode"]}'
                     f'{"" if cfg["enabled"] else ", off"}): '
                     f'{probed} of {len(state)} doors probed'
                     + (f'; down: {", ".join(down)}' if down else '')
                     + (f'; certificate expiring: {", ".join(expiring)}' if expiring else '')
                     + (f'; {len(changes)} verdict change'
                        f'{"s" if len(changes) != 1 else ""}' if changes else '')
                     + (f'; errors: {"; ".join(errors)}' if errors else '')),
            **{k: v for k, v in summary.items() if k != 'errors'}),
            failed('cycle record'))
    return state, summary
