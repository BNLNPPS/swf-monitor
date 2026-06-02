"""
Command generation for PCS production tasks.

Generates Condor (submit_csv.sh) and PanDA (prun) submission commands
from a fully specified ProdTask (Dataset + ProdConfig + overrides).

Reference repos:
- eic/job_submission_condor — Condor submission framework
- eic/simulation_campaign_hepmc3 — in-container execution pipeline
- eic/simulation_campaign_datasets — CSV input files
"""

import shlex


def build_condor_command(task):
    """
    Build the Condor submit_csv.sh command from a ProdTask.

    Produces the env-var-prefixed command used in the Colab notebook:
        EBEAM=... PBEAM=... scripts/submit_csv.sh osg_csv hepmc3 {csv} {hours}
    """
    ds = task.output_dataset
    cfg = task.get_effective_config()
    physics = ds.physics_tag.parameters
    data = cfg.get('data') or {}

    env = {}
    # Beam energies from physics tag
    env['EBEAM'] = str(physics.get('beam_energy_electron', ''))
    env['PBEAM'] = str(physics.get('beam_energy_hadron', ''))

    # Detector from dataset
    env['DETECTOR_VERSION'] = ds.detector_version
    env['DETECTOR_CONFIG'] = ds.detector_config

    # Software stack from config
    env['JUG_XL_TAG'] = cfg.get('jug_xl_tag') or ''

    # Output flags
    env['COPYRECO'] = 'true' if cfg.get('copy_reco') else 'false'
    env['COPYFULL'] = 'true' if cfg.get('copy_full') else 'false'
    env['COPYLOG'] = 'true' if cfg.get('copy_log') else 'false'

    if cfg.get('use_rucio'):
        env['USERUCIO'] = 'true'
        env['X509_USER_PROXY'] = 'secrets/x509_user_proxy'

    # Rucio RSE override
    if cfg.get('rucio_rse'):
        env['OUT_RSE'] = cfg['rucio_rse']

    # External EVGEN input source (CSV manifest etc.)
    csv_path = task.input_source_location
    if csv_path:
        env['CSV_FILE'] = csv_path

    # Background mixing (conditional)
    if cfg.get('bg_mixing'):
        evgen = ds.evgen_tag.parameters
        env['SIGNAL_FREQ'] = str(evgen.get('signal_freq', '0'))
        env['SIGNAL_STATUS'] = str(evgen.get('signal_status', '0'))
        if evgen.get('bg_tag_prefix'):
            env['TAG_PREFIX'] = evgen['bg_tag_prefix']
        if evgen.get('bg_files'):
            env['BG_FILES'] = evgen['bg_files']

    # Build env string (skip empty values)
    env_str = ' \\\n  '.join(f'{k}={v}' for k, v in env.items() if v)

    # Target hours from config (default 2)
    target_hours = cfg.get('target_hours_per_job') or 2

    csv = csv_path or '<csv_file>'
    cmd = f'scripts/submit_csv.sh osg_csv hepmc3 {csv} {target_hours}'

    return f'{env_str} \\\n  {cmd}'


