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


def stage_label(stage):
    """The short stage a key carries: simu, reco, another stage's own
    name, or '' for a crash whose stage the record does not know."""
    stage = str(stage or '')
    return STAGE_LABELS.get(stage, stage)


def record_key(exit_code, jeditaskid, stage=''):
    """A record-level key: exit code by task, and by stage when the task's
    crashes split by stage (SEGFAULT_DIAGNOSIS.md, Signature record)."""
    key = f'exit{int(exit_code)}:task{int(jeditaskid)}'
    return f'{key}:{stage}' if stage else key


def job_filter(sig):
    """The Q that selects a signature's own crashed jobs: its tasks, its
    exit code, and its stage where the entry is a stage entry; for a
    trace-level entry, its members' filters joined."""
    from django.db.models import Q
    if sig.level == 'trace':
        members = CrashSignature.objects.filter(key__in=list((sig.data or {}).get('members') or []))
        q = Q(pk__in=[])
        for m in members:
            q |= job_filter(m)
        return q
    task_ids = [t.get('jeditaskid') for t in (sig.tasks or []) if t.get('jeditaskid')]
    q = Q(phase=PHASE, jeditaskid__in=task_ids, data__crash__exit_code=sig.exit_code)
    stage = (sig.data or {}).get('stage')
    if (sig.data or {}).get('split'):
        if stage:
            q &= Q(data__crash__stage__in=[k for k, v in STAGE_LABELS.items() if v == stage] + [stage])
        else:
            q &= (Q(data__crash__stage='') | Q(data__crash__stage__isnull=True))
    return q
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
    # A task whose crashes carry two or more known stages splits by
    # stage: one entry per stage, the stage on the key, and the crashes
    # with no stage under the plain key (SEGFAULT_DIAGNOSIS.md, Signature
    # record). Every other task keeps its plain key.
    stages_by_task = defaultdict(set)
    for job in qs.only('jeditaskid', 'data').iterator(chunk_size=5000):
        crash = (job.data or {}).get('crash') or {}
        label = stage_label(crash.get('stage'))
        if label:
            stages_by_task[(int(crash.get('exit_code') or 0),
                            int(job.jeditaskid) if job.jeditaskid else 0)].add(label)
    split_tasks = {k for k, v in stages_by_task.items() if len(v) >= 2}
    groups = {}
    for job in qs.only('pandaid', 'jeditaskid', 'prod_task_id', 'seq_number',
                       'data').iterator(chunk_size=5000):
        crash = (job.data or {}).get('crash') or {}
        exit_code = int(crash.get('exit_code') or 0)
        tid = int(job.jeditaskid) if job.jeditaskid else 0
        stage = stage_label(crash.get('stage')) if (exit_code, tid) in split_tasks else ''
        g = groups.get((exit_code, tid, stage))
        if g is None:
            g = groups[(exit_code, tid, stage)] = {
                'exit_code': exit_code, 'jeditaskid': tid, 'stage': stage,
                'split': (exit_code, tid) in split_tasks,
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
    split_keys = defaultdict(list)
    for (exit_code, tid, stage) in groups:
        if (exit_code, tid) in split_tasks:
            split_keys[(exit_code, tid)].append(record_key(exit_code, tid, stage))
    for (exit_code, tid, stage), g in sorted(groups.items(),
                                             key=lambda kv: -kv[1]['crashes']):
        tot = totals.get(tid, {'finished': 0, 'failed': 0})
        denom = tot['finished'] + tot['failed']
        rate = g['crashes'] / denom if denom else None
        class_hint = classify(exit_code, rate, g['minutes'], th)
        prod_task = prod_tasks.get(tid)
        key = record_key(exit_code, tid, stage)
        sig = CrashSignature.objects.filter(key=key).first()
        created = sig is None
        if created:
            sig = CrashSignature(key=key, status='new')
            if g['split'] and stage:
                _inherit_from_plain(sig, exit_code, tid, stage)
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
            'stage': stage,
            'split': g['split'],
            'split_keys': sorted(split_keys.get((exit_code, tid), [])),
        })
        sig.data = data
        sig.updated_at = now
        sig.save()
        summary['signatures'] += 1
        summary['created'] += int(created)
        summary['classes'][class_hint] += 1
    # A split task's plain entry that holds no crash any more (every job
    # carries a stage) is retired: its fields have moved to the stage
    # entries; the findings that named it name the stage entries.
    retired = 0
    for (exit_code, tid) in split_tasks:
        if (exit_code, tid, '') in groups:
            continue
        plain = CrashSignature.objects.filter(key=record_key(exit_code, tid)).first()
        if plain is not None:
            _retire_plain(plain, sorted(split_keys[(exit_code, tid)]))
            retired += 1
    summary['retired'] = retired
    summary['classes'] = dict(summary['classes'])
    return summary


