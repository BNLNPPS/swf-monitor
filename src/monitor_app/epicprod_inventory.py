import csv
import json
import logging
import os
import re
from io import StringIO
from pathlib import PurePosixPath

from django.conf import settings
from django.db import OperationalError, ProgrammingError, transaction
from django.utils import timezone

from .models import EpicProdFile, EpicProdJob

logger = logging.getLogger(__name__)


PSEUDO_DATASETS = {'seq_number', 'pseudo_dataset'}
PAYLOAD_LOG_MEMBERS = (
    'payload.stdout',
    'payload.stderr',
    'pilotlog.txt',
    'pandatracerlog.txt',
)


def is_pseudo_panda_file(file_info):
    return (
        file_info.get('type') == 'pseudo_input'
        or file_info.get('dataset') in PSEUDO_DATASETS
        or file_info.get('lfn') in {'pseudo_lfn'}
    )


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _csv_row(row):
    parsed = next(csv.reader(StringIO(row)))
    if len(parsed) < 4:
        raise ValueError(f'EVGEN csv row has fewer than 4 fields: {row!r}')
    return parsed[0], parsed[1], parsed[2], parsed[3]


def _payload_names(file_col, ext, chunk, env):
    """Mirror the current hepmc3 run.sh naming contract.

    The dispatcher calls run.sh with BASENAME=EVGEN/<file_col>, EXTENSION=<ext>,
    EVENTS_PER_TASK=<events>, and ichunk=<chunk>. run.sh derives TASKNAME from
    basename(BASENAME)+'.'+chunk and TAG from the EVGEN-relative directory under
    DETECTOR_VERSION/DETECTOR_CONFIG[/TAG_PREFIX].
    """
    basename = f'EVGEN/{file_col}'
    task_suffix = f'.{chunk}' if chunk else ''
    taskname = f'{PurePosixPath(basename).name}{task_suffix}'

    input_file = f'{basename}.{ext}'
    input_dir = str(PurePosixPath(input_file).parent)
    if input_dir == '.':
        evgen_tag = ''
    elif input_dir.startswith('EVGEN/'):
        evgen_tag = input_dir[len('EVGEN/'):]
    elif input_dir == 'EVGEN':
        evgen_tag = ''
    else:
        evgen_tag = input_dir

    tag_parts = [
        env.get('DETECTOR_VERSION') or 'main',
        env.get('DETECTOR_CONFIG') or '',
    ]
    tag_prefix = env.get('TAG_PREFIX') or ''
    if tag_prefix:
        tag_parts.append(tag_prefix.strip('/'))
    if evgen_tag:
        tag_parts.append(evgen_tag.strip('/'))
    tag = '/'.join(p for p in tag_parts if p)
    return {
        'basename': basename,
        'input_file': input_file,
        'taskname': taskname,
        'tag': tag,
        'evgen_tag': evgen_tag,
        'log_dir': f'LOG/{tag}',
        'full_dir': f'FULL/{tag}',
        'reco_dir': f'RECO/{tag}',
    }