def build_panda_command(task):
    """
    Build the PanDA prun command from a ProdTask.

    Produces prun arguments for PanDA submission. The actual submission
    uses PrunScript.main() from pandaclient, but this generates the
    equivalent CLI command for reference/execution.
    """
    ds = task.output_dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}

    parts = ['prun']

    # Exec command (the payload) — shlex.quote so inner quotes survive
    exec_cmd = data.get('exec_command', '')
    if exec_cmd:
        parts.append(f'--exec {shlex.quote(exec_cmd)}')

    # Output dataset — task_name (no scope prefix, no .bN block suffix),
    # matching how PanDA records the production taskName.
    parts.append(f'--outDS {ds.task_name}')
    # Official group production (group.EIC scope, EIC.production privilege)
    if data.get('official'):
        parts.append('--official')

    # Container image
    container = cfg.get('container_image') or ''
    if container:
        parts.append(f'--containerImage {container}')

    # Site and queue
    if cfg.get('panda_site'):
        parts.append(f'--site {cfg["panda_site"]}')

    # Working group
    if cfg.get('panda_working_group'):
        parts.append(f'--workingGroup {cfg["panda_working_group"]}')

    # Resource type
    if cfg.get('panda_resource_type'):
        parts.append(f'--resourceType {cfg["panda_resource_type"]}')

    # Authorization
    prod_source = data.get('prod_source_label', 'test')
    parts.append(f'--prodSourceLabel {prod_source}')

    # VO
    vo = data.get('vo', 'wlcg')
    parts.append(f'--vo {vo}')

    # Processing type
    if data.get('processing_type'):
        parts.append(f'--processingType {data["processing_type"]}')

    # Job/event counts
    if data.get('n_jobs'):
        parts.append(f'--nJobs {data["n_jobs"]}')
    if data.get('events_per_job'):
        parts.append(f'--nEventsPerJob {data["events_per_job"]}')

    # Core count
    if data.get('corecount'):
        parts.append(f'--nCore {data["corecount"]}')

    # Flags
    if data.get('no_build'):
        parts.append('--noBuild')
    if data.get('skip_scout'):
        parts.append('--expertOnly_skipScout')

    # JEDI-managed outputs (simple payloads). Real production payloads that
    # self-register in Rucio use noOutput instead — then set neither.
    if data.get('outputs'):
        parts.append(f'--outputs {data["outputs"]}')

    return ' \\\n  '.join(parts)


def _build_env_string(task):
    """Shared env-var string for Condor command and JEDI jobParameters."""
    ds = task.output_dataset
    cfg = task.get_effective_config()
    physics = ds.physics_tag.parameters
    evgen = ds.evgen_tag.parameters

    env = {
        'EBEAM': str(physics.get('beam_energy_electron', '')),
        'PBEAM': str(physics.get('beam_energy_hadron', '')),
        'DETECTOR_VERSION': ds.detector_version,
        'DETECTOR_CONFIG': ds.detector_config,
        'JUG_XL_TAG': cfg.get('jug_xl_tag') or '',
        'COPYRECO': 'true' if cfg.get('copy_reco') else 'false',
        'COPYFULL': 'true' if cfg.get('copy_full') else 'false',
        'COPYLOG': 'true' if cfg.get('copy_log') else 'false',
    }
    # External EVGEN input source — payload-staged (see JEDI_INTEGRATION.md
    # § External EVGEN Inputs). The payload run.sh reads CSV_FILE and stages
    # the listed files at runtime.
    if task.input_source_location:
        env['CSV_FILE'] = task.input_source_location
    if cfg.get('bg_mixing'):
        if evgen.get('signal_freq') is not None:
            env['SIGNAL_FREQ'] = str(evgen['signal_freq'])
        if evgen.get('signal_status') is not None:
            env['SIGNAL_STATUS'] = str(evgen['signal_status'])
        if evgen.get('bg_tag_prefix'):
            env['TAG_PREFIX'] = evgen['bg_tag_prefix']
        if evgen.get('bg_files'):
            env['BG_FILES'] = evgen['bg_files']
    return ' '.join(f'{k}={v}' for k, v in env.items() if v)


def build_task_dump(task):
    """
    Build a fully-resolved dict describing a ProdTask and everything it
    references: dataset, all four tags with parameters, the ProdConfig
    as stored, and the effective config (after task-level overrides).

    Suitable for human inspection or downstream tooling. Pure read.
    """
    ds = task.output_dataset
    cfg = task.prod_config

    def _tag(t):
        if t is None:
            return None
        return {
            'tag_label': t.tag_label,
            'tag_number': t.tag_number,
            'status': t.status,
            'description': t.description,
            'parameters': dict(t.parameters or {}),
            'created_by': t.created_by,
            'created_at': t.created_at.isoformat() if t.created_at else None,
        }

    def _cfg(c):
        if c is None:
            return None
        out = {}
        for f in c._meta.get_fields():
            if not hasattr(f, 'attname'):
                continue
            if f.name in ('id',):
                continue
            val = getattr(c, f.name, None)
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            out[f.name] = val
        return out

    effective = task.get_effective_config()
    for k, v in list(effective.items()):
        if hasattr(v, 'isoformat'):
            effective[k] = v.isoformat()

    return {
        'task': {
            'id': task.id,
            'name': task.name,
            'description': task.description,
            'status': task.status,
            'csv_file': task.csv_file,
            'overrides': task.overrides or {},
            'input_dataset_dids': [d.did for d in task.input_datasets],
            'output_dataset_dids': [d.did for d in task.output_datasets],
            'intermediate_dataset_dids': [d.did for d in task.intermediate_datasets],
            'created_by': task.created_by,
            'created_at': task.created_at.isoformat() if task.created_at else None,
            'updated_at': task.updated_at.isoformat() if task.updated_at else None,
        },
        'dataset': {
            'id': ds.id,
            'dataset_name': ds.dataset_name,
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'blocks': ds.blocks,
            'description': ds.description,
            'created_by': ds.created_by,
            'created_at': ds.created_at.isoformat() if ds.created_at else None,
        },
        'tags': {
            'physics': _tag(ds.physics_tag),
            'evgen':   _tag(ds.evgen_tag),
            'simu':    _tag(ds.simu_tag),
            'reco':    _tag(ds.reco_tag),
        },
        'prod_config': _cfg(cfg),
        'effective_config': effective,
    }