def _inherit_from_plain(sig, exit_code, tid, stage):
    """A new stage entry takes from the task's plain entry what belongs
    to its stage: the trace when the trace's stage is this one; the
    reproductions of jobs whose stage is this one, with the outcome, the
    verdict, the diagnosis marking, the assessments and the package that
    followed them."""
    plain = CrashSignature.objects.filter(key=record_key(exit_code, tid)).first()
    if plain is None:
        return
    pdata = plain.data or {}
    trace = plain.trace or {}
    if trace.get('trace_status') and stage_label(trace.get('stage')) == stage:
        sig.trace = dict(trace)
        sig.status = 'traced' if trace.get('trace_status') == 'found' else 'new'
    repro = []
    for entry in (plain.reproduction or []):
        job = EpicProdJob.objects.filter(pandaid=entry.get('pandaid'), phase=PHASE).only('data').first()
        job_stage = stage_label(((job.data if job else None) or {}).get('crash', {}).get('stage'))
        if job_stage == stage:
            repro.append(entry)
    if repro:
        sig.reproduction = repro
        sig.verdict = plain.verdict
        sig.status = plain.status if plain.status not in ('new', 'digging', 'traced') else sig.status
        sig.assessment_ids = list(plain.assessment_ids or [])
        sig.package = dict(plain.package or {})
        data = dict(sig.data or {})
        for k in ('reproduction_outcome', 'reproduction_settled_at', 'diagnosis'):
            if k in pdata:
                data[k] = pdata[k]
        sig.data = data


def _retire_plain(plain, stage_keys):
    """Retire a split task's plain entry: leave its frame group, hand its
    name in the findings to the stage entries, delete it."""
    for e in finding_entries():
        edata = e.data or {}
        keys = [str(k) for k in (edata.get('signatures') or [e.name])]
        if plain.key not in keys:
            continue
        fstage = stage_label(edata.get('stage'))
        take = [k for k in stage_keys if not fstage or k.endswith(':' + fstage)] or stage_keys
        new_keys = [k for k in keys if k != plain.key] + [k for k in take if k not in keys]
        set_finding(e.name, {'signatures': new_keys}, changed_by='inventory:split')
    frame_key_ = (plain.data or {}).get('member_of')
    plain.delete()
    if frame_key_:
        frame = CrashSignature.objects.filter(key=frame_key_).first()
        if frame is not None:
            remerge_frame(frame)


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
        'member_of': (sig.data or {}).get('member_of', ''),
        'members': list((sig.data or {}).get('members') or []),
        'stage': (sig.data or {}).get('stage', ''),
        'split_keys': [k for k in ((sig.data or {}).get('split_keys') or []) if k != sig.key],
        'left_frame': (sig.data or {}).get('left_frame') or {},
        # A reproduction can be formed when a crashed job has its manifest
        # row and a PCS task to run under (crashed_run); both are read at
        # the nightly record build, so a row resolved during the day counts
        # from the next build.
        'runnable': bool((sig.configuration or {}).get('prod_task')
                         and ((sig.data or {}).get('rows_resolved') or 0) > 0),
        'reproduction_outcome': (sig.data or {}).get('reproduction_outcome', ''),
        'finding_anchor': '',
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
    # The curated finding each frame has, by signature key or by the
    # trace-level entry a member belongs to.
    anchors = finding_anchors()
    for r in rows:
        r['finding_anchor'] = anchors.get(r['key']) or anchors.get(r['member_of'] or '') or ''
    counts = defaultdict(int)
    for s in CrashSignature.objects.values_list('class_hint', flat=True):
        counts[s] += 1
    return rows, dict(counts)


def signature_detail(key, jobs_limit=200):
    """One signature with its crashed jobs, or None."""
    sig = CrashSignature.objects.filter(key=key).first()
    if sig is None:
        return None
    detail = signature_summary(sig)
    anchors = finding_anchors()
    detail['finding_anchor'] = anchors.get(sig.key) or anchors.get(detail['member_of'] or '') or ''
    detail.update({
        'tasks_detail': sig.tasks or [],
        'trace': sig.trace or {},
        'reproduction': sig.reproduction or [],
        'reproduction_outcome': (sig.data or {}).get('reproduction_outcome', ''),
        'verdict': sig.verdict,
        'assessment_ids': sig.assessment_ids or [],
        'package': sig.package or {},
        'data': sig.data or {},
        'local_repro': local_repro(sig),
    })
    # The reproduction attempts, execution and result apart, from the
    # shared read-only summary (monitor_app/reproductions.py); a
    # trace-level signature shows its members' attempts too. Nothing is
    # reconciled here: the canary agent does that after each collection.
    from .reproductions import attempts as _attempts, global_counts
    keys = [sig.key] + list((sig.data or {}).get('members') or [])
    detail['attempts'] = _attempts(keys=keys)
    detail['attempt_counts'] = global_counts(detail['attempts'])
    task_ids = [t.get('jeditaskid') for t in (sig.tasks or []) if t.get('jeditaskid')]
    jobs = []
    if task_ids or sig.level == 'trace':
        qs = (EpicProdJob.objects.filter(job_filter(sig))
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
        key=record_key(exit_code, job.jeditaskid, stage_label(crash.get('stage')))).first()
    if sig is None:
        sig = CrashSignature.objects.filter(key=record_key(exit_code, job.jeditaskid)).first()
    if sig is None:
        # A trace-level merge keeps the record entry as a member.
        sig = CrashSignature.objects.filter(
            tasks__contains=[{'jeditaskid': int(job.jeditaskid)}],
            exit_code=int(exit_code)).first()
    return signature_summary(sig) if sig else None


