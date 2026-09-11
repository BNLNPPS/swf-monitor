"""The segfault catalog: crash signatures from the crash inventory.

swf-epicprod docs/SEGFAULT_DIAGNOSIS.md. The inventory
(scripts/segfault-inventory.py) writes one EpicProdJob row per payload
crash; this module groups them into CrashSignature rows at record level
(exit code x PanDA task), reads the class from the record, and serves
the catalog to the page, the MCP tools and the REST endpoints from
swfdb alone. Nothing here reaches the PanDA database except
``task_totals``, which the nightly builder calls; no page render path
does.

Class reading, thresholds in SysConfig (``segfault_class_*``):

- ``abort``: exit 134, whatever the rate;
- ``configuration_dead``: crash rate at or above ``dead_rate``;
- ``storm``: rate at or above ``storm_rate`` and the interquartile
  range of time to death under ``storm_iqr`` of the median;
- ``sparse``: rate under ``sparse_rate``;
- ``mixed`` otherwise, a class to look at rather than a verdict.
"""
import logging
from collections import defaultdict
from datetime import datetime, timezone as dt_timezone

from django.db import connections
from django.utils import timezone

from .epicprod_inventory import TRACE_MAX_FRAMES
from .models import CrashSignature, EpicProdJob, SysConfig

logger = logging.getLogger(__name__)

PHASE = 'payload_crash'
SIGNAL_NAMES = {6: 'SIGABRT', 7: 'SIGBUS', 8: 'SIGFPE', 11: 'SIGSEGV'}
CLASS_LABELS = {
    'storm': 'Storm', 'configuration_dead': 'Config dead',
    'sparse': 'Sparse', 'abort': 'Abort', 'mixed': 'Mixed',
}
STAGE_LABELS = {'simulation': 'simu', 'reconstruction': 'reco'}
CLASS_ORDER = ['storm', 'configuration_dead', 'mixed', 'sparse', 'abort']
STATUS_LABELS = dict(CrashSignature.STATUSES)

# Fields the record pass owns; everything else on a signature (status,
# verdict, trace, reproduction, assessments, package) belongs to the
# later stages and survives a rebuild.
RECORD_FIELDS = ['level', 'exit_code', 'signal', 'class_hint', 'tasks',
                 'configuration', 'sites', 'crashes', 'rate', 'minutes_p10',
                 'minutes_p50', 'first_seen', 'last_seen', 'rows_lost',
                 'events_lost', 'data', 'updated_at']

THRESHOLD_DEFAULTS = {
    'segfault_class_dead_rate': 0.95,
    'segfault_class_storm_rate': 0.20,
    'segfault_class_storm_iqr': 0.20,
    'segfault_class_sparse_rate': 0.05,
}


def thresholds():
    """The class thresholds, each seeded into SysConfig at its default on
    first read, so every knob is on the System page."""
    out = {}
    for key, default in THRESHOLD_DEFAULTS.items():
        value = SysConfig.get_setting(key, default)
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            logger.error('SysConfig %s=%r is not a number; using %s',
                         key, value, default)
            out[key] = default
    return out


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * p
    lo, hi = int(pos), min(int(pos) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 1)


def classify(exit_code, rate, minutes, th):
    """The class reading from the record: exit code, crash rate and the
    spread of time to death."""
    if exit_code == 134:
        return 'abort'
    if rate is None:
        return 'mixed'
    if rate >= th['segfault_class_dead_rate']:
        return 'configuration_dead'
    p25, p50, p75 = (_percentile(minutes, 0.25), _percentile(minutes, 0.5),
                     _percentile(minutes, 0.75))
    if (rate >= th['segfault_class_storm_rate'] and p50
            and (p75 - p25) < th['segfault_class_storm_iqr'] * p50):
        return 'storm'
    if rate < th['segfault_class_sparse_rate']:
        return 'sparse'
    return 'mixed'


# ------------------------------------------------------------- the record

def task_totals(jeditaskids):
    """{jeditaskid: {'finished': n, 'failed': n}} over the tasks' whole
    life from the PanDA record; the builder's call, never a page's."""
    from .panda.constants import PANDA_SCHEMA
    ids = sorted({int(t) for t in jeditaskids if t})
    if not ids:
        return {}
    out = defaultdict(lambda: {'finished': 0, 'failed': 0})
    with connections['panda'].cursor() as cur:
        cur.execute(
            f"""SELECT "jeditaskid", "jobstatus", count(*)
                FROM "{PANDA_SCHEMA}"."jobsarchived4"
                WHERE "jeditaskid" = ANY(%s) AND "jobstatus" IN ('finished', 'failed')
                GROUP BY 1, 2""", [ids])
        for tid, status, n in cur.fetchall():
            out[int(tid)][status] = int(n)
    return dict(out)


