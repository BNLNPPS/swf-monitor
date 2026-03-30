"""
Command generation for PCS production tasks.

Generates Condor (submit_csv.sh) and PanDA (prun) submission commands
from a fully specified ProdTask (Dataset + ProdConfig + overrides).

Reference repos:
- eic/job_submission_condor — Condor submission framework
- eic/simulation_campaign_hepmc3 — in-container execution pipeline
- eic/simulation_campaign_datasets — CSV input files
"""


def build_condor_command(task):
    """
    Build the Condor submit_csv.sh command from a ProdTask.

    Produces the env-var-prefixed command used in the Colab notebook:
        EBEAM=... PBEAM=... scripts/submit_csv.sh osg_csv hepmc3 {csv} {hours}
    """
    ds = task.dataset
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

    # Background mixing (conditional)
    if cfg.get('bg_mixing'):
        evgen = ds.evgen_tag.parameters
        env['SIGNAL_FREQ'] = str(evgen.get('signal_freq', '0'))
        env['SIGNAL_STATUS'] = str(evgen.get('signal_status', '0'))
        if evgen.get('bg_tag_prefix'):
            env['TAG_PREFIX'] = evgen['bg_tag_prefix']
        if task.csv_file:
            env['CSV_FILE'] = task.csv_file
        if evgen.get('bg_files'):
            env['BG_FILES'] = evgen['bg_files']

    # Build env string (skip empty values)
    env_str = ' \\\n  '.join(f'{k}={v}' for k, v in env.items() if v)

    # Target hours from config (default 2)
    target_hours = cfg.get('target_hours_per_job') or 2

    csv = task.csv_file or '<csv_file>'
    cmd = f'scripts/submit_csv.sh osg_csv hepmc3 {csv} {target_hours}'

    return f'{env_str} \\\n  {cmd}'


def build_panda_command(task):
    """
    Build the PanDA prun command from a ProdTask.

    Produces prun arguments for PanDA submission. The actual submission
    uses PrunScript.main() from pandaclient, but this generates the
    equivalent CLI command for reference/execution.
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}

    parts = ['prun']

    # Exec command (the payload)
    exec_cmd = data.get('exec_command', '')
    if exec_cmd:
        parts.append(f'--exec "{exec_cmd}"')

    # Output dataset
    parts.append(f'--outDS {ds.did}')

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

    return ' \\\n  '.join(parts)