# ---------------------------------------------------------------- the dig

def representative(sig, runnable=False):
    """The crashed job the dig reads for a signature: the one with the
    median time to death among its jobs, per ERROR_ATTRIBUTION.md's one
    representative per signature. With ``runnable``, the median among the
    jobs a reproduction can run (a resolved manifest row and a PCS task),
    since a merged signature can hold legacy attempts without a manifest
    record beside PCS attempts with one. None when no job qualifies."""
    task_ids = [t.get('jeditaskid') for t in (sig.tasks or []) if t.get('jeditaskid')]
    if not task_ids:
        return None
    timed = []
    qs = (EpicProdJob.objects.filter(job_filter(sig))
          .only('pandaid', 'jeditaskid', 'data'))
    if runnable:
        qs = qs.filter(prod_task__isnull=False, data__crash__row__isnull=False)
    for job in qs:
        crash = (job.data or {}).get('crash') or {}
        if runnable and not crash.get('row'):
            continue
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


def reproduction_entry(sig, pandaid):
    """The signature's reproduction request whose canary job is
    ``pandaid``, or None. A reproduction's job is not in the crash
    inventory (the canary wrapper exits 0 to keep the report), so the
    dig reaches it through this record."""
    for entry in sig.reproduction or []:
        if entry.get('canary_pandaid') and int(entry['canary_pandaid']) == int(pandaid):
            return entry
    return None


def report_note(pandaid):
    """(note, source) of a job's payload report: the sweep's filing on
    the job's inventory row (``payload_report``), else the PanDA
    metatable, where the pilot lifts jobReport.json for a job that
    finished at the server (a reproduction on the reference queue
    carries no log dataset, so its report reaches the record there and
    nowhere else). ('', '') when the job has none. The dig's and the
    reconciliation's call, never a page's."""
    import json
    from .panda.constants import PANDA_SCHEMA
    job = EpicProdJob.objects.filter(pandaid=int(pandaid)).only('data').first()
    filed = str(((((job.data if job else None) or {}).get('payload_report') or {})
                 .get('report') or {}).get('note') or '')
    if filed.startswith('crash:'):
        return filed, 'payload_report'
    try:
        with connections['panda'].cursor() as cur:
            cur.execute(f'SELECT "metadata" FROM "{PANDA_SCHEMA}"."metatable" WHERE "pandaid" = %s',
                        [int(pandaid)])
            row = cur.fetchone()
    except Exception as e:                                    # noqa: BLE001
        logger.error('job %s: metatable read failed: %s', pandaid, e)
        return filed, 'payload_report' if filed else ''
    meta = None
    if row and row[0]:
        try:
            meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except (ValueError, TypeError) as e:
            logger.error('job %s: metatable metadata unparsable: %s', pandaid, e)
    payload = meta.get('payload') if isinstance(meta, dict) else None
    note = str((payload or {}).get('note') or '') if isinstance(payload, dict) else ''
    if note:
        return note, 'metatable'
    return filed, 'payload_report' if filed else ''


