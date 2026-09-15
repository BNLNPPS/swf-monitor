"""The declared record's pure readers (monitor_app/declared.py): CRIC
documents to records, a record's standing in time, the attribution
window, the line a queue row shows. No store."""
from datetime import datetime, timezone as dt_timezone

from monitor_app.declared import (
    declared_at, rules_from_pandaqueuestatus, rules_from_ddmendpointstatus,
    standing_of, summary_line, windows_from_downtime)


# Xin's rule of 2026-09-14 as CRIC returned it (showall=1).
PQS = {
    'E1_BNL': {'a': {
        'mode': {'OFFLINE': {'manual': {
            'activity': 'a', 'expiration': '2026-09-15T00:21:00',
            'last_modified': '2026-09-14T02:20:19.409339',
            'operationdn': 'xzhao@bnl.gov', 'pandaqueue': 'E1_BNL',
            'probe': 'manual', 'reason': 'scheduled downtime',
            'updated': '2026-09-14T02:20:19.404270', 'value': 'OFFLINE'}}},
        'status': {'value': 'OFFLINE'}}},
    'CC-IN2P3_Rubin_Merge': {'a': {'mode': {'BROKEROFF': {'manual': {
        'expiration': '2029-12-31T17:06:00', 'updated': '2023-05-03T19:06:54',
        'operationdn': 'someone', 'reason': 'manual'}}}}},
}


def _t(s):
    return datetime.fromisoformat(s).replace(tzinfo=dt_timezone.utc)


def test_queue_rule_becomes_one_record_in_scope():
    rows = rules_from_pandaqueuestatus(PQS, {'E1_BNL', 'BNL_PanDA_1'})
    assert [r['name'] for r in rows] == ['queue:E1_BNL:a:OFFLINE:manual']
    r = rows[0]
    assert (r['kind'], r['target'], r['value'], r['activity']) == ('queue', 'E1_BNL', 'OFFLINE', 'a')
    assert r['end'] == '2026-09-15T00:21:00+00:00'
    assert r['declared_by'] == 'xzhao@bnl.gov' and r['reason'] == 'scheduled downtime'
    assert r['start'] is None


def test_standing_follows_the_clock():
    r = rules_from_pandaqueuestatus(PQS, {'E1_BNL'})[0]
    assert standing_of(r, _t('2026-09-14T12:00:00')) == 'active'
    assert standing_of(r, _t('2026-09-15T00:21:00')) == 'expired'
    window = {'start': '2026-09-20T08:00:00+00:00', 'end': '2026-09-20T16:00:00+00:00'}
    assert standing_of(window, _t('2026-09-15T12:00:00')) == 'future'
    assert standing_of(window, _t('2026-09-20T09:00:00')) == 'active'


def test_attribution_covers_the_window_and_the_cache_lag():
    r = rules_from_pandaqueuestatus(PQS, {'E1_BNL'})[0]
    # Zhaoyu's pilot read OFFLINE at 00:24 UTC, three minutes after expiry.
    assert declared_at([r], _t('2026-09-15T00:24:13'), kind='queue', target='E1_BNL')
    assert not declared_at([r], _t('2026-09-15T00:35:00'), kind='queue', target='E1_BNL')
    assert not declared_at([r], _t('2026-09-14T01:00:00'), kind='queue', target='E1_BNL')
    assert not declared_at([r], _t('2026-09-14T12:00:00'), kind='queue', target='E1_JLAB')


def test_endpoint_rule_and_downtime_window():
    eps = rules_from_ddmendpointstatus(
        {'BNL_PROD_DISK_1': {'w': {'mode': {'OFF': {'manual': {
            'expiration': '2026-09-14T22:00:00', 'updated': '2026-09-14T02:00:00',
            'operationdn': 'xzhao@bnl.gov', 'reason': 'scheduled downtime'}}}}}},
        {'BNL_PROD_DISK_1'})
    assert eps[0]['name'] == 'endpoint:BNL_PROD_DISK_1:w:OFF:manual'
    assert eps[0]['kind'] == 'endpoint' and eps[0]['activity'] == 'w'
    wins = windows_from_downtime(
        {'123': {'id': 123, 'rc_site': 'BNL-OSG', 'severity': 'OUTAGE',
                 'classification': 'SCHEDULED', 'start_time': '2026-09-20T08:00:00',
                 'end_time': '2026-09-20T16:00:00', 'description': 'farm maintenance',
                 'info_url': 'https://example/x', 'affected_services': ['BNL-CE-1']},
         '124': {'id': 124, 'rc_site': 'CERN-PROD', 'start_time': '2026-09-20T08:00:00'}},
        {'BNL-OSG'})
    assert [w['name'] for w in wins] == ['site:BNL-OSG:123']
    assert wins[0]['start'] == '2026-09-20T08:00:00+00:00' and wins[0]['services'] == ['BNL-CE-1']


def test_summary_lines_read_as_an_operator_would():
    r = rules_from_pandaqueuestatus(PQS, {'E1_BNL'})[0]
    assert summary_line(r) == 'offline until 09/15 00:21 UTC: scheduled downtime (xzhao@bnl.gov)'
    w = windows_from_downtime(
        {'1': {'id': 1, 'rc_site': 'BNL-OSG', 'severity': 'OUTAGE',
               'start_time': '2026-09-20T08:00:00', 'end_time': '2026-09-20T16:00:00',
               'description': 'farm maintenance', 'provider': 'gocdb'}}, {'BNL-OSG'})[0]
    assert summary_line(w) == 'outage 09/20 08:00 UTC to 09/20 16:00 UTC: farm maintenance (gocdb)'
