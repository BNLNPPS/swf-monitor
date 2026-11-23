"""register-evgen-rucio.py's DID and PFN for a door path: the deterministic
form EIC-XRD expects (prefix //cache/eic/EPIC), so add_replicas accepts it."""
import importlib.util
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[3] / 'scripts'
spec = importlib.util.spec_from_file_location('register_evgen_rucio',
                                              SCRIPTS / 'register-evgen-rucio.py')
reg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reg)

DIR = '/EVGEN/EW_BSM/ALP/madgraph5-3.7.0-1.0/aem-axem/10x110/ma_1.0'
FILE = DIR + '/madgraph5-3.7.0-1.0_aem-axem_10x110_ma_1.0_run000.hepmc3.tree.root'


class PlanDatasetsTests(unittest.TestCase):
    def test_base_is_the_cache_area(self):
        self.assertEqual(reg.XRD_BASE, '/cache/eic/EPIC')

    def test_name_and_pfn_from_a_cache_door_path(self):
        datasets = reg.plan_datasets([(reg.XRD_BASE + FILE, 10956266)],
                                     {reg.XRD_BASE + FILE: '0a1b2c3d'})
        self.assertEqual(list(datasets), [DIR])
        entry, = datasets[DIR]
        self.assertEqual(entry['name'], FILE)
        self.assertEqual((entry['scope'], entry['bytes'], entry['adler32']),
                         ('epic', 10956266, '0a1b2c3d'))
        self.assertEqual(entry['pfn'],
                         f'{reg.XRD_DOOR}//cache/eic/EPIC/{FILE}')
        self.assertTrue(entry['pfn'].startswith(
            'root://dtn-rucio.jlab.org:1094//cache/eic/EPIC//EVGEN/'))


if __name__ == '__main__':
    unittest.main()