def trace_from_report(sig, pandaid):
    """Record on the signature the trace read from a job's payload report
    when its note is a crash capture (payload 0.12 and later put the
    crashing stage's log tail there, SEGFAULT_DIAGNOSIS.md, Traces going
    forward): the dig's first source, and the reconciliation's for a
    reproduced crash. Returns the dig summary, or None when the report
    carries no crash note or no frame (the dig then falls back to the
    log tarball)."""
    from .epicprod_inventory import trace_extract
    note, source = report_note(pandaid)
    if not note.startswith('crash:'):
        return None
    result = trace_extract([note])
    if result.get('trace_status') != 'found':
        return None
    merged = record_trace(sig, result, pandaid, '')
    logger.info('segfault %s: trace read from the %s payload report of job %s',
                sig.key, source, pandaid)
    return {'key': sig.key, 'pandaid': pandaid, 'trace_status': 'found', 'source': source,
            'program': result.get('program'), 'stage': result.get('stage'),
            'frame': result.get('frame'), 'library': result.get('library'),
            'events_processed': result.get('events_processed'), 'merged_into': merged}


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
    key = frame_key(sig.exit_code, program, frame)
    candidates = [s for s in CrashSignature.objects.filter(
        level='record', exit_code=sig.exit_code, trace__frame=frame,
        trace__program=program)]
    # A member whose reproduction settled it under another finding than
    # the frame's is that finding's crash, whatever frame its one trace
    # read (task 38864: the trace of one row's reconstruction crash, the
    # reproduced rows the dRICH simulation crash); it leaves the frame.
    merged = CrashSignature.objects.filter(key=key).first()
    frame_finding = covering_finding(merged) if merged is not None else None
    frame_fid = (frame_finding or {}).get('fid')
    members, left = [], []
    for m in candidates:
        diag = (m.data or {}).get('diagnosis') or {}
        if (diag.get('state') == 'covered' and diag.get('finding')
                and frame_fid and diag['finding'] != frame_fid):
            left.append(m)
        else:
            members.append(m)
    for m in left:
        mdata = dict(m.data or {})
        if mdata.get('member_of') == key:
            mdata['member_of'] = ''
            mdata['left_frame'] = {'key': key, 'finding': (m.data or {}).get('diagnosis', {}).get('finding')}
            m.data = mdata
            m.save(update_fields=['data', 'updated_at'])
    if len(members) < 2:
        if merged is not None and left:
            # the frame survives with what remains, or goes with its last member
            if len(members) == 1:
                _refresh_frame(merged, members)
                return key
            merged.delete()
        return None
    created = merged is None
    if created:
        merged = CrashSignature(key=key, level='trace', status='traced',
                                exit_code=sig.exit_code, signal=sig.signal)
    _refresh_frame(merged, members, sig)
    return key


def remerge_frame(frame):
    """Recompute a trace-level entry from its members as they stand now
    (after a split, a retirement, or a member leaving for another
    finding). Returns the key, or None when the frame is gone."""
    members = [m for m in CrashSignature.objects.filter(
        key__in=list((frame.data or {}).get('members') or []))]
    if not members:
        frame.delete()
        return None
    return merge_trace_level(members[0])


def _refresh_frame(merged, members, sig=None):
    """The frame entry's counts, tasks, sites and stages from its members."""
    sig = sig or members[0]
    key = merged.key
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


def remerge_frames():
    """Every trace-level entry recomputed from its members: the nightly
    pass after the record build. Returns the keys kept and gone."""
    kept, gone = [], []
    for frame in list(CrashSignature.objects.filter(level='trace')):
        if remerge_frame(frame):
            kept.append(frame.key)
        else:
            gone.append(frame.key)
    return {'kept': kept, 'gone': gone}


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
# The second run of a reproduction is a second production-class queue
# (SEGFAULT_DIAGNOSIS.md, Reproduction, 2026-09-13): the crash-or-not
# verdict and the trace come from any queue since payload 0.14, and the
# reference queue's one slot is asked for only when the reading needs
# the host (a memory ceiling, a core dump, a stall watched live).
ELSEWHERE_QUEUE = 'UM_GREX_PanDA_1'
ELSEWHERE_FROM = {'UM_GREX_PanDA_1': 'BNL_OSG_EPIC_PROD_1'}
CRASH_EXITS = {134, 135, 136, 139}


def elsewhere_queue(origin):
    """The default second queue for a crash that happened on ``origin``."""
    return ELSEWHERE_FROM.get(origin or '', ELSEWHERE_QUEUE)


def run_role(queue, origin):
    """The role a run plays in a reproduction: ``production`` on the queue
    the crash happened on, ``reference`` on the reference queue,
    ``elsewhere`` on any other queue. The last two settle the outcome the
    same way against the production run."""
    if queue == REFERENCE_QUEUE:
        return 'reference'
    if origin and queue == origin:
        return 'production'
    return 'elsewhere'


def crashed_run(sig, pandaid=None):
    """The crashed job a reproduction runs (the representative unless
    named) with its crash record, its manifest row and the row as manifest
    text. Raises ValueError with the reason when the run cannot be formed."""
    if pandaid is None:
        rep = representative(sig, runnable=True) or representative(sig)
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
    return job, crash, row, row_text


def repro_environment(prod_task):
    """The payload environment of a run outside PanDA: the task's own
    (pcs.commands._evgen_env) with registration, the copies and the log
    upload off, so the run leaves its outputs in the working directory and
    touches no catalog."""
    from pcs.commands import _evgen_env
    env = _evgen_env(prod_task)
    env.update({'USERUCIO': 'false', 'COPYRECO': 'false', 'COPYFULL': 'false',
                'COPYLOG': 'false'})
    return env


