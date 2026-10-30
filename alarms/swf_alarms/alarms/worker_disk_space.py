"""Warn on worker filesystem pressure before the pilot's final 1 GB abort."""
import json

from ..common import Detection

PARAMS = {'warn_free_fraction': 0.20, 'critical_free_fraction': 0.10,
          'warn_free_gb': 20, 'warn_inode_fraction': 0.10}


def _key(volume):
    return volume['site'] + ':' + volume['volume']


def _last_measured(client):
    """The newest retained measurement of each volume, from the history product."""
    with client.db_conn.cursor() as cur:
        cur.execute('SELECT value FROM swf_cached_product WHERE key = %s',
                    ['worker_disk_space_history:v1'])
        row = cur.fetchone()
    value = (row or {}).get('value') or {}
    if isinstance(value, str):
        value = json.loads(value)
    last = {}
    for sample in value.get('samples', []):
        rows = {}
        for volume in sample.get('volumes', []):
            rows.setdefault(_key(volume), []).append(volume)
        last.update(rows)
    return last


def _judge(volume, params):
    free = volume['free_bytes'] / 1e9
    fraction = volume['free_fraction']
    inode_total = volume.get('total_inodes', 0)
    inode_fraction = volume.get('free_inodes', 0) / inode_total if inode_total else 1
    if (fraction >= float(params.get('warn_free_fraction', 0.20))
            and free >= float(params.get('warn_free_gb', 20))
            and inode_fraction >= float(params.get('warn_inode_fraction', 0.10))):
        return None
    critical = fraction < float(params.get('critical_free_fraction', 0.10)) or free < 5
    return Detection('worker-disk:' + _key(volume),
                     'GREX {} disk {}: {:.1f} GB free ({:.1f}%)'.format(
                         volume['volume'], 'critical' if critical else 'low', free, fraction * 100),
                     'Filesystem headroom from native pilot job {}, sampled at {}. '
                     'Free space or use the site scratch before pilots abort at 1 GB. '
                     'Available inodes: {} of {}. Seven days of measurements are retained '
                     'in worker_disk_space_history:v1.'.format(
                         volume['pandaid'], volume['observed_at'],
                         volume.get('free_inodes'), inode_total),
                     dict(volume, severity='critical' if critical else 'warning'))


def detect(client, params):
    # A five-minute alarm poll must not judge the previous cached response
    # while its replacement builds in the background.
    record = client._get('/api/worker-disk-space/', {'refresh': '1'})
    lost = (record.get('age_seconds') is None or record.get('age_seconds', 0) > 180
            or record.get('running_jobs') and
            (not record.get('reporting_jobs') or
             not any(v.get('volume') == 'ce' for v in record.get('volumes', []))))
    if lost:
        yield Detection('worker-disk:UM_GREX_PanDA_1:visibility',
                        'GREX worker disk monitoring unavailable',
                        'No fresh shared CE-spool headroom measurement is available; '
                        'capacity cannot be confirmed. Restore reporting; missing data is not recovery.', record)
    measured = {}
    for volume in record.get('volumes', []):
        measured.setdefault(_key(volume), []).append(volume)
    if lost:
        # A volume no longer measured keeps the verdict of its last retained
        # measurement, so lost visibility never clears a pressure incident.
        for key, volumes in _last_measured(client).items():
            measured.setdefault(key, volumes)
    # The summary keeps a worker volume's worst sample by each measure; one
    # incident per volume carries the most severe of them.
    for volumes in measured.values():
        found = [d for d in (_judge(v, params) for v in volumes) if d]
        if found:
            yield max(found, key=lambda d: (d.extra_data['severity'] == 'critical',
                                            -d.extra_data['free_fraction']))
