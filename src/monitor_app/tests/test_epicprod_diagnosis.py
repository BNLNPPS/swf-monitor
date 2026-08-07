from django.test import SimpleTestCase

from monitor_app.epicprod_inventory import diagnosis_from_log_texts


class EpicProdDiagnosisTests(SimpleTestCase):
    def test_taiwan_checksum_failure_is_attributed_to_output_storage(self):
        stdout = """
export OUT_RSE=ASGC-XRD
Finished processing.
✓ VALID: /srv/job/reco.eicrecon.edm4eic.root
RECO ROOT file validation passed.
ERROR: Rucio registration failed for RECO file.
"""
        stderr = """
Trying upload with https to EIC-XRD-LOG
Successful upload of temporary file. https://dtn-rucio.jlab.org:1094/log.tar.gz
Trying upload with root to ASGC-XRD
Successful upload of temporary file. root://hpceph-xrootd.twgrid.org:1094/reco.root.rucio.upload
RSE checksum unavailable: Timer expired
Upload attempt failed
Trying upload with https to ASGC-XRD
Successful upload of temporary file. https://hpceph-xrootd.twgrid.org:1094/reco.root.rucio.upload
RSEChecksumUnavailable: HTTP 500
Upload attempt failed
"""

        diagnosis = diagnosis_from_log_texts(
            [stdout, stderr],
            job={'computingsite': 'BNL_ePIC_GOOGLE', 'jobstatus': 'failed'},
        )

        self.assertEqual(diagnosis['phase'], 'output_registration')
        self.assertTrue(diagnosis['payload_completed'])
        self.assertTrue(diagnosis['validation_passed'])
        self.assertEqual(diagnosis['operation'], 'remote_checksum')
        self.assertEqual(diagnosis['rse'], 'ASGC-XRD')
        self.assertEqual(
            diagnosis['endpoint'], 'hpceph-xrootd.twgrid.org')
        self.assertEqual(diagnosis['protocols_failed'], ['root', 'https'])
        self.assertEqual(diagnosis['cause_layer'], 'storage')
        self.assertEqual(diagnosis['cause_entity'], 'ASGC-XRD')
        self.assertEqual(diagnosis['cause_confidence'], 'confirmed')
        self.assertIn('ASGC-XRD at hpceph-xrootd.twgrid.org',
                      diagnosis['failure_summary'])
        self.assertNotIn('JLab', diagnosis['failure_summary'])
        self.assertNotIn('JLab', str(diagnosis['timeline']))

    def test_duplicate_did_checksum_conflict_is_data_management(self):
        logs = ["""
export OUT_RSE=ASGC-XRD
register_to_rucio.py
DataIdentifierAlreadyExists: File DID already exists
Local checksum deadbeef does not match remote checksum cafebabe
"""]

        diagnosis = diagnosis_from_log_texts(logs)

        self.assertEqual(diagnosis['phase'], 'output_registration')
        self.assertEqual(diagnosis['operation'], 'rucio_replica_conflict')
        self.assertEqual(diagnosis['cause_layer'], 'data_management')
        self.assertEqual(diagnosis['cause_entity'], 'ASGC-XRD')
        self.assertEqual(diagnosis['cause_confidence'], 'confirmed')
        self.assertEqual(
            diagnosis['conflict'][1],
            {'local_checksum': 'deadbeef', 'remote_checksum': 'cafebabe'},
        )

    def test_worker_failures_remain_separate_from_storage_failures(self):
        backoff = diagnosis_from_log_texts([], job={
            'computingsite': 'BNL_ePIC_GOOGLE',
            'jobstatus': 'failed',
            'superrordiag': 'BackoffLimitExceeded: retry limit reached',
        })
        shutdown = diagnosis_from_log_texts([], job={
            'computingsite': 'BNL_ePIC_GOOGLE',
            'jobstatus': 'failed',
            'piloterrordiag': 'Job killed due to imminent node shutdown',
        })

        self.assertEqual(backoff['phase'], 'worker_execution')
        self.assertEqual(backoff['operation'], 'worker_backoff_limit')
        self.assertEqual(backoff['cause_layer'], 'compute')
        self.assertEqual(backoff['cause_entity'], 'BNL_ePIC_GOOGLE')
        self.assertEqual(shutdown['phase'], 'worker_execution')
        self.assertEqual(shutdown['operation'], 'node_shutdown')
        self.assertEqual(shutdown['cause_layer'], 'compute')

    def test_top_level_checksum_without_transfer_evidence_is_unresolved(self):
        diagnosis = diagnosis_from_log_texts([], job={
            'computingsite': 'BNL_ePIC_GOOGLE',
            'jobstatus': 'failed',
            'piloterrordiag': 'Could not get the checksum',
        })

        self.assertEqual(diagnosis['phase'], 'failed')
        self.assertEqual(diagnosis['cause_layer'], 'unknown')
        self.assertEqual(diagnosis['cause_entity'], '')
        self.assertEqual(diagnosis['cause_confidence'], 'unresolved')