PAYLOAD_REPO = 'https://github.com/BNLNPPS/swf-epicprod'
PAYLOAD_PATH = 'swf_epicprod/payload/run.sh'
PAYLOAD_VERSION_FILE = 'swf_epicprod/payload/VERSION'
# The JLab door and base the payload streams EVGEN input from (run.sh,
# XRDRURL and XRDRBASE defaults).
INPUT_DOOR = 'root://dtn2304.jlab.org:8443//jlab-osdf-ro/eic/EPIC/volatile'


def local_repro(sig, pandaid=None):
    """The crashed job's run as a shell fragment for a reproduction by hand:
    the image, the payload environment, the input, and the payload's
    invocation with the row's arguments as the dispatcher passes them
    (evgen_job_dispatcher.run_row: EVGEN/<file>, extension, events, chunk).
    The representative job unless one is named. Returns {'error': reason}
    when the run cannot be formed."""
    try:
        job, crash, row, row_text = crashed_run(sig, pandaid)
    except ValueError as e:
        return {'error': str(e)}
    try:
        env = repro_environment(job.prod_task)
    except Exception as e:                                    # noqa: BLE001
        logger.error('segfault %s: payload environment of task %s failed: %s',
                     sig.key, job.prod_task.name, e)
        return {'error': f'payload environment of {job.prod_task.name}: {e}'}
    container = crash.get('container') or ''
    if not container:
        cfg = job.prod_task.prod_config
        container = getattr(cfg, 'container_image', '') if cfg else ''
    # The payload the job ran, from its attempt's submission record; a
    # version was set by exactly one commit (the one that wrote it into
    # payload/VERSION), which the clone resolves and checks out. An attempt
    # without one is a legacy attempt: it ran the production team's script,
    # not this payload.
    from pcs.models import PandaTasks
    attempt = PandaTasks.objects.filter(jedi_task_id=job.jeditaskid).only('metadata').first()
    payload_version = str(((attempt.metadata if attempt else None) or {}).get('payload_version') or '')
    ichunk = int(row['ichunk'])
    nevents = int(row['nevents'])
    input_url = f"{INPUT_DOOR}/EVGEN/{row['file']}.{row['ext']}"
    signal_name = SIGNAL_NAMES.get(crash.get('signal'), crash.get('signal'))
    lines = [
        f"# PanDA job {job.pandaid} (task {job.jeditaskid}, {job.prod_task.name}): "
        f"the payload died on {signal_name} (exit {crash.get('exit_code')}) "
        f"after {crash.get('minutes')} min at {crash.get('computingsite')}",
        f"# Image: {container or '(not on record)'}",
        "# Inside the image (eic-shell, or: apptainer exec <image> bash), in an empty directory:",
        f"git clone {PAYLOAD_REPO}",
    ]
    if payload_version:
        lines += [
            f"# The payload the job ran, {payload_version}: the commit that set that version",
            f"git -C swf-epicprod checkout $(git -C swf-epicprod log -S'{payload_version}' "
            f"--format=%H -- {PAYLOAD_VERSION_FILE} | tail -1)",
        ]
    else:
        lines += [
            "# No payload version on the attempt's record: a legacy attempt ran the production",
            "# team's script (eic/job_submission_condor), not this payload; the current payload",
            "# runs the same row through the same stages in the same image.",
        ]
    lines += ["cat > environment-manifest.sh <<'EOF'"]
    lines += [f'export {k}={v}' for k, v in env.items()]
    lines += [
        'EOF',
        f"swf-epicprod/{PAYLOAD_PATH} EVGEN/{row['file']} {row['ext']} {nevents} {ichunk:04d}",
        f"# Input (public read): {input_url}",
        f"# Manifest row {ichunk + 1}: {nevents} events after skipping {ichunk} x {nevents}, "
        f"seed {ichunk + 1}; outputs stay in the working directory, nothing is registered.",
    ]
    return {'pandaid': job.pandaid, 'jeditaskid': job.jeditaskid,
            'task': job.prod_task.name, 'container': container, 'row_text': row_text,
            'payload_version': payload_version, 'input_url': input_url,
            'environment': env, 'fragment': '\n'.join(lines)}


def reproduction_plan(sig, pandaid=None):
    """What a reproduction of the signature runs: the crashed job (the
    representative unless named), its row, its PCS task, its production
    queue and that queue's memory. Raises ValueError with the reason when
    the run cannot be formed."""
    job, crash, row, row_text = crashed_run(sig, pandaid)
    pandaid = job.pandaid
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
            'production_queue': site, 'elsewhere_queue': elsewhere_queue(site),
            'reference_queue': REFERENCE_QUEUE,
            'maxrss_mb': maxrss_mb, 'job_maxrss_mb': crash.get('maxrss_mb')}


