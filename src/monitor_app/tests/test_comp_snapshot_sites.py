"""The room's sites take their queues and status from the queue registry
(swf-epicprod docs/QUEUE_REGISTRY.md)."""
import copy
from unittest.mock import patch

from swf_epicprod import queues as registry

from monitor_app import comp_snapshot

LIVE = {
    'front.queues': list(registry.SEED_FRONT_QUEUES),
    'front.closed_queues': dict(registry.SEED_CLOSED),
    'comp.sites': [dict(g, queues=copy.deepcopy(r['queues']))
                   for g, r in zip(comp_snapshot.DEFAULT_SITES, registry.SEED_ROOM)],
}


def _sites(config, groups):
    registry.invalidate()
    try:
        with patch.object(registry, '_config', return_value=config), \
             patch('monitor_app.models.SysConfig.get_setting', return_value=groups):
            return comp_snapshot._sites_config()
    finally:
        registry.invalidate()


def test_derived_record_reproduces_the_room_as_it_was():
    sites = _sites(copy.deepcopy(LIVE), LIVE['comp.sites'])
    shown = {s['id']: [(q['id'], q['status']) for q in s['queues']] for s in sites}
    assert [s['id'] for s in sites] == ['osg', 'grex', 'compute-canada', 'nersc', 'google', 'jlab']
    assert shown['osg'] == [('BNL_OSG_PanDA_1', None)]
    assert shown['grex'] == [('UM_GREX_PanDA_1', None)]
    assert shown['compute-canada'] == [('ComputeCanada_PanDA_1', 'not_built')]
    assert shown['nersc'] == [('NERSC_Perlmutter_epic', 'off'), ('NERSC_Perlmutter_epic_es', 'off')]
    assert shown['google'] == [('BNL_ePIC_GOOGLE', 'off'), ('BNL_ePIC_GOOGLE_es', 'off')]
    assert shown['jlab'] == [('JLAB_PanDA_1', 'off')]
    assert sites[0]['execute_sites'] is True


def test_adopted_record_and_stripped_groups_keep_the_room_and_follow_a_change():
    data = copy.deepcopy(LIVE)
    record, _ = registry.adopt_into(data, 'test', strip_room=True)
    assert all('queues' not in g for g in data['comp.sites'])
    registry.change(record, 'UM_GREX_PanDA_1', 'retired', reason='gone', by='test')
    registry.change(record, 'NERSC_Perlmutter_epic', 'production', by='test')
    registry.change(record, 'NEW_Q', 'commissioning', reason='new', by='test', site='elsewhere')
    sites = {s['id']: s for s in _sites(data, data['comp.sites'])}
    assert sites['grex']['queues'] == []
    assert sites['nersc']['queues'][0] == {'id': 'NERSC_Perlmutter_epic', 'status': None,
                                           'status_reason': ''}
    assert sites['elsewhere']['queues'][0]['status'] == 'off'


def test_drift_is_a_red_report_line_naming_the_copy():
    items = comp_snapshot._drift_items([{'queue': 'BNL_OSG_EPIC_PROD_1', 'copy': 'harvester on osgsub01-harvester',
                                         'holds': 'online', 'expected': 'offline (retired)'}])
    assert items[0]['level'] == 'red'
    assert items[0]['text'].startswith('Queue drift, BNL_OSG_EPIC_PROD_1: harvester on osgsub01-harvester holds online')
    assert comp_snapshot._drift_items([]) == []