def build_task_params(task):
    """
    Build a JEDI ``taskParamMap`` dict from a ProdTask.

    The returned dict can be passed directly to
    ``pandaclient.Client.insertTaskParams()`` for JEDI submission.
    Pure mapping — no database writes, no network.

    Field mapping follows ``docs/JEDI_INTEGRATION.md``.
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}

    task_name = ds.task_name
    working_group = cfg.get('panda_working_group') or 'EIC'

    params = {
        # Identity
        'taskName': task_name,
        'userName': task.created_by,
        'vo': data.get('vo', 'eic'),
        'workingGroup': working_group,
        'campaign': ds.detector_version,

        # Processing
        'prodSourceLabel': data.get('prod_source_label', 'test'),
        'taskType': data.get('task_type', 'production'),
        'processingType': data.get('processing_type', 'epicproduction'),
        'taskPriority': data.get('task_priority', 900),

        # Executable (containerized)
        'transPath': data.get(
            'transformation',
            'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
        ),
        'transUses': '',
        'transHome': '',
        'architecture': '',
        'container_name': cfg.get('container_image') or '',

        # Splitting (MC generation: noInput=True)
        'noInput': True,
        'nFilesPerJob': data.get('files_per_job', 1),
        'coreCount': data.get('corecount', 1),
        'ramCount': data.get('ram_count', 2000),
        'ramUnit': 'MBPerCore',

        # Site selection
        'site': cfg.get('panda_site') or '',
        'cloud': data.get('cloud', working_group),
    }

    # Job count — for noInput tasks this drives the number of jobs
    if data.get('n_jobs'):
        params['nFiles'] = data['n_jobs']
    if data.get('events_per_job'):
        params['nEventsPerJob'] = data['events_per_job']
    if cfg.get('events_per_task'):
        params['nEvents'] = cfg['events_per_task']

    # Walltime in seconds (JEDI expects seconds)
    hours = cfg.get('target_hours_per_job')
    if hours is not None:
        params['walltime'] = int(float(hours) * 3600)

    # Flags
    if data.get('skip_scout'):
        params['skipScout'] = True
    if data.get('disable_auto_retry'):
        params['disableAutoRetry'] = True
    if cfg.get('use_rucio'):
        params['useRucio'] = True

    # Log/output dataset templates — use task_name (no .bN suffix)
    log_dataset = f'{ds.scope}:{task_name}.log'
    out_dataset = f'{ds.scope}:{task_name}'
    log_filename = f'{task_name}.log.${{SN}}.log.tgz'
    params['log'] = {
        'dataset': log_dataset,
        'type': 'template',
        'param_type': 'log',
        'token': 'local',
        'destination': 'local',
        'value': log_filename,
    }

    # jobParameters: env + exec command, then output template
    env_str = _build_env_string(task)
    exec_cmd = data.get('exec_command') or './run.sh'
    constant_value = f'{env_str} {exec_cmd}' if env_str else exec_cmd
    output_filename = f'{task_name}.${{SN}}.root'
    params['jobParameters'] = [
        {
            'type': 'constant',
            'value': constant_value,
        },
        {
            'type': 'template',
            'param_type': 'output',
            'token': 'local',
            'destination': 'local',
            'dataset': out_dataset,
            'value': output_filename,
            'offset': data.get('output_offset', 1000),
        },
    ]

    return params