def _append_reproduction(key, entry):
    """Append one request entry to the signature under a row lock, so a
    request recorded meanwhile by another writer is kept."""
    from django.db import transaction
    with transaction.atomic():
        sig = CrashSignature.objects.select_for_update().get(key=key)
        sig.reproduction = list(sig.reproduction or []) + [entry]
        fields = ['reproduction', 'updated_at']
        if sig.status in ('new', 'traced', 'digging'):
            sig.status = 'reproducing'
            fields.append('status')
        sig.save(update_fields=fields)
        return sig


def reproduce(sig, pandaid, queues, mem_limits, username):
    """Queue one payload canary per queue to the canary agent for the
    crashed job's row, and record each request on the signature as it is
    sent, with the request identity the run will carry
    (monitor_app/reproductions.py). ``mem_limits`` maps queue -> MB or
    None. Returns the entries added; a send that fails after an earlier
    one succeeded leaves the earlier request on record and raises."""
    import json
    from .activemq_connection import ActiveMQConnectionManager
    from .reproductions import new_request_id
    plan = reproduction_plan(sig, pandaid)
    entries = []
    now = _iso(timezone.now())
    for queue in queues:
        request_id = new_request_id()
        msg = {'msg_type': 'payload_canary', 'namespace': CANARY_NAMESPACE,
               'task': plan['task'], 'queue': queue, 'row_text': plan['row_text'],
               'signature': sig.key, 'pandaid': plan['pandaid'],
               'request_id': request_id,
               'created_by': username or 'segfault_reproduce'}
        if plan.get('container'):
            msg['container'] = plan['container']
        limit = mem_limits.get(queue)
        if limit:
            msg['mem_limit_mb'] = int(limit)
        sent = ActiveMQConnectionManager().send_message(CANARY_QUEUE, json.dumps(msg))
        if not sent:
            raise RuntimeError(
                'the canary agent queue could not be reached'
                + (f" (the request for {', '.join(e['queue'] for e in entries)} was sent)"
                   if entries else ''))
        entry = {'request_id': request_id, 'run_id': None,
                 'pandaid': plan['pandaid'], 'queue': queue,
                 'role': run_role(queue, plan['production_queue']),
                 'row_text': plan['row_text'], 'mem_limit_mb': limit,
                 'container': plan.get('container') or '',
                 'requested_at': now, 'requested_by': username,
                 'outcome': 'submitted', 'jedi_task_id': None,
                 'canary_pandaid': None, 'minutes': None,
                 'verdict_time': None, 'exit_code': None}
        saved = _append_reproduction(sig.key, entry)
        sig.reproduction, sig.status = saved.reproduction, saved.status
        entries.append(entry)
    return entries


def reproduction_refresh(sig):
    """Reconcile one signature's reproduction requests with their runs
    (monitor_app/reproductions.py): the canary agent's step after each
    collection, never a page read. Kept under its name for the callers
    that have it; returns True when anything changed."""
    from .reproductions import reconcile
    changed = reconcile(sig.key, queue_diagnosis=queue_diagnosis)
    sig.refresh_from_db()
    return bool(changed.get('entries') or changed.get('outcome'))


def queue_traced_studies(limit, requested_by='auto:traced'):
    """The nightly automatic study: a traced signature that no finding
    reads and no study has been asked of is studied once, largest first,
    at most ``limit`` a night, without waiting for a reproduction (a
    reproduced one is queued by the reconciliation). Each is marked
    queued before its message goes out, so a later pass never asks
    twice; a frame a finding already reads is marked covered instead,
    every night, outside the cap (no study, nothing to bound).
    Returns {'queued': [keys], 'covered': [keys]}."""
    from django.db import transaction
    out = {'queued': [], 'covered': []}
    candidates = (CrashSignature.objects.filter(level='record')
                  .exclude(status__in=('diagnosed', 'handed_off', 'fixed', 'accepted'))
                  .order_by('-crashes'))
    now = _iso(timezone.now())
    for sig in candidates:
        if (sig.trace or {}).get('trace_status') != 'found' or (sig.data or {}).get('diagnosis'):
            continue
        with transaction.atomic():
            sig = CrashSignature.objects.select_for_update().get(pk=sig.pk)
            data = dict(sig.data or {})
            if data.get('diagnosis'):
                continue
            finding = covering_finding(sig)
            if not finding and len(out['queued']) >= limit:
                continue
            if finding:
                data['diagnosis'] = {'state': 'covered', 'finding': finding['fid'],
                                     'anchor': finding['anchor'], 'requested_by': requested_by,
                                     'submitted_at': now}
                sig.verdict = f"covered by finding {finding['fid']}: {finding['title']}".strip(': ')
                sig.status = 'diagnosed'
                sig.data = data
                sig.save(update_fields=['data', 'verdict', 'status', 'updated_at'])
                out['covered'].append(sig.key)
                continue
            data['diagnosis'] = {'state': 'queued', 'requested_by': requested_by,
                                 'submitted_at': now}
            sig.data = data
            sig.save(update_fields=['data', 'updated_at'])
        if queue_diagnosis(sig.key, requested_by):
            out['queued'].append(sig.key)
        else:
            # The bus refused: unmark, so the next night asks again.
            CrashSignature.objects.filter(pk=sig.pk).update(
                data={k: v for k, v in (sig.data or {}).items() if k != 'diagnosis'})
    return out


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