def _configuration(prod_task, payload_version=''):
    """The configuration block of a task's signature."""
    if prod_task is None:
        return {}
    dataset = getattr(prod_task, 'dataset', None)
    config = getattr(prod_task, 'prod_config', None)
    campaign = getattr(prod_task, 'campaign', None) or getattr(dataset, 'campaign', None)
    physics = getattr(dataset, 'physics_tag', None)
    return {
        'prod_task': prod_task.name,
        'campaign': getattr(campaign, 'name', '') if campaign else '',
        'composed_name': getattr(dataset, 'composed_name', '') if dataset else '',
        'process': getattr(physics, 'name', '') if physics else '',
        'prod_config_id': getattr(config, 'id', None) if config else None,
        'prod_config': getattr(config, 'name', '') if config else '',
        'container_image': getattr(config, 'container_image', '') if config else '',
        'detector_version': getattr(dataset, 'detector_version', '') if dataset else '',
        'payload_version': payload_version or '',
    }


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


def _parse_dt(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def _row_key(row):
    file_col = row.get('file') or ''
    head, _, stem = file_col.rpartition('/')
    return head, stem, f"{int(row.get('ichunk') or 0):04d}"


def _rows_lost(prod_task, rows):
    """(rows_lost, events_lost, note) for the crashed rows of a
    configuration: those with no delivered RECO output in any attempt,
    read through the delivery lookup the residual rerun uses. A lookup
    that cannot be made is reported, never guessed."""
    if prod_task is None:
        return None, None, 'no PCS task'
    if not rows:
        return None, None, 'rows unresolved'
    try:
        from pcs.commands import _delivered_row_keys
        keys, dids, _arrival = _delivered_row_keys(prod_task)
    except Exception as e:                                    # noqa: BLE001
        logger.error('segfault rows_lost %s: delivery lookup failed: %s',
                     prod_task.name, e)
        return None, None, f'delivery lookup failed: {str(e)[:120]}'
    if keys is None:
        return None, None, 'no recorded RECO outputs'
    lost = [r for r in rows.values() if _row_key(r) not in keys]
    return (len(lost), sum(int(r.get('nevents') or 0) for r in lost),
            f'{len(dids)} RECO dataset(s) checked')


def build_record_signatures(jeditaskids=None, rows_lost=True,
                            rows_lost_max_tasks=50):
    """Group the crash inventory into record-level signatures and upsert
    them. ``jeditaskids`` limits the pass to those tasks (the nightly
    top-up); None rebuilds every task in the inventory. Returns a
    summary dict."""
    from pcs.models import PandaTasks
    th = thresholds()
    qs = EpicProdJob.objects.filter(phase=PHASE)
    if jeditaskids:
        qs = qs.filter(jeditaskid__in=[int(t) for t in jeditaskids])
    groups = {}
    for job in qs.only('pandaid', 'jeditaskid', 'prod_task_id', 'seq_number',
                       'data').iterator(chunk_size=5000):
        crash = (job.data or {}).get('crash') or {}
        exit_code = int(crash.get('exit_code') or 0)
        tid = int(job.jeditaskid) if job.jeditaskid else 0
        g = groups.get((exit_code, tid))
        if g is None:
            g = groups[(exit_code, tid)] = {
                'exit_code': exit_code, 'jeditaskid': tid,
                'taskname': crash.get('taskname') or '',
                'prod_task_id': job.prod_task_id, 'crashes': 0,
                'minutes': [], 'sites': defaultdict(lambda: {'crashes': 0, 'hosts': set()}),
                'first': None, 'last': None, 'rows': {}, 'unresolved_rows': 0,
                'stages': defaultdict(int), 'payload_version': '',
            }
        g['crashes'] += 1
        if crash.get('minutes') is not None:
            g['minutes'].append(float(crash['minutes']))
        site = g['sites'][crash.get('computingsite') or 'unknown']
        site['crashes'] += 1
        if crash.get('modificationhost'):
            site['hosts'].add(crash['modificationhost'])
        seen = _parse_dt(crash.get('endtime') or crash.get('modificationtime'))
        if seen:
            g['first'] = seen if not g['first'] or seen < g['first'] else g['first']
            g['last'] = seen if not g['last'] or seen > g['last'] else g['last']
        if crash.get('row'):
            g['rows'][job.seq_number] = crash['row']
        else:
            g['unresolved_rows'] += 1
        if crash.get('stage'):
            g['stages'][crash['stage']] += 1
        digest = crash.get('digest') or {}
        if digest.get('version') and not g['payload_version']:
            g['payload_version'] = digest['version']
        if crash.get('container') and not g.get('container'):
            g['container'] = crash['container']

    totals = task_totals([g['jeditaskid'] for g in groups.values()])
    prod_tasks = {}
    payload_versions = {}
    for pt in PandaTasks.objects.filter(
            jedi_task_id__in=[g['jeditaskid'] for g in groups.values()]
    ).select_related('prod_task', 'prod_task__dataset', 'prod_task__prod_config',
                     'prod_task__dataset__physics_tag', 'prod_task__campaign',
                     'prod_task__dataset__campaign'):
        prod_tasks[pt.jedi_task_id] = pt.prod_task
        payload_versions[pt.jedi_task_id] = (pt.metadata or {}).get('payload_version', '')

    # Loss is a delivery lookup per configuration; the largest signatures
    # first, bounded per pass.
    lost_budget = rows_lost_max_tasks if rows_lost else 0
    summary = {'signatures': 0, 'created': 0, 'classes': defaultdict(int),
               'rows_lost_checked': 0}
    now = timezone.now()
    for (exit_code, tid), g in sorted(groups.items(),
                                      key=lambda kv: -kv[1]['crashes']):
        tot = totals.get(tid, {'finished': 0, 'failed': 0})
        denom = tot['finished'] + tot['failed']
        rate = g['crashes'] / denom if denom else None
        class_hint = classify(exit_code, rate, g['minutes'], th)
        prod_task = prod_tasks.get(tid)
        key = f'exit{exit_code}:task{tid}'
        sig = CrashSignature.objects.filter(key=key).first()
        created = sig is None
        if created:
            sig = CrashSignature(key=key, status='new')
        lost_note = (sig.data or {}).get('rows_lost_note') or 'not checked'
        lost, events_lost = sig.rows_lost, sig.events_lost
        if lost_budget > 0 and prod_task is not None and g['rows']:
            lost, events_lost, lost_note = _rows_lost(prod_task, g['rows'])
            lost_budget -= 1
            summary['rows_lost_checked'] += 1
        elif prod_task is None:
            lost, events_lost, lost_note = None, None, 'no PCS task'
        elif not g['rows']:
            lost, events_lost, lost_note = None, None, 'rows unresolved'
        sig.exit_code = exit_code
        sig.signal = exit_code - 128
        sig.class_hint = class_hint
        sig.tasks = [{
            'jeditaskid': tid, 'taskname': g['taskname'],
            'crashes': g['crashes'], 'finished': tot['finished'],
            'failed': tot['failed'], 'first_seen': _iso(g['first']),
            'last_seen': _iso(g['last']),
        }]
        sig.configuration = _configuration(prod_task, payload_versions.get(tid, '')
                                           or g['payload_version'])
        # The image the crashed task ran, from its PanDA task parameters:
        # what a reproduction runs, whatever the configuration says now.
        sig.configuration['container_image_ran'] = g.get('container', '')
        sig.sites = sorted(
            ({'site': s, 'crashes': v['crashes'], 'hosts': len(v['hosts'])}
             for s, v in g['sites'].items()),
            key=lambda s: -s['crashes'])
        sig.crashes = g['crashes']
        sig.rate = round(rate, 4) if rate is not None else None
        sig.minutes_p10 = _percentile(g['minutes'], 0.10)
        sig.minutes_p50 = _percentile(g['minutes'], 0.50)
        sig.first_seen, sig.last_seen = g['first'], g['last']
        sig.rows_lost, sig.events_lost = lost, events_lost
        data = dict(sig.data or {})
        data.update({
            'hosts': len({h for v in g['sites'].values() for h in v['hosts']}),
            'rows_resolved': len(g['rows']),
            'rows_unresolved': g['unresolved_rows'],
            'minutes_p25': _percentile(g['minutes'], 0.25),
            'minutes_p75': _percentile(g['minutes'], 0.75),
            'stages': dict(g['stages']),
            'rows_lost_note': lost_note,
            'record_built_at': _iso(now),
        })
        sig.data = data
        sig.updated_at = now
        sig.save()
        summary['signatures'] += 1
        summary['created'] += int(created)
        summary['classes'][class_hint] += 1
    summary['classes'] = dict(summary['classes'])
    return summary


# ---------------------------------------------------------- the reading

# The campaign images all live at one cvmfs path; the version is the
# information. Another image keeps its name.
IMAGE_PREFIX = '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:'


def _image_label(image):
    if image.startswith(IMAGE_PREFIX):
        return image[len(IMAGE_PREFIX):]
    return image.rpartition('/')[2]


def signature_summary(sig):
    """The list-row form of a signature, as the page and the tools show it."""
    return {
        'key': sig.key,
        'level': sig.level,
        'exit_code': sig.exit_code,
        'signal': sig.signal,
        'signal_name': SIGNAL_NAMES.get(sig.signal, f'signal {sig.signal}'),
        'class': sig.class_hint,
        'class_label': CLASS_LABELS.get(sig.class_hint, sig.class_hint),
        'tasks': len(sig.tasks or []),
        'task_ids': [t.get('jeditaskid') for t in (sig.tasks or [])],
        'task_names': [t.get('taskname') for t in (sig.tasks or [])],
        'configuration': sig.configuration or {},
        'image': _image_label((sig.configuration or {}).get('container_image_ran')
                              or (sig.configuration or {}).get('container_image') or ''),
        'crashes': sig.crashes,
        'rate': sig.rate,
        'rate_pct': (f'{100 * sig.rate:.1f}%' if sig.rate is not None else ''),
        'minutes_p10': sig.minutes_p10,
        'minutes_p50': sig.minutes_p50,
        'sites': sig.sites or [],
        'site_names': [s.get('site') for s in (sig.sites or [])],
        'hosts': (sig.data or {}).get('hosts'),
        'rows_lost': sig.rows_lost,
        'events_lost': sig.events_lost,
        'rows_lost_note': (sig.data or {}).get('rows_lost_note', ''),
        'stages': {STAGE_LABELS.get(k, k): v
                   for k, v in ((sig.data or {}).get('stages') or {}).items()},
        'status': sig.status,
        'status_label': STATUS_LABELS.get(sig.status, sig.status),
        'trace_status': (sig.trace or {}).get('trace_status', 'unknown'),
        'frame': (sig.trace or {}).get('frame', ''),
        # The curated finding for this frame (the findings_segfault
        # narrative, EPICPROD_NARRATIVES.md): its anchor is the trace-level
        # key, set on the entry and its members when the finding is written.
        'finding_anchor': (sig.data or {}).get('finding_anchor', ''),
        'first_seen': _iso(sig.first_seen),
        'last_seen': _iso(sig.last_seen),
        'updated_at': _iso(sig.updated_at),
    }


def catalog(status=None, class_hint=None, limit=None):
    """The catalog rows, crashes descending, with the class counts."""
    qs = CrashSignature.objects.all()
    if status:
        qs = qs.filter(status=status)
    if class_hint:
        qs = qs.filter(class_hint=class_hint)
    qs = qs.order_by('-crashes', 'key')
    if limit:
        qs = qs[:int(limit)]
    rows = [signature_summary(s) for s in qs]
    counts = defaultdict(int)
    for s in CrashSignature.objects.values_list('class_hint', flat=True):
        counts[s] += 1
    return rows, dict(counts)


def signature_detail(key, jobs_limit=200):
    """One signature with its crashed jobs, or None."""
    sig = CrashSignature.objects.filter(key=key).first()
    if sig is None:
        return None
    if sig.reproduction:
        try:
            reproduction_refresh(sig)
        except Exception as e:                                # noqa: BLE001
            logger.error('segfault %s: reproduction refresh failed: %s', key, e)
    detail = signature_summary(sig)
    detail.update({
        'tasks_detail': sig.tasks or [],
        'trace': sig.trace or {},
        'reproduction': sig.reproduction or [],
        'reproduction_outcome': (sig.data or {}).get('reproduction_outcome', ''),
        'verdict': sig.verdict,
        'assessment_ids': sig.assessment_ids or [],
        'package': sig.package or {},
        'data': sig.data or {},
    })
    task_ids = [t.get('jeditaskid') for t in (sig.tasks or []) if t.get('jeditaskid')]
    jobs = []
    if task_ids:
        qs = (EpicProdJob.objects
              .filter(phase=PHASE, jeditaskid__in=task_ids,
                      data__crash__exit_code=sig.exit_code)
              .only('pandaid', 'jeditaskid', 'seq_number', 'data')
              .order_by('-pandaid'))
        detail['jobs_total'] = qs.count()
        for job in qs[:jobs_limit]:
            crash = (job.data or {}).get('crash') or {}
            row = crash.get('row') or {}
            jobs.append({
                'pandaid': job.pandaid,
                'jeditaskid': job.jeditaskid,
                'seq': job.seq_number,
                'row': (f"{row.get('file', '').rpartition('/')[2]} "
                        f"chunk {row.get('ichunk')} ({row.get('nevents')} ev)"
                        if row else ''),
                'site': crash.get('computingsite', ''),
                'host': crash.get('modificationhost', ''),
                'minutes': crash.get('minutes'),
                'maxrss_mb': crash.get('maxrss_mb'),
                'stage': STAGE_LABELS.get(crash.get('stage', ''), crash.get('stage', '')),
                # PanDA's times are naive UTC; stated with the zone so the
                # house formatter shows them in Eastern.
                'endtime': _iso(_parse_dt(crash.get('endtime'))),
            })
    detail['jobs'] = jobs
    return detail


def signature_for_job(pandaid):
    """The signature a crashed job belongs to, for the job page's card,
    or None."""
    job = EpicProdJob.objects.filter(pandaid=pandaid, phase=PHASE).only(
        'jeditaskid', 'data').first()
    if job is None or not job.jeditaskid:
        return None
    crash = (job.data or {}).get('crash') or {}
    exit_code = crash.get('exit_code')
    if not exit_code:
        return None
    sig = CrashSignature.objects.filter(
        key=f'exit{int(exit_code)}:task{int(job.jeditaskid)}').first()
    if sig is None:
        # A trace-level merge keeps the record entry as a member.
        sig = CrashSignature.objects.filter(
            tasks__contains=[{'jeditaskid': int(job.jeditaskid)}],
            exit_code=int(exit_code)).first()
    return signature_summary(sig) if sig else None


# ---------------------------------------------------------------- the dig

def representative(sig):
    """The crashed job the dig reads for a signature: the one with the
    median time to death among its jobs, per ERROR_ATTRIBUTION.md's one
    representative per signature. None when the signature has no jobs."""
    task_ids = [t.get('jeditaskid') for t in (sig.tasks or []) if t.get('jeditaskid')]
    if not task_ids:
        return None
    timed = []
    for job in (EpicProdJob.objects
                .filter(phase=PHASE, jeditaskid__in=task_ids,
                        data__crash__exit_code=sig.exit_code)
                .only('pandaid', 'jeditaskid', 'data')):
        crash = (job.data or {}).get('crash') or {}
        timed.append((crash.get('minutes') if crash.get('minutes') is not None else -1,
                      job.pandaid, job.jeditaskid))
    if not timed:
        return None
    timed.sort()
    _minutes, pandaid, jeditaskid = timed[len(timed) // 2]
    return pandaid, jeditaskid


def resolve_log_did(pandaid, jeditaskid):
    """(scope, lfn) of the job's log tarball from the PanDA record: the
    job's own file row while filestable4 holds it, else the task's log
    dataset contents row, whose LFN carries a ``$JEDITASKID`` placeholder.
    The builder's and the dig's call, never a page's."""
    from .panda.constants import PANDA_SCHEMA
    with connections['panda'].cursor() as cur:
        cur.execute(
            f"""SELECT "scope", "lfn" FROM "{PANDA_SCHEMA}"."filestable4"
                WHERE "pandaid" = %s AND "type" = 'log'""", [int(pandaid)])
        row = cur.fetchone()
        if row and row[1]:
            return row[0] or 'group.EIC', row[1]
        cur.execute(
            f"""SELECT c."lfn" FROM "{PANDA_SCHEMA}"."jedi_dataset_contents" c
                JOIN "{PANDA_SCHEMA}"."jedi_datasets" d
                  ON d."datasetid" = c."datasetid" AND d."jeditaskid" = c."jeditaskid"
                WHERE c."jeditaskid" = %s AND c."pandaid" = %s AND d."type" = 'log'""",
            [int(jeditaskid), int(pandaid)])
        row = cur.fetchone()
    if row and row[0]:
        return 'group.EIC', row[0].replace('$JEDITASKID', str(int(jeditaskid)))
    return None, None


def frame_key(exit_code, program, frame):
    import hashlib
    digest = hashlib.sha256(f'{program}|{frame}'.encode()).hexdigest()[:12]
    return f'exit{exit_code}:frame:{digest}'


def record_trace(sig, result, pandaid, reason=''):
    """Write a dig's outcome on the signature: the trace when found, the
    status ``absent`` or ``log_unavailable`` with its reason otherwise;
    then merge into a trace-level entry when another record-level entry
    shares the crashing frame. Returns the trace-level key or None."""
    trace = dict(sig.trace or {})
    trace.update({
        'trace_status': result.get('trace_status', 'log_unavailable'),
        'program': result.get('program', ''),
        'stage': result.get('stage', ''),
        'frame': result.get('frame', ''),
        'library': result.get('library', ''),
        'frames': [f"{f['n']}: {f['function']}" + (f"  [{f['library']}]" if f.get('library') else '')
                   for f in (result.get('frames') or [])][:TRACE_MAX_FRAMES],
        'context': result.get('context') or [],
        'events_processed': result.get('events_processed'),
        'form': result.get('form', ''),
        'source_pandaid': pandaid,
        'reason': reason,
        'dug_at': _iso(timezone.now()),
    })
    sig.trace = trace
    if trace['trace_status'] == 'found' and sig.status in ('new', 'digging'):
        sig.status = 'traced'
    elif sig.status == 'digging':
        sig.status = 'new'
    sig.save(update_fields=['trace', 'status', 'updated_at'])
    if trace['trace_status'] != 'found' or sig.level != 'record':
        return None
    return merge_trace_level(sig)


def merge_trace_level(sig):
    """Record-level entries sharing a crashing frame become members of one
    trace-level entry (key exit<code>:frame:<sha>), whose counts are the
    members' sums; the members stay in the catalog at record level."""
    frame, program = (sig.trace or {}).get('frame'), (sig.trace or {}).get('program')
    if not frame:
        return None
    members = [s for s in CrashSignature.objects.filter(
        level='record', exit_code=sig.exit_code, trace__frame=frame,
        trace__program=program)]
    if len(members) < 2:
        return None
    key = frame_key(sig.exit_code, program, frame)
    merged = CrashSignature.objects.filter(key=key).first()
    created = merged is None
    if created:
        merged = CrashSignature(key=key, level='trace', status='traced',
                                exit_code=sig.exit_code, signal=sig.signal)
    tasks, sites, minutes = [], defaultdict(lambda: {'crashes': 0, 'hosts': 0}), []
    for m in members:
        tasks.extend(m.tasks or [])
        for s in (m.sites or []):
            sites[s['site']]['crashes'] += s.get('crashes', 0)
            sites[s['site']]['hosts'] += s.get('hosts', 0)
    merged.tasks = tasks
    merged.sites = sorted(({'site': k, **v} for k, v in sites.items()),
                          key=lambda s: -s['crashes'])
    merged.crashes = sum(m.crashes for m in members)
    rates = [m.rate for m in members if m.rate is not None]
    merged.rate = round(sum(rates) / len(rates), 4) if rates else None
    p10 = [m.minutes_p10 for m in members if m.minutes_p10 is not None]
    p50 = [m.minutes_p50 for m in members if m.minutes_p50 is not None]
    merged.minutes_p10 = round(sum(p10) / len(p10), 1) if p10 else None
    merged.minutes_p50 = round(sum(p50) / len(p50), 1) if p50 else None
    firsts = [m.first_seen for m in members if m.first_seen]
    lasts = [m.last_seen for m in members if m.last_seen]
    merged.first_seen = min(firsts) if firsts else None
    merged.last_seen = max(lasts) if lasts else None
    lost = [m.rows_lost for m in members if m.rows_lost is not None]
    merged.rows_lost = sum(lost) if lost else None
    ev = [m.events_lost for m in members if m.events_lost is not None]
    merged.events_lost = sum(ev) if ev else None
    classes = defaultdict(int)
    for m in members:
        classes[m.class_hint] += m.crashes
    merged.class_hint = max(classes.items(), key=lambda kv: kv[1])[0]
    merged.configuration = dict(sig.configuration or {})
    prod_tasks = sorted({(m.configuration or {}).get('prod_task', '') for m in members} - {''})
    merged.configuration['prod_task'] = (
        prod_tasks[0] if len(prod_tasks) == 1
        else f'{len(prod_tasks)} configurations' if prod_tasks else '')
    merged.configuration['prod_tasks'] = prod_tasks
    if not merged.trace:
        merged.trace = dict(sig.trace)
    data = dict(merged.data or {})
    data['members'] = sorted(m.key for m in members)
    data['hosts'] = sum((m.data or {}).get('hosts') or 0 for m in members)
    data['stages'] = {}
    for m in members:
        for stage, n in ((m.data or {}).get('stages') or {}).items():
            data['stages'][stage] = data['stages'].get(stage, 0) + n
    merged.data = data
    merged.save()
    for m in members:
        mdata = dict(m.data or {})
        if mdata.get('member_of') != key:
            mdata['member_of'] = key
            m.data = mdata
            m.save(update_fields=['data', 'updated_at'])
    return key


def dig_candidates(limit):
    """Record-level signatures never dug, largest first, for the automatic
    dig: at most ``limit`` a night so a storm is not a thousand fetches."""
    qs = (CrashSignature.objects.filter(level='record')
          .exclude(trace__has_key='trace_status').order_by('-crashes'))
    return list(qs[:limit])


# --------------------------------------------------------- reproduction

CANARY_QUEUE = '/queue/canary.ops'
CANARY_NAMESPACE = 'canary'
REFERENCE_QUEUE = 'BNL_NPPS_GPU'
CRASH_EXITS = {134, 135, 136, 139}


def reproduction_plan(sig, pandaid=None):
    """What a reproduction of the signature runs: the crashed job (the
    representative unless named), its row, its PCS task, its production
    queue and that queue's memory. Raises ValueError with the reason when
    the run cannot be formed."""
    if pandaid is None:
        rep = representative(sig)
        if rep is None:
            raise ValueError('no crashed job on record')
        pandaid = rep[0]
    job = EpicProdJob.objects.filter(pandaid=int(pandaid), phase=PHASE).select_related(
        'prod_task').first()
    if job is None:
        raise ValueError(f'job {pandaid} is not in the crash inventory')
    crash = (job.data or {}).get('crash') or {}
    if job.prod_task is None:
        raise ValueError(f'job {pandaid} has no PCS task; move its task to PCS first '
                         f'(EPICPROD_RETRIES.md, Move this task to PCS)')
    row = crash.get('row')
    if not row:
        raise ValueError(f"job {pandaid}'s manifest row is unresolved; the attempt has "
                         f'no manifest record')
    row_text = f"{row['file']},{row['ext']},{row['nevents']},{int(row['ichunk']):04d}"
    site = crash.get('computingsite') or ''
    maxrss_mb = None
    if site:
        # The queue's memory from schedconfig (the PanDA record, a table
        # read, no remote service).
        from .panda.queries import get_queue
        cfg = (get_queue(site) or {}).get('queue') or {}
        raw = cfg.get('maxrss')
        try:
            maxrss_mb = int(raw) if raw else None
        except (TypeError, ValueError):
            maxrss_mb = None
    return {'pandaid': int(pandaid), 'task': job.prod_task.name, 'row_text': row_text,
            'container': crash.get('container') or '',
            'production_queue': site, 'reference_queue': REFERENCE_QUEUE,
            'maxrss_mb': maxrss_mb, 'job_maxrss_mb': crash.get('maxrss_mb')}


def reproduce(sig, pandaid, queues, mem_limits, username):
    """Queue one payload canary per queue to the canary agent for the
    crashed job's row, and record the runs on the signature as submitted.
    ``mem_limits`` maps queue -> MB or None. Returns the entries added."""
    import json
    from .activemq_connection import ActiveMQConnectionManager
    plan = reproduction_plan(sig, pandaid)
    entries = []
    now = _iso(timezone.now())
    for queue in queues:
        msg = {'msg_type': 'payload_canary', 'namespace': CANARY_NAMESPACE,
               'task': plan['task'], 'queue': queue, 'row_text': plan['row_text'],
               'signature': sig.key, 'pandaid': plan['pandaid'],
               'created_by': username or 'segfault_reproduce'}
        if plan.get('container'):
            msg['container'] = plan['container']
        limit = mem_limits.get(queue)
        if limit:
            msg['mem_limit_mb'] = int(limit)
        sent = ActiveMQConnectionManager().send_message(CANARY_QUEUE, json.dumps(msg))
        if not sent:
            raise RuntimeError('the canary agent queue could not be reached')
        entries.append({'pandaid': plan['pandaid'], 'queue': queue,
                        'row_text': plan['row_text'], 'mem_limit_mb': limit,
                        'container': plan.get('container') or '',
                        'requested_at': now, 'requested_by': username,
                        'outcome': 'submitted', 'jedi_task_id': None,
                        'canary_pandaid': None, 'minutes': None,
                        'verdict_time': None, 'exit_code': None})
    sig.reproduction = list(sig.reproduction or []) + entries
    if sig.status in ('new', 'traced', 'digging'):
        sig.status = 'reproducing'
    sig.save(update_fields=['reproduction', 'status', 'updated_at'])
    return entries


def reproduction_refresh(sig):
    """Bring the signature's reproduction entries up to date from the
    canary runs (ProbeRun, kind payload, this signature), and settle the
    outcome once a production run and a reference run have both
    reported: reproduced (both crashed), site_dependent (production
    only), not_reproduced (neither), inconclusive (a run failed for
    another reason). swfdb only; returns True when anything changed."""
    from canary.store.models import ProbeRun
    entries = list(sig.reproduction or [])
    if not entries:
        return False
    runs = list(ProbeRun.objects.filter(
        data__kind='payload', data__signature=sig.key).select_related('queue')
        .order_by('submitted_at'))
    changed = False
    from datetime import timedelta
    claimed = set()
    for e in entries:
        if e.get('outcome') not in ('submitted', 'running', None):
            continue
        requested = _parse_dt(e.get('requested_at')) or datetime.min.replace(tzinfo=dt_timezone.utc)
        # The run this request produced: same queue and crashed job, submitted
        # from the request on, the earliest not yet claimed by another entry.
        run = next((r for r in runs if r.queue.name == e['queue']
                    and (r.data or {}).get('reproduction_of') == e['pandaid']
                    and r.submitted_at >= requested - timedelta(seconds=5)
                    and r.id not in claimed), None)
        if run is None:
            continue
        claimed.add(run.id)
        d = run.data or {}
        before = dict(e)
        e['jedi_task_id'] = run.jeditaskid
        e['run_id'] = str(run.id)
        e['canary_pandaid'] = d.get('pandaid')
        if run.status == ProbeRun.Status.FAILED_SUBMIT:
            e['outcome'] = 'inconclusive'
            e['reason'] = 'submission failed: ' + str(d.get('error') or d.get('stderr') or '')[-200:]
        elif run.status == ProbeRun.Status.COLLECTED:
            rc = d.get('payload_exit_code')
            e['exit_code'] = rc
            e['minutes'] = d.get('run_seconds') / 60.0 if d.get('run_seconds') else e.get('minutes')
            e['verdict_time'] = _iso(run.modified_at)
            if rc in CRASH_EXITS:
                e['outcome'] = 'crashed'
            elif rc == 0:
                e['outcome'] = 'completed'
            else:
                e['outcome'] = 'inconclusive'
                e['reason'] = f'payload exited {rc}'
        elif run.status == ProbeRun.Status.FAILED:
            e['outcome'] = 'inconclusive'
            e['reason'] = 'the canary job failed: ' + ', '.join(
                str(x) for x in (d.get('errors') or [])[:3])
            e['verdict_time'] = _iso(run.modified_at)
        elif run.status == ProbeRun.Status.FINISHED:
            e['outcome'] = 'inconclusive'
            e['reason'] = d.get('collect_note') or 'finished without a payload report'
        elif d.get('started_at') or d.get('wait_s') is not None:
            e['outcome'] = 'running'
        if e != before:
            changed = True
    # The pair settles the signature.
    settled = [e for e in entries if e.get('outcome') in ('crashed', 'completed', 'inconclusive')]
    prod = [e for e in settled if e['queue'] != REFERENCE_QUEUE]
    ref = [e for e in settled if e['queue'] == REFERENCE_QUEUE]
    outcome = None
    if prod and ref:
        p, r = prod[-1]['outcome'], ref[-1]['outcome']
        if p == 'crashed' and r == 'crashed':
            outcome = 'reproduced'
        elif p == 'crashed' and r == 'completed':
            outcome = 'site_dependent'
        elif p == 'completed' and r == 'completed':
            outcome = 'not_reproduced'
        elif p == 'completed' and r == 'crashed':
            outcome = 'reproduced'
        else:
            outcome = 'inconclusive'
    elif ref and ref[-1]['outcome'] == 'crashed':
        outcome = 'reproduced'
    data = dict(sig.data or {})
    if outcome and data.get('reproduction_outcome') != outcome:
        data['reproduction_outcome'] = outcome
        data['reproduction_settled_at'] = _iso(timezone.now())
        sig.data = data
        # Diagnosis runs on its own once a signature is reproduced with a
        # trace (SEGFAULT_DIAGNOSIS.md, Diagnosis), once per settlement.
        if (outcome in ('reproduced', 'site_dependent')
                and (sig.trace or {}).get('trace_status') == 'found'
                and not data.get('diagnosis')):
            queue_diagnosis(sig.key, 'auto:reproduced')
        if outcome in ('reproduced', 'site_dependent') and sig.status in ('reproducing', 'traced', 'new'):
            sig.status = 'reproduced'
        elif outcome == 'not_reproduced' and sig.status == 'reproducing':
            sig.status = 'not_reproduced'
        changed = True
    if changed:
        sig.reproduction = entries
        sig.save(update_fields=['reproduction', 'status', 'data', 'updated_at'])
    return changed


def queue_diagnosis(key, requested_by):
    """Queue a signature's LLM study to the ops agent; reported, never
    raised (a page read must not fail on the bus)."""
    import json
    from .activemq_connection import ActiveMQConnectionManager
    try:
        sent = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops',
            json.dumps({'msg_type': 'segfault_diagnose', 'namespace': 'prodops',
                        'key': key, 'requested_by': requested_by}))
        if not sent:
            logger.error('segfault %s: diagnosis not queued, bus unreachable', key)
        return bool(sent)
    except Exception as e:                                    # noqa: BLE001
        logger.error('segfault %s: diagnosis not queued: %s', key, e)
        return False
