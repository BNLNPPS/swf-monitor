"""Advance capacity warning and lost visibility are different from recovery."""
import importlib.util
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace

from monitor_app.worker_disk_space import summarize
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'alarms'))
from swf_alarms.alarms import worker_disk_space as alarm


class WorkerDiskSpaceTests(unittest.TestCase):
    def metrics(self, free=38000, stamp=1000):
        return (f'diskSample={stamp} ceFreeMB={free} ceTotalMB=200000 '
                'ceIFree=900000 ceITotal=1000000')

    def detect(self, record):
        record = dict(record, age_seconds=0)
        return list(alarm.detect(SimpleNamespace(_get=lambda _: record), {}))

    def test_warns_before_one_gib_abort(self):
        record = summarize([('UM_GREX_PanDA_1', 1, self.metrics())], 1000)
        found = self.detect(record)
        self.assertEqual(len(found), 1)
        self.assertIn('low', found[0].subject)
        self.assertGreater(found[0].extra_data['free_bytes'], 30 * 2**30)

    def test_worst_fresh_measurement_is_used(self):
        result = summarize([('UM_GREX_PanDA_1', 1, self.metrics(80000)),
                            ('UM_GREX_PanDA_1', 2, self.metrics(1000))], 1000)
        self.assertEqual(result['volumes'][0]['pandaid'], 2)
        self.assertIn('critical', self.detect(result)[0].subject)

    def test_missing_and_stale_measurements_do_not_clear(self):
        for metrics in ('', self.metrics(stamp=1)):
            result = summarize([('UM_GREX_PanDA_1', 1, metrics)], 2000)
            self.assertIn('unavailable', self.detect(result)[0].subject)

    def test_healthy_space_clears(self):
        self.assertEqual(self.detect(summarize([('UM_GREX_PanDA_1', 1,
                                                self.metrics(80000))], 1000)), [])

    def test_worker_volume_does_not_substitute_for_missing_shared_spool(self):
        metrics = self.metrics(80000).replace('ce', 'pilot')
        record = summarize([('UM_GREX_PanDA_1', 1, metrics)], 1000)
        self.assertEqual(record['reporting_jobs'], 1)
        self.assertIn('unavailable', self.detect(record)[0].subject)

    def test_inode_pressure_warns_even_with_free_bytes(self):
        metrics = self.metrics(80000).replace('ceIFree=900000', 'ceIFree=10')
        self.assertEqual(len(self.detect(summarize([('UM_GREX_PanDA_1', 1, metrics)], 1000))), 1)


if __name__ == '__main__':
    unittest.main()