# --------------------------------------------------------------- findings

FINDING_KIND = 'finding'
FINDING_CONTEXT = 'segfault'
FINDING_FIELDS = ('date', 'frame', 'stage', 'title', 'class', 'action', 'standing',
                  'fix', 'notes', 'sources', 'signatures', 'model_reading')


def _finding_context():
    from .models import EntryContext
    ctx, _ = EntryContext.objects.get_or_create(
        name=FINDING_CONTEXT,
        defaults={'title': 'Segfault findings',
                  'description': 'The curated reading of the segfault catalog, one '
                                 'entry per crashing frame (SEGFAULT_DIAGNOSIS.md, Findings).'})
    return ctx


def finding_entries():
    """The findings as stored: Entry rows of kind finding in the segfault
    context, not archived, newest first by serial (the order they were
    written; a finding without a serial yet sorts by its creation time)."""
    from .models import Entry
    rows = list(Entry.objects.filter(kind=FINDING_KIND, context_id=FINDING_CONTEXT,
                                     archived=False, deleted_at__isnull=True))
    rows.sort(key=lambda e: (int((e.data or {}).get('serial') or 0), e.timestamp_created), reverse=True)
    return rows


def finding_id(serial):
    """The finding's permanent id as shown: f-8."""
    return f'f-{int(serial)}' if serial else ''


def _next_finding_serial(ctx):
    """The next free serial of the findings store, under the context row's
    lock so two writers never draw the same number. Serials are assigned
    once, at creation, and never reused: a finding keeps its id whatever
    is written or retired around it."""
    from .models import Entry, EntryContext
    EntryContext.objects.select_for_update().get(pk=ctx.pk)
    taken = [int((e.data or {}).get('serial') or 0)
             for e in Entry.objects.filter(kind=FINDING_KIND, context=ctx).only('data')]
    return max(taken, default=0) + 1


def set_finding(name, fields, changed_by):
    """Create or update the finding named ``name`` (the frame's catalog key,
    the trace-level entry's where one exists) with ``fields`` (FINDING_FIELDS
    plus ``what``, the reading, as the entry's content). A new finding draws
    the next permanent serial (``data['serial']``, shown as f-n). Every
    substantive change leaves an EntryVersion stamped with ``changed_by``.
    Returns the Entry and whether it was created."""
    from django.db import transaction
    from .models import Entry
    from .signals import set_changed_by
    set_changed_by(changed_by or 'unknown')
    with transaction.atomic():
        ctx = _finding_context()
        entry = Entry.objects.filter(kind=FINDING_KIND, context=ctx, name=name).first()
        created = entry is None
        if created:
            entry = Entry(kind=FINDING_KIND, context=ctx, name=name, status='open')
        data = dict(entry.data or {})
        for key in FINDING_FIELDS:
            if key in fields:
                data[key] = fields[key]
        data.setdefault('signatures', [name])
        if not data.get('serial'):
            data['serial'] = _next_finding_serial(ctx)
        data['updated_by'] = changed_by
        entry.data = data
        if 'title' in fields:
            entry.title = str(fields['title'] or '')
        if 'what' in fields:
            entry.content = str(fields['what'] or '')
        if 'standing' in fields:
            entry.status = str(fields['standing'] or 'open')
        entry.timestamp_modified = __import__('time').time()
        entry.save()
    log_finding = __import__('monitor_app.epicprod_logging', fromlist=['log_epicprod_action']).log_epicprod_action
    log_finding('web', 'segfault_finding_set', subject_type='crash_signature', subject_key=name,
                username=changed_by, sublevel='normal', live_default=True,
                message=f"segfault finding {'created' if created else 'updated'}: {name}: "
                        f"{entry.title}"[:300], created=int(created))
    return entry, created


def assign_finding_serials(changed_by):
    """Give every finding without a serial one, in creation order, so the
    findings written before serials existed are numbered as they were
    made. Returns [(name, serial)] assigned. Idempotent."""
    from django.db import transaction
    from .models import Entry
    from .signals import set_changed_by
    set_changed_by(changed_by or 'unknown')
    assigned = []
    with transaction.atomic():
        ctx = _finding_context()
        rows = [e for e in Entry.objects.filter(kind=FINDING_KIND, context=ctx)
                if not (e.data or {}).get('serial')]
        rows.sort(key=lambda e: (e.timestamp_created, e.id))
        for e in rows:
            data = dict(e.data or {})
            data['serial'] = _next_finding_serial(ctx)
            e.data = data
            e.save()
            assigned.append((e.name, data['serial']))
    return assigned


