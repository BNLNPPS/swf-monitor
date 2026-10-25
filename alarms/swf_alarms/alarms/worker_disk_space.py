"""Warn on worker filesystem pressure before the pilot's final 1 GiB abort."""
from ..common import Detection

PARAMS = {'warn_free_fraction': 0.20, 'critical_free_fraction': 0.10,
          'warn_free_gib': 20, 'warn_inode_fraction': 0.10}


def detect(client, params):
    record = client._get('/api/worker-disk-space/')
    if (record.get('age_seconds') is None or record.get('age_seconds', 0) > 180
            or record.get('running_jobs') and not record.get('reporting_jobs')):
        yield Detection('worker-disk:UM_GREX_PanDA_1:visibility',
                        'GREX worker disk monitoring unavailable',
                        'Running jobs have no fresh filesystem headroom measurement; '
                        'capacity cannot be confirmed. Restore reporting; missing data is not recovery.', record)
    for volume in record.get('volumes', []):
        free = volume['free_bytes'] / 2**30
        fraction = volume['free_fraction']
        inode_total = volume.get('total_inodes', 0)
        inode_fraction = volume.get('free_inodes', 0) / inode_total if inode_total else 1
        if (fraction >= float(params.get('warn_free_fraction', 0.20))
                and free >= float(params.get('warn_free_gib', 20))
                and inode_fraction >= float(params.get('warn_inode_fraction', 0.10))):
            continue
        critical = fraction < float(params.get('critical_free_fraction', 0.10)) or free < 5
        yield Detection('worker-disk:' + volume['site'] + ':' + volume['volume'],
                        'GREX {} disk {}: {:.1f} GiB free ({:.1f}%)'.format(
                            volume['volume'], 'critical' if critical else 'low', free, fraction * 100),
                        'Filesystem headroom from native pilot job {}, sampled at {}. '
                        'Free space or use the site scratch before pilots abort at 1 GiB. '
                        'Available inodes: {} of {}. Seven days of measurements are retained '
                        'in worker_disk_space_history:v1.'.format(
                            volume['pandaid'], volume['observed_at'],
                            volume.get('free_inodes'), inode_total),
                        dict(volume, severity='critical' if critical else 'warning'))