def build_expected_files_for_task(task, spec=None):
    """Return expected ePIC production files for a PCS EVGEN task.

    When ``spec`` is supplied this is definition-only and does not query Rucio
    or PanDA. If ``spec`` is omitted, the PCS EVGEN spec is regenerated; that is
    intended for operator/agent backfill paths, not page rendering.
    """
    if spec is None:
        from pcs.commands import build_evgen_task_params
        spec = build_evgen_task_params(task)
    env = spec.get('env') or {}
    out = []
    jeditaskid = task.panda_task_id
    rse = env.get('OUT_RSE') or 'EIC-XRD'

    for job_index, row in enumerate(spec.get('csvRows') or []):
        file_col, ext, events, chunk = _csv_row(row)
        seq_number = job_index + 1
        names = _payload_names(file_col, ext, chunk, env)
        input_did = f"/{names['input_file']}"
        input_lfn = PurePosixPath(input_did).name
        input_dataset = str(PurePosixPath(input_did).parent)

        common = {
            'prod_task': task,
            'jeditaskid': jeditaskid,
            'seq_number': seq_number,
            'job_index': job_index,
            'source': 'pcs_expected',
            'status': 'expected',
        }
        out.append({
            **common,
            'role': 'input',
            'stage': 'EVGEN',
            'scope': 'epic',
            'dataset_name': input_dataset,
            'did_name': input_did,
            'lfn': input_lfn,
            'rse_expected': '',
            'data': {'csv_row': row, 'events': events, 'chunk': chunk},
        })

        if str(env.get('COPYFULL', '')).lower() == 'true':
            did = f"/{names['full_dir']}/{names['taskname']}.edm4hep.root"
            out.append({
                **common,
                'role': 'output',
                'stage': 'FULL',
                'scope': 'epic',
                'dataset_name': f"/{names['full_dir']}",
                'did_name': did,
                'lfn': PurePosixPath(did).name,
                'rse_expected': rse,
                'data': {'csv_row': row, 'chunk': chunk},
            })

        if str(env.get('COPYRECO', '')).lower() == 'true':
            did = f"/{names['reco_dir']}/{names['taskname']}.eicrecon.edm4eic.root"
            out.append({
                **common,
                'role': 'output',
                'stage': 'RECO',
                'scope': 'epic',
                'dataset_name': f"/{names['reco_dir']}",
                'did_name': did,
                'lfn': PurePosixPath(did).name,
                'rse_expected': rse,
                'data': {'csv_row': row, 'chunk': chunk},
            })

        if str(env.get('COPYLOG', '')).lower() == 'true':
            # The payload log implementation has changed between per-file logs
            # and timestamped tarballs. Keep a dataset-level expectation until
            # the exact shipped script emits a stable manifest.
            out.append({
                **common,
                'role': 'log',
                'stage': 'LOG',
                'scope': 'epic',
                'dataset_name': f"/{names['log_dir']}",
                'did_name': f"/{names['log_dir']}",
                'lfn': f"{names['taskname']} log outputs",
                'rse_expected': 'EIC-XRD-LOG',
                'data': {
                    'csv_row': row,
                    'chunk': chunk,
                    'dataset_level': True,
                    'reason': 'payload log file name may include runtime timestamp',
                },
            })
    return out


@transaction.atomic
def sync_expected_files_for_task(task, spec=None):
    expected = build_expected_files_for_task(task, spec=spec)
    rows = []
    for item in expected:
        lookup = {
            'prod_task': task,
            'source': item['source'],
            'role': item['role'],
            'stage': item['stage'],
            'seq_number': item['seq_number'],
            'did_name': item['did_name'],
        }
        defaults = {k: v for k, v in item.items() if k not in lookup}
        obj, _ = EpicProdFile.objects.update_or_create(
            **lookup,
            defaults=defaults,
        )
        rows.append(obj)
    return rows


def _seq_number_from_files(files):
    for f in files or []:
        if f.get('type') == 'pseudo_input' and str(f.get('lfn') or '').isdigit():
            return int(f['lfn'])
    for f in files or []:
        lfn = f.get('lfn') or ''
        m = re.search(r'\.(\d{6})\.log\.tgz$', lfn)
        if m:
            return int(m.group(1))
    return None


def _prod_task_for_jeditaskid(jeditaskid):
    if not jeditaskid:
        return None
    try:
        from pcs.models import PandaTasks, ProdTask
        assoc = (
            PandaTasks.objects
            .filter(jedi_task_id=int(jeditaskid))
            .select_related('prod_task', 'prod_task__dataset', 'prod_task__prod_config')
            .first()
        )
        if assoc:
            return assoc.prod_task
        return (ProdTask.objects
                .filter(panda_task_id=int(jeditaskid))
                .select_related('dataset', 'prod_config')
                .first())
    except Exception:
        logger.exception("PCS lookup failed for JEDI task %s", jeditaskid)
        return None


def _rucio_conflict_details(text):
    if 'DataIdentifierAlreadyExists' not in text and 'File DID already exists' not in text:
        return None
    detail = 'Rucio file DID already exists'
    checksum = re.search(
        r'Local checksum\s+([0-9a-fA-F]+)\s+does not match remote checksum\s+([0-9a-fA-F]+)',
        text,
    )
    data = {}
    if checksum:
        data = {'local_checksum': checksum.group(1), 'remote_checksum': checksum.group(2)}
        detail = (
            f"Rucio file DID already exists; local checksum {checksum.group(1)} "
            f"does not match remote checksum {checksum.group(2)}"
        )
    return detail, data


def _timeline_from_log_text(text):
    events = []
    if 'Finished processing.' in text:
        events.append({'phase': 'reconstruction_complete',
                       'message': 'eicrecon finished processing'})
    valid = re.search(r'VALID:\s+(\S+\.eicrecon\.edm4eic\.root)', text)
    if valid:
        events.append({'phase': 'reco_validation_passed',
                       'message': 'RECO ROOT file validation passed',
                       'path': valid.group(1)})
    if 'register_to_rucio.py' in text:
        events.append({'phase': 'rucio_registration_attempted',
                       'message': 'Payload attempted JLab Rucio registration'})
    conflict = _rucio_conflict_details(text)
    if conflict:
        events.append({'phase': 'rucio_registration_failed',
                       'message': conflict[0],
                       'details': conflict[1]})
    return events