def findings(*, name=None, version=None, include_history=False):
    """The findings joined live to the catalog: each entry with the
    signatures it names, their crashes, tasks, configurations and loss.
    Optional history is read from immutable EntryVersion snapshots. A
    requested version must belong to the named, active segfault finding.
    Returns (entries, error); raises ValueError for an unknown version."""
    from .models import EntryVersion
    current = [e for e in finding_entries() if name is None or e.name == name]
    history = {}
    if include_history:
        for v in EntryVersion.objects.filter(entry_id__in=[e.id for e in current]).order_by('-version_num').values(
                'entry_id', 'version_num', 'timestamp', 'changed_by'):
            v['replaced_at'] = _iso(datetime.fromtimestamp(v['timestamp'], tz=dt_timezone.utc))
            history.setdefault(v['entry_id'], []).append(v)
    snapshot = None
    if version is not None:
        if name is None or not current:
            raise ValueError('No such finding version')
        snapshot = EntryVersion.objects.filter(entry=current[0], version_num=version).first()
        if snapshot is None:
            raise ValueError('No such finding version')
    entries = []
    for i, e in enumerate(current):
        source = snapshot if snapshot is not None else e
        f = source.data or {}
        keys = [str(k) for k in (f.get('signatures') or [e.name])]
        sigs = {s.key: s for s in CrashSignature.objects.filter(key__in=keys)}
        rows, crashes, prod_tasks, rows_lost, events_lost = [], 0, set(), 0, 0
        for key in keys:
            s = sigs.get(key)
            if s is None:
                rows.append({'key': key, 'missing': True})
                continue
            rows.append(signature_summary(s))
            if s.level == 'trace' or len(keys) == 1:
                crashes = max(crashes, s.crashes)
                cfg = s.configuration or {}
                for t in cfg.get('prod_tasks') or ([cfg.get('prod_task')] if cfg.get('prod_task') else []):
                    prod_tasks.add(t)
                rows_lost += s.rows_lost or 0
                events_lost += s.events_lost or 0
        serial = (e.data or {}).get('serial') or 0
        entries.append({
            'n': i + 1, 'id': e.id, 'name': e.name,
            'serial': serial, 'fid': finding_id(serial),
            'anchor': (keys[0] if keys else e.name).replace(':', '-'),
            'date': str(f.get('date') or ''), 'frame': f.get('frame', ''),
            'stage': f.get('stage', ''), 'title': source.title, 'what': (source.content or '').strip(),
            'sources': f.get('sources') or [], 'class': f.get('class', ''),
            'action': (f.get('action') or '').strip(),
            'standing': f.get('standing', '') if snapshot is not None else e.status or f.get('standing', ''),
            'fix': f.get('fix', ''), 'notes': (f.get('notes') or '').strip(),
            'model_reading': bool(f.get('model_reading')),
            'updated_by': f.get('updated_by', ''), 'updated_at': _iso(
                datetime.fromtimestamp(snapshot.timestamp if snapshot is not None else e.timestamp_modified,
                                       tz=dt_timezone.utc)),
            'signatures': rows, 'crashes': crashes,
            'tasks': len({t for r in rows for t in (r.get('task_ids') or [])}),
            'configurations': sorted(prod_tasks), 'rows_lost': rows_lost, 'events_lost': events_lost,
        })
        if include_history:
            current_keys = (e.data or {}).get('signatures') or [e.name]
            entries[-1].update({
                'versions': history.get(e.id, []),
                'version': snapshot.version_num if snapshot is not None else None,
                'replaced_by': snapshot.changed_by if snapshot is not None else '',
                'current_anchor': str(current_keys[0]).replace(':', '-'),
            })
    return entries, ''


def finding_anchors():
    """{signature key: findings-page anchor} for every signature a finding
    names."""
    out = {}
    for e in finding_entries():
        keys = [str(k) for k in ((e.data or {}).get('signatures') or [e.name])]
        anchor = (keys[0] if keys else e.name).replace(':', '-')
        for k in keys:
            out[k] = anchor
    return out


def covering_finding(sig):
    """The finding that names this signature or the frame entry it merged
    under: the reading of its crash exists, so no study is owed and the
    signature is marked diagnosed by that finding once its reproduction
    settles. {'fid', 'title', 'anchor'} or None."""
    member_of = (sig.data or {}).get('member_of') or ''
    for e in finding_entries():
        keys = [str(k) for k in ((e.data or {}).get('signatures') or [e.name])]
        if sig.key in keys or (member_of and member_of in keys):
            return {'fid': finding_id((e.data or {}).get('serial')), 'title': e.title or '',
                    'anchor': (keys[0] if keys else e.name).replace(':', '-')}
    return None
