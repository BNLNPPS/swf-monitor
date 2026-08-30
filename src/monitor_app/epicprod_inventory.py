import csv
import json
import logging
import os
import re
from datetime import datetime
from io import StringIO
from pathlib import PurePosixPath
from urllib.parse import urlparse

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


def _rucio_transfer_details(text):
    """Extract the failed Rucio operation and its actual storage target."""
    out_rses = re.findall(
        r'(?m)^\s*(?:export\s+)?OUT_RSE\s*=\s*[\'"]?([^\'"\s]+)',
        text,
    )
    out_rse = out_rses[-1] if out_rses else ''
    attempts = []
    current = None
    for line in text.splitlines():
        match = re.search(r'Trying upload with (\S+) to (\S+)', line)
        if match:
            current = {
                'protocol': match.group(1).lower(),
                'rse': match.group(2),
                'endpoint': '',
                'failed': False,
            }
            attempts.append(current)
            continue
        if current and 'Successful upload of temporary file.' in line:
            url_match = re.search(r'((?:root|https?|davs)://\S+)', line)
            if url_match:
                current['endpoint'] = (
                    urlparse(url_match.group(1)).hostname or '')
        if current and (
                'Upload attempt failed' in line
                or 'RSE checksum unavailable' in line
                or 'RSEChecksumUnavailable' in line):
            current['failed'] = True

    failed_attempts = [attempt for attempt in attempts if attempt['failed']]
    relevant = failed_attempts or attempts
    rse = next(
        (attempt['rse'] for attempt in reversed(relevant)
         if attempt.get('rse')),
        out_rse,
    )
    endpoints = list(dict.fromkeys(
        attempt['endpoint'] for attempt in relevant
        if attempt.get('endpoint')
    ))
    protocols_failed = list(dict.fromkeys(
        attempt['protocol'] for attempt in failed_attempts
        if attempt.get('protocol')
    ))

    conflict = _rucio_conflict_details(text)
    if not rse:
        copy_match = re.search(
            r'Found COPYING replica .* on (\S+)\s+[—-]\s+deleting',
            text,
        )
        if copy_match:
            rse = copy_match.group(1)

    checksum_unavailable = bool(
        re.search(
            r'(?:RSE checksum unavailable|RSEChecksumUnavailable|'
            r'Could not get the checksum)',
            text,
            re.IGNORECASE,
        )
    )
    upload_failed = bool(
        re.search(
            r'(?:Upload attempt failed|NoFilesUploaded|'
            r'None of the given files have been uploaded)',
            text,
            re.IGNORECASE,
        )
    )

    operation = ''
    cause_layer = 'unknown'
    cause_confidence = 'unresolved'
    failure_summary = ''
    if checksum_unavailable:
        operation = 'remote_checksum'
        cause_layer = 'storage'
        cause_confidence = (
            'confirmed' if rse and endpoints
            else 'supported' if rse
            else 'unresolved'
        )
        target = rse or (endpoints[0] if endpoints else 'remote storage')
        if rse and endpoints:
            target = f'{rse} at {endpoints[0]}'
        protocols = (
            f" over {' and '.join(protocols_failed)}"
            if protocols_failed else ''
        )
        failure_summary = f'{target} remote checksum unavailable{protocols}'
    elif conflict:
        operation = 'rucio_replica_conflict'
        cause_layer = 'data_management'
        cause_confidence = 'confirmed' if rse else 'supported'
        failure_summary = conflict[0]
        if rse:
            failure_summary = f'{rse}: {failure_summary}'
    elif upload_failed:
        operation = 'output_upload'
        cause_layer = 'storage'
        cause_confidence = (
            'supported' if rse or endpoints else 'unresolved')
        target = rse or (endpoints[0] if endpoints else 'remote storage')
        failure_summary = f'Output upload failed at {target}'

    return {
        'operation': operation,
        'rse': rse,
        'endpoint': endpoints[0] if endpoints else '',
        'protocols_failed': protocols_failed,
        'cause_layer': cause_layer,
        'cause_entity': rse or (endpoints[0] if endpoints else ''),
        'cause_confidence': cause_confidence,
        'failure_summary': failure_summary,
        'conflict': conflict,
        'attempted': bool(attempts or 'register_to_rucio.py' in text),
    }