def _fetch_job_log_texts(pandaid):
    texts = []
    try:
        from askpanda_atlas.log_analysis_impl import _fetch_log_text
        from decouple import config
        base_url = config('PANDA_BASE_URL', default='https://pandamon01.sdcc.bnl.gov')
        for filename in ('payload.stdout', 'payload.stderr', 'pilotlog.txt'):
            try:
                text = _fetch_log_text(pandaid, filename, base_url, timeout=30)
            except Exception as exc:
                logger.warning("epicprod inventory log fetch failed for %s/%s: %s",
                               pandaid, filename, exc)
                continue
            if text:
                texts.append(text)
    except Exception as exc:
        logger.warning("epicprod inventory log fetch unavailable for %s: %s", pandaid, exc)
    return texts


def cached_payload_log_parts(jeditaskid, pandaid):
    """Read payload-log cache members written by the prod-ops agent."""
    if not (jeditaskid and pandaid):
        return []
    cache_root = getattr(settings, 'SWF_TMP_DIR', '/data/swf-tmp')
    jobdir = os.path.join(cache_root, 'panda-logs', str(jeditaskid), str(pandaid))
    if not os.path.isfile(os.path.join(jobdir, '.done')):
        return []
    parts = []
    for name in PAYLOAD_LOG_MEMBERS:
        path = os.path.join(jobdir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', errors='replace') as f:
                text = f.read()
        except OSError as exc:
            text = f'(could not read {name}: {exc})'
        parts.append({'name': name, 'text': text})
    return parts


def cached_payload_log_texts(jeditaskid, pandaid):
    return [part['text'] for part in cached_payload_log_parts(jeditaskid, pandaid)]


def diagnosis_from_log_texts(log_texts, job=None):
    """Derive the ePIC production phase from payload log text."""
    job = job or {}
    combined_log_text = '\n'.join(t for t in log_texts if t)
    timeline = _timeline_from_log_text(combined_log_text)
    conflict = _rucio_conflict_details(combined_log_text)

    phase = ''
    failure_summary = ''
    if conflict:
        phase = 'rucio_registration_failed'
        failure_summary = conflict[0]
    elif timeline:
        phase = timeline[-1]['phase']
    elif job.get('jobstatus') in ('failed', 'closed'):
        phase = 'failed'
        failure_summary = (job.get('piloterrordiag') or '').strip()

    return {
        'available': bool(phase or failure_summary or timeline),
        'phase': phase,
        'failure_summary': failure_summary,
        'timeline': timeline,
        'guidance': (
            'Use phase/failure_summary as the production-facing diagnosis. '
            'This is parsed from payload logs and app inventory, and can be '
            'more specific than the top-level PanDA pilot error for '
            'payload-managed input/output workflows.'
        ),
    }


def diagnosis_for_study_data(study_data, epicprod_job=None):
    """Return persisted or cache-derived production diagnosis for a job page/tool."""
    if epicprod_job:
        data = epicprod_job.data or {}
        return {
            'available': True,
            'phase': epicprod_job.phase,
            'failure_summary': epicprod_job.failure_summary,
            'timeline': data.get('timeline') or [],
            'last_refreshed_at': (
                epicprod_job.last_refreshed_at.isoformat()
                if epicprod_job.last_refreshed_at else ''
            ),
            'source': 'epicprod_inventory',
            'guidance': (
                'Use phase/failure_summary as the production-facing diagnosis. '
                'This is parsed from payload logs and app inventory, and can be '
                'more specific than the top-level PanDA pilot error for '
                'payload-managed input/output workflows.'
            ),
        }

    job = study_data.get('job') or {}
    pandaid = study_data.get('pandaid') or job.get('pandaid')
    jeditaskid = job.get('jeditaskid')
    log_analysis = study_data.get('log_analysis') or {}
    log_texts = [log_analysis.get('log_excerpt') or '']
    cached_texts = cached_payload_log_texts(jeditaskid, pandaid)
    log_texts.extend(cached_texts)
    diagnosis = diagnosis_from_log_texts(log_texts, job=job)
    diagnosis['last_refreshed_at'] = ''
    diagnosis['source'] = 'payload_log_cache' if cached_texts else 'study_job'
    return diagnosis


def sync_job_from_study_data(study_data):
    """Persist epicprod diagnosis from an existing study_job() result."""
    job = study_data.get('job') or {}
    pandaid = int(study_data.get('pandaid') or job.get('pandaid'))
    jeditaskid = job.get('jeditaskid')
    files = study_data.get('files') or []
    seq_number = _seq_number_from_files(files)
    prod_task = _prod_task_for_jeditaskid(jeditaskid)

    if prod_task:
        sync_expected_files_for_task(prod_task)

    log_analysis = study_data.get('log_analysis') or {}
    log_texts = [log_analysis.get('log_excerpt') or '']
    log_texts.extend(cached_payload_log_texts(jeditaskid, pandaid))
    log_texts.extend(_fetch_job_log_texts(pandaid))
    diagnosis = diagnosis_from_log_texts(log_texts, job=job)
    phase = diagnosis['phase']
    failure_summary = diagnosis['failure_summary']
    timeline = diagnosis['timeline']

    data = {
        'panda': {
            k: job.get(k)
            for k in (
                'pandaid', 'jeditaskid', 'jobname', 'jobstatus', 'computingsite',
                'creationtime', 'starttime', 'endtime', 'piloterrorcode',
                'piloterrordiag', 'transexitcode', 'noutputdatafiles',
                'outputfilebytes',
            )
            if k in job
        },
        'timeline': timeline,
        'log_analysis': _jsonable(log_analysis),
    }

    epic_job, _ = EpicProdJob.objects.update_or_create(
        pandaid=pandaid,
        defaults={
            'jeditaskid': jeditaskid,
            'prod_task': prod_task,
            'seq_number': seq_number,
            'job_index': seq_number - 1 if seq_number else None,
            'status': job.get('jobstatus') or '',
            'phase': phase,
            'failure_summary': failure_summary,
            'data': data,
            'last_refreshed_at': timezone.now(),
        },
    )

    if jeditaskid and seq_number:
        EpicProdFile.objects.filter(
            jeditaskid=jeditaskid,
            seq_number=seq_number,
            pandaid__isnull=True,
        ).update(
            job=epic_job,
            pandaid=pandaid,
            job_index=seq_number - 1,
        )

    if conflict and jeditaskid and seq_number:
        detail, detail_data = conflict
        for f in EpicProdFile.objects.filter(
            jeditaskid=jeditaskid,
            seq_number=seq_number,
            role='output',
            stage='RECO',
        ):
            f.status = 'conflict'
            f.status_detail = detail
            merged = dict(f.data or {})
            merged['rucio_conflict'] = detail_data
            f.data = merged
            f.save(update_fields=['status', 'status_detail', 'data', 'updated_at'])

    return epic_job


def inventory_for_job_context(study_data):
    """Return display context for job pages.

    Safe to call before the migration exists: database table errors produce a
    fallback filtered PanDA file list.
    """
    panda_files = [
        f for f in (study_data.get('files') or [])
        if not is_pseudo_panda_file(f)
    ]
    rows = []
    epic_job = None
    try:
        pandaid = int(study_data.get('pandaid') or (study_data.get('job') or {}).get('pandaid'))
        epic_job = EpicProdJob.objects.filter(pandaid=pandaid).first()
        if epic_job:
            for f in EpicProdFile.objects.filter(job=epic_job).order_by('role', 'stage', 'lfn'):
                rows.append({
                    'role': f.role,
                    'stage': f.stage,
                    'scope': f.scope,
                    'dataset_name': f.dataset_name,
                    'did_name': f.did_name,
                    'lfn': f.lfn,
                    'size': f.bytes,
                    'status': f.status,
                    'status_detail': f.status_detail,
                    'rse': f.rse_expected,
                    'source': f.source,
                    'data': f.data or {},
                })
    except (OperationalError, ProgrammingError):
        return {'epicprod_job': None, 'display_files': _panda_display_rows(panda_files)}

    existing = {(r.get('source'), r.get('lfn'), r.get('did_name')) for r in rows}
    for r in _panda_display_rows(panda_files):
        key = (r.get('source'), r.get('lfn'), r.get('did_name'))
        if key not in existing:
            rows.append(r)
    return {'epicprod_job': epic_job, 'display_files': rows or _panda_display_rows(panda_files)}


def _panda_display_rows(panda_files):
    rows = []
    for f in panda_files:
        rows.append({
            'role': 'log' if f.get('type') == 'log' else f.get('type', ''),
            'stage': 'PANDA_LOG' if f.get('type') == 'log' else '',
            'scope': f.get('scope') or '',
            'dataset_name': f.get('dataset') or f.get('destinationdblock') or '',
            'did_name': f.get('lfn') or '',
            'lfn': f.get('lfn') or '',
            'size': f.get('fsize'),
            'status': f.get('status') or '',
            'status_detail': '',
            'rse': '',
            'source': 'panda_filestable',
            'data': _jsonable(f),
        })
    return rows
