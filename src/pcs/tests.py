"""Tests for PCS command/task-param generation.

Uses lightweight fakes rather than Django fixtures — the builders are pure
mapping functions, so we do not need the ORM.
"""
from types import SimpleNamespace
from unittest import TestCase

from pcs.commands import build_task_params


def _make_task():
    """Build a SimpleNamespace task matching the example in docs/JEDI_INTEGRATION.md."""
    physics_tag = SimpleNamespace(
        tag_label='p3001',
        parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 100},
    )
    evgen_tag = SimpleNamespace(tag_label='e1', parameters={})
    simu_tag = SimpleNamespace(tag_label='s1', parameters={})
    reco_tag = SimpleNamespace(tag_label='r1', parameters={})

    task_name = 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1'
    dataset = SimpleNamespace(
        scope='group.EIC',
        detector_version='26.02.0',
        detector_config='epic_craterlake',
        physics_tag=physics_tag,
        evgen_tag=evgen_tag,
        simu_tag=simu_tag,
        reco_tag=reco_tag,
        block_num=1,
        did=f'group.EIC:{task_name}.b1',
        task_name=task_name,
    )

    cfg = {
        'panda_site': 'BNL_EPIC_PROD_1',
        'panda_working_group': 'EIC',
        'container_image': 'docker://eicweb/jug_xl:26.02.0-stable',
        'jug_xl_tag': '26.02.0-stable',
        'target_hours_per_job': 2,
        'events_per_task': 1000,
        'use_rucio': True,
        'copy_reco': True,
        'copy_full': False,
        'copy_log': True,
        'bg_mixing': False,
        'data': {
            'prod_source_label': 'managed',
            'processing_type': 'epicproduction',
            'n_jobs': 10,
            'events_per_job': 100,
            'corecount': 1,
            'ram_count': 4000,
            'transformation': 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
        },
    }

    return SimpleNamespace(
        dataset=dataset,
        created_by='wenaus',
        get_effective_config=lambda: cfg,
    )


class BuildTaskParamsTest(TestCase):
    def test_matches_design_example(self):
        params = build_task_params(_make_task())

        # Identity
        self.assertEqual(params['taskName'],
                         'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1')
        self.assertEqual(params['userName'], 'wenaus')
        self.assertEqual(params['vo'], 'eic')
        self.assertEqual(params['workingGroup'], 'EIC')
        self.assertEqual(params['campaign'], '26.02.0')

        # Processing
        self.assertEqual(params['prodSourceLabel'], 'managed')
        self.assertEqual(params['taskType'], 'production')
        self.assertEqual(params['processingType'], 'epicproduction')
        self.assertEqual(params['taskPriority'], 900)

        # Executable
        self.assertEqual(params['transPath'],
                         'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02')
        self.assertEqual(params['transUses'], '')
        self.assertEqual(params['transHome'], '')
        self.assertEqual(params['architecture'], '')
        self.assertEqual(params['container_name'],
                         'docker://eicweb/jug_xl:26.02.0-stable')

        # Splitting
        self.assertTrue(params['noInput'])
        self.assertEqual(params['nFiles'], 10)
        self.assertEqual(params['nFilesPerJob'], 1)
        self.assertEqual(params['nEventsPerJob'], 100)
        self.assertEqual(params['coreCount'], 1)
        self.assertEqual(params['ramCount'], 4000)
        self.assertEqual(params['ramUnit'], 'MBPerCore')
        self.assertEqual(params['walltime'], 7200)  # 2 hours

        # Site
        self.assertEqual(params['site'], 'BNL_EPIC_PROD_1')
        self.assertEqual(params['cloud'], 'EIC')

        # Log dataset
        self.assertEqual(params['log']['dataset'],
                         'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log')
        self.assertEqual(params['log']['param_type'], 'log')
        self.assertEqual(params['log']['type'], 'template')
        self.assertIn('.${SN}.log.tgz', params['log']['value'])

        # jobParameters
        constant_entry = params['jobParameters'][0]
        self.assertEqual(constant_entry['type'], 'constant')
        for expected in ('EBEAM=10', 'PBEAM=100',
                         'DETECTOR_VERSION=26.02.0',
                         'DETECTOR_CONFIG=epic_craterlake',
                         'JUG_XL_TAG=26.02.0-stable',
                         'COPYRECO=true', 'COPYFULL=false', 'COPYLOG=true',
                         './run.sh'):
            self.assertIn(expected, constant_entry['value'])

        output_entry = params['jobParameters'][1]
        self.assertEqual(output_entry['type'], 'template')
        self.assertEqual(output_entry['param_type'], 'output')
        self.assertEqual(output_entry['dataset'],
                         'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1')
        self.assertIn('.${SN}.root', output_entry['value'])
        self.assertEqual(output_entry['offset'], 1000)

    def test_defaults_when_data_missing(self):
        task = _make_task()
        # Strip data dict
        task.get_effective_config = lambda: {
            'panda_site': '',
            'panda_working_group': '',
            'container_image': '',
            'target_hours_per_job': None,
            'events_per_task': None,
            'use_rucio': False,
            'copy_reco': False,
            'copy_full': False,
            'copy_log': False,
            'bg_mixing': False,
            'data': {},
        }
        params = build_task_params(task)

        self.assertEqual(params['vo'], 'eic')
        self.assertEqual(params['workingGroup'], 'EIC')
        self.assertEqual(params['prodSourceLabel'], 'test')
        self.assertEqual(params['processingType'], 'epicproduction')
        self.assertEqual(params['coreCount'], 1)
        self.assertEqual(params['ramCount'], 2000)
        self.assertNotIn('walltime', params)
        self.assertNotIn('nFiles', params)
        self.assertNotIn('skipScout', params)
        self.assertNotIn('useRucio', params)