def _timeline_from_log_text(text, transfer=None):
    transfer = transfer or _rucio_transfer_details(text)
    events = []
    if 'Finished processing.' in text:
        events.append({'phase': 'reconstruction_complete',
                       'message': 'eicrecon finished processing'})
    valid = re.search(r'VALID:\s+(\S+\.eicrecon\.edm4eic\.root)', text)
    if valid:
        events.append({'phase': 'reco_validation_passed',
                       'message': 'RECO ROOT file validation passed',
                       'path': valid.group(1)})
    if transfer['attempted']:
        details = {
            key: transfer[key]
            for key in ('rse', 'endpoint')
            if transfer.get(key)
        }
        events.append({
            'phase': 'output_registration_attempted',
            'message': 'Payload attempted Rucio output registration',
            'details': details,
        })
    if transfer['operation']:
        details = {
            key: transfer[key]
            for key in (
                'operation', 'rse', 'endpoint', 'protocols_failed',
                'cause_layer', 'cause_entity', 'cause_confidence',
            )
            if transfer.get(key) not in ('', [], None)
        }
        events.append({
            'phase': 'output_registration_failed',
            'message': transfer['failure_summary'],
            'details': details,
        })
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


_WORKER_ENDED_RE = re.compile(
    r'^The worker was (?P<worker>\w+) while the job was (?P<job>\w+)\s*:\s*(?P<sacct>.+)$')

# A PanDA job at an HPC site runs inside a harvester batch job (one Slurm
# allocation, many slots). When the batch job ends under a running PanDA job
# the server records taskbuffer 300 with the sacct line; the Slurm state in it
# is the batch system's own verdict and the top of the evidence ladder.
_SLURM_STATE_OPERATIONS = {
    'TIMEOUT': ('worker_walltime', 'confirmed'),
    'NODE_FAIL': ('node_failure', 'confirmed'),
    'OUT_OF_MEMORY': ('node_memory', 'confirmed'),
    'PREEMPTED': ('worker_preempted', 'confirmed'),
    'CANCELLED': ('worker_cancelled', 'confirmed'),
    'COMPLETED': ('worker_ended', 'supported'),
}
# A job started this close to the batch job's start that ran to the wall
# needed more than the batch job's limit; later starts were started too late.
_BATCH_START_GRACE_SECONDS = 15 * 60


def _as_datetime(value):
    if not value:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def _seconds_between(start, end):
    start, end = _as_datetime(start), _as_datetime(end)
    if start is None or end is None:
        return None
    try:
        seconds = (end - start).total_seconds()
    except TypeError:
        return None
    return seconds if seconds >= 0 else None


