"""Host-disk alarms distinguish early pressure, recovery and lost visibility."""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alarms'))
from swf_alarms.alarms import host_disk_space as alarm


class HostDiskSpaceTests(unittest.TestCase):
    def detect(self, volumes, age=0):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=age)
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [dict(key='host_report:osgsub01', built_at=stamp,
            value={'collected_at': stamp.isoformat(), 'health': {'volumes': volumes}})]
        conn = SimpleNamespace(cursor=lambda: cursor)
        return list(alarm.detect(SimpleNamespace(db_conn=conn), {'hosts': 'osgsub01'}))

    def test_pressure_warns_before_full_and_aliases_are_not_repeated(self):
        volume = dict(device=2, used_percent=90, free_bytes=26 * 2**30)
        found = self.detect({'/var': volume, '/data': volume})
        self.assertEqual(len(found), 1)
        self.assertIn('/var 90.0% full, 26.0 GiB free', found[0].subject)
        self.assertEqual(len(found[0].extra_data['volumes']), 1)

    def test_healthy_space_clears_and_small_root_is_not_false_alarm(self):
        self.assertEqual(self.detect({'/': dict(used_percent=71, free_bytes=3 * 2**30)}), [])

    def test_missing_or_stale_reading_stays_an_incident(self):
        low = {'/var': dict(used_percent=97, free_bytes=8 * 2**30)}
        for found in (self.detect(low), self.detect(low, age=20), self.detect({})):
            self.assertEqual(found[0].dedupe_key, 'host-disk:osgsub01')
        self.assertIn('monitoring unavailable', self.detect(low, age=20)[0].subject)

    def test_reporter_records_unprivileged_free_space(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/harvester-reporter.py'
        spec = importlib.util.spec_from_file_location('reporter_disk', path)
        reporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reporter)
        health = reporter.disk_health()
        self.assertGreater(health['volumes']['/']['total_bytes'], 0)
        self.assertIn('free_bytes', health['volumes']['/'])


if __name__ == '__main__':
    unittest.main()
