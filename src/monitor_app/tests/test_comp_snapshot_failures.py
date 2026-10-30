"""The room's failures part: Snapper categories, catalog labels, the correction root."""
from unittest import mock

from django.test import SimpleTestCase

from monitor_app import comp_snapshot
from monitor_app.comp_snapshot import FRONT_STATES


class _Rule:
    component = 'pilot'
    code = 1305
    diag_substring = ''
    queue_scope = ''
    corrected_label = 'payload failure behind a stale apptainer line'
    note = ''
    evidence_url = ''


class TopFailuresTests(SimpleTestCase):
    def _builder(self):
        b = comp_snapshot._Build.__new__(comp_snapshot._Build)
        b.S = 'doma_panda'
        b.hour_ago = 'x'
        return b

    def test_ddm_failures_are_named_not_unattributed(self):
        rows = [('BNL_OSG_PanDA_1', 'ddm', 200, 'Could not add files to DDM: bytes mismatch', '0', 616),
                ('BNL_OSG_PanDA_1', 'pilot', 1152, 'File transfer timed out', None, 2),
                ('UM_GREX_PanDA_1', 'none', 0, '', None, 1)]
        with mock.patch.object(comp_snapshot, '_rows', return_value=rows), \
                mock.patch.object(comp_snapshot.error_corrections, '_rules', return_value=[]):
            out = self._builder().top_failures()
        self.assertEqual(out['BNL_OSG_PanDA_1'][0], ('ddm 200 - Could not add files to DDM (registration refused)', 616))
        self.assertEqual(out['BNL_OSG_PanDA_1'][1][1], 2)
        self.assertTrue(out['BNL_OSG_PanDA_1'][1][0].startswith('pilot 1152'))
        self.assertEqual(out['UM_GREX_PanDA_1'], [('failed with no error code in any component', 1)])
        self.assertNotIn('unattributed', str(out))

    def test_exit_codes_merge_into_one_category(self):
        rows = [('Q', 'executor', 78, 'registration', '78', 5), ('Q', 'executor', 78, 'registration', '78', 3)]
        with mock.patch.object(comp_snapshot, '_rows', return_value=rows), \
                mock.patch.object(comp_snapshot.error_corrections, '_rules', return_value=[]):
            out = self._builder().top_failures()
        self.assertEqual(out['Q'], [('executor 78', 8)])

    def test_a_correction_rule_rewrites_the_label(self):
        rows = [('NERSC_Perlmutter_epic', 'pilot', 1305, 'bind mounting /global', '78', 40)]
        with mock.patch.object(comp_snapshot, '_rows', return_value=rows), \
                mock.patch.object(comp_snapshot.error_corrections, '_rules', return_value=[_Rule()]), \
                mock.patch.object(comp_snapshot.error_corrections, 'exit_reading', return_value='Rucio registration failed (exit 78)'):
            out = self._builder().top_failures()
        self.assertEqual(out['NERSC_Perlmutter_epic'], [('Rucio registration failed (exit 78)', 40)])

    def test_a_degraded_front_holds_releases_not_the_queue(self):
        self.assertEqual(FRONT_STATES['degraded'], ('red', 'holds new releases, a gate is red'))