def _span_text(seconds):
    """'2 minutes' under an hour, else '3h58m'."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f'{minutes} minute{"s" if minutes != 1 else ""}'
    return f'{minutes // 60}h{minutes % 60:02d}m'


def _hours_text(seconds):
    """'4-hour' for a batch job's length, rounded to the hour; under an hour
    the minutes ('45-minute')."""
    hours = int(round(seconds / 3600))
    if hours < 1:
        return f'{int(seconds // 60)}-minute'
    return f'{hours}-hour'


def _worker_ended_reading(job, worker=None):
    """Read a taskbuffer 300 diagnostic ('The worker was finished while the
    job was running : <sacct line>') into (operation, confidence, summary),
    with the batch job's start and end from the harvester worker record when
    given. Returns None when the diagnostic is not of that form."""
    diag = str(job.get('taskbuffererrordiag') or '').strip()
    match = _WORKER_ENDED_RE.match(diag)
    if not match:
        return None
    worker = worker or {}
    tokens = match.group('sacct').split()
    # sacct columns: JobID User Partition Account AllocCPUS State ExitCode
    state = tokens[-2].upper() if len(tokens) >= 2 else ''
    exit_code = tokens[-1] if tokens else ''
    site = str(job.get('computingsite') or '') or 'the site'
    ran = _seconds_between(job.get('starttime'), job.get('endtime'))
    lost = f' Its {_span_text(ran)} of processing is lost.' if ran else ''
    batch_len = _seconds_between(worker.get('starttime'), worker.get('endtime'))
    offset = _seconds_between(worker.get('starttime'), job.get('starttime'))

    if match.group('job').lower() == 'starting':
        return ('worker_ended_before_start', 'confirmed',
                f'Never ran: its batch job at {site} ended before this job started.')

    operation, confidence = _SLURM_STATE_OPERATIONS.get(state, ('worker_ended', 'supported'))
    if state == 'TIMEOUT':
        if batch_len is None:
            summary = f'Killed at {site} when its batch job hit its time limit.{lost}'
        else:
            limit = _hours_text(batch_len)
            head = f'Killed at {site} when its {limit} batch job hit the limit.'
            if offset is not None and offset <= _BATCH_START_GRACE_SECONDS and ran:
                summary = (f'{head} This job started {_span_text(offset)} into the batch '
                           f'job and had run {_span_text(ran)}. It cannot finish in a '
                           f'{limit} batch job.')
            elif offset is not None:
                left = max(batch_len - offset, 0)
                summary = (f'{head} This job was started with only {_span_text(left)} '
                           f'of the batch job left.{lost}')
            else:
                summary = f'{head}{lost}'
    elif state == 'NODE_FAIL':
        summary = f'Killed at {site}: the node it was running on failed (Slurm NODE_FAIL).{lost}'
    elif state == 'OUT_OF_MEMORY':
        summary = f'Killed at {site}: its batch job ran out of memory (Slurm OUT_OF_MEMORY).{lost}'
    elif state == 'PREEMPTED':
        summary = f'Killed at {site}: its batch job was preempted.{lost}'
    elif state == 'CANCELLED':
        summary = f'Killed at {site}: its batch job was cancelled.{lost}'
    elif state == 'COMPLETED':
        after = f' after {_span_text(batch_len)}' if batch_len else ''
        summary = (f'Killed at {site}: its batch job exited normally{after} while this '
                   f'job was still running, and took the job down with it.{lost} Not a '
                   f'time limit, not a node failure; why the batch job exited early is '
                   f'not known.')
    else:
        state_text = f'{state} {exit_code}'.strip() or 'unknown'
        summary = f'Killed at {site}: its batch job ended with Slurm state {state_text}.{lost}'
    return operation, confidence, summary


def diagnosis_from_log_texts(log_texts, job=None, worker=None):
    """Derive production phase and causal attribution from job evidence.
    ``worker`` is the harvester worker record of the batch job the PanDA job
    ran in (start, end, diag), used when the batch job ended under the job."""
    job = job or {}
    combined_log_text = '\n'.join(t for t in log_texts if t)
    transfer = _rucio_transfer_details(combined_log_text)
    timeline = _timeline_from_log_text(combined_log_text, transfer=transfer)
    conflict = transfer.get('conflict')

    phase = ''
    failure_summary = ''
    operation = transfer['operation']
    cause_layer = transfer['cause_layer']
    cause_entity = transfer['cause_entity']
    cause_confidence = transfer['cause_confidence']
    if operation:
        phase = 'output_registration'
        failure_summary = transfer['failure_summary']
    elif timeline:
        phase = timeline[-1]['phase']
    else:
        worker_diag = ' | '.join(str(job.get(key) or '') for key in (
            'superrordiag', 'taskbuffererrordiag',
            'piloterrordiag', 'jobdispatchererrordiag',
        ))
        worker_lower = worker_diag.lower()
        site = str(job.get('computingsite') or '')
        if 'backofflimitexceeded' in worker_lower:
            phase = 'worker_execution'
            operation = 'worker_backoff_limit'
            cause_layer = 'compute'
            cause_entity = site
            cause_confidence = 'confirmed' if site else 'supported'
            failure_summary = 'Worker reached its backoff limit'
        elif 'imminent node shutdown' in worker_lower:
            phase = 'worker_execution'
            operation = 'node_shutdown'
            cause_layer = 'compute'
            cause_entity = site
            cause_confidence = 'confirmed' if site else 'supported'
            failure_summary = 'Worker terminated for imminent node shutdown'
        elif _worker_ended_reading(job, worker):
            # taskbuffer 300: the batch job ended under the PanDA job. The
            # Slurm state in the diagnostic separates the time limit, node
            # failure, and a batch job that exited on its own.
            operation, cause_confidence, failure_summary = _worker_ended_reading(job, worker)
            phase = 'worker_execution'
            cause_layer = 'compute'
            cause_entity = site
            if not site and cause_confidence == 'confirmed':
                cause_confidence = 'supported'
        elif 'kill by ' in worker_lower:
            phase = 'operator_cancelled'
            operation = 'operator_cancel'
            cause_layer = 'operator'
            cause_entity = str(job.get('produsername') or '')
            cause_confidence = 'confirmed'
            failure_summary = next(
                (str(job.get(key) or '').strip() for key in (
                    'taskbuffererrordiag', 'piloterrordiag')
                 if 'kill by ' in str(job.get(key) or '').lower()),
                'Job cancelled by operator',
            )
    if not phase and job.get('jobstatus') in ('failed', 'closed', 'cancelled'):
        phase = 'failed'
        failure_summary = (job.get('piloterrordiag') or '').strip()

    return {
        'available': bool(phase or failure_summary or timeline),
        'phase': phase,
        'failure_summary': failure_summary,
        'timeline': timeline,
        'conflict': conflict,
        'operation': operation,
        'rse': transfer['rse'],
        'endpoint': transfer['endpoint'],
        'protocols_failed': transfer['protocols_failed'],
        'payload_completed': 'Finished processing.' in combined_log_text,
        'validation_passed': bool(
            re.search(r'(?:RECO ROOT file validation passed|✓\s+VALID:)',
                      combined_log_text)),
        'cause_layer': cause_layer,
        'cause_entity': cause_entity,
        'cause_confidence': cause_confidence,
        'guidance': (
            'Use phase/failure_summary as the production-facing diagnosis. '
            'Causal claims require cause_layer, cause_entity, and '
            'cause_confidence from this structured evidence; execution site '
            'or a top-level PanDA diagnostic alone is not causal attribution.'
        ),
    }


def diagnosis_for_study_data(study_data, epicprod_job=None, fetch_logs=False):
    """Return persisted or cache-derived production diagnosis for a job page/tool."""
    if epicprod_job:
        data = epicprod_job.data or {}
        stored = data.get('diagnosis') or {}
        if stored or not fetch_logs:
            return {
                'available': True,
                'phase': epicprod_job.phase,
                'failure_summary': epicprod_job.failure_summary,
                'timeline': data.get('timeline') or [],
                'operation': stored.get('operation', ''),
                'rse': stored.get('rse', ''),
                'endpoint': stored.get('endpoint', ''),
                'protocols_failed': stored.get('protocols_failed') or [],
                'payload_completed': bool(stored.get('payload_completed')),
                'validation_passed': bool(stored.get('validation_passed')),
                'cause_layer': stored.get('cause_layer', 'unknown'),
                'cause_entity': stored.get('cause_entity', ''),
                'cause_confidence': stored.get(
                    'cause_confidence', 'unresolved'),
                'last_refreshed_at': (
                    epicprod_job.last_refreshed_at.isoformat()
                    if epicprod_job.last_refreshed_at else ''
                ),
                'source': 'epicprod_inventory',
                'guidance': (
                    'Causal claims require cause_layer, cause_entity, and '
                    'cause_confidence from this structured evidence.'
                ),
            }

    job = study_data.get('job') or {}
    pandaid = study_data.get('pandaid') or job.get('pandaid')
    jeditaskid = job.get('jeditaskid')
    log_analysis = study_data.get('log_analysis') or {}
    log_texts = [log_analysis.get('log_excerpt') or '']
    cached_texts = cached_payload_log_texts(jeditaskid, pandaid)
    log_texts.extend(cached_texts)
    fetched_texts = []
    if fetch_logs and not cached_texts:
        fetched_texts = _fetch_job_log_texts(pandaid)
        log_texts.extend(fetched_texts)
    diagnosis = diagnosis_from_log_texts(
        log_texts, job=job, worker=study_data.get('harvester'))
    diagnosis['last_refreshed_at'] = ''
    diagnosis['source'] = (
        'payload_log_cache' if cached_texts
        else 'live_payload_logs' if fetched_texts
        else 'study_job'
    )
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
    diagnosis = diagnosis_from_log_texts(
        log_texts, job=job, worker=study_data.get('harvester'))
    phase = diagnosis['phase']
    failure_summary = diagnosis['failure_summary']
    timeline = diagnosis['timeline']
    conflict = diagnosis.get('conflict')

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
        'diagnosis': {
            key: _jsonable(diagnosis.get(key))
            for key in (
                'operation', 'rse', 'endpoint', 'protocols_failed',
                'payload_completed', 'validation_passed', 'cause_layer',
                'cause_entity', 'cause_confidence',
            )
        },
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
