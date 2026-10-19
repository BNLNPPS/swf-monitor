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

    def test_batch_job_ended_under_the_job_reads_the_slurm_state(self):
        # Job 2610213 in batch job 57584849 (4 h, TIMEOUT): started 2 minutes in.
        early = diagnosis_from_log_texts([], job={
            'computingsite': 'NERSC_Perlmutter_epic', 'jobstatus': 'failed',
            'starttime': '2026-08-25T15:10:41', 'endtime': '2026-08-25T19:08:26',
            'taskbuffererrordiag': (
                'The worker was finished while the job was running : 57584849     '
                'epic-scor+ regular_m+      m3763        256    TIMEOUT      0:0 ')},
            worker={'starttime': '2026-08-25T15:08:54', 'endtime': '2026-08-25T19:08:09'})
        # Job 2578721 in batch job 57465559 (4 h, TIMEOUT): started 2h43m in.
        late = diagnosis_from_log_texts([], job={
            'computingsite': 'NERSC_Perlmutter_epic', 'jobstatus': 'failed',
            'starttime': '2026-08-23T13:58:49', 'endtime': '2026-08-23T15:18:27',
            'taskbuffererrordiag': (
                'The worker was finished while the job was running : 57465559     '
                'epic-scor+ regular_m+      m3763        256    TIMEOUT      0:0 ')},
            worker={'starttime': '2026-08-23T11:15:54', 'endtime': '2026-08-23T15:18:15'})
        completed = diagnosis_from_log_texts([], job={
            'computingsite': 'NERSC_Perlmutter_epic', 'jobstatus': 'failed',
            'starttime': '2026-08-20T08:30:00', 'endtime': '2026-08-20T10:00:00',
            'taskbuffererrordiag': (
                'The worker was finished while the job was running : 57271748     '
                'epic-scor+ regular_m+      m3763        256  COMPLETED      0:0 ')},
            worker={'starttime': '2026-08-20T08:00:00', 'endtime': '2026-08-20T10:00:00'})
        node_fail = diagnosis_from_log_texts([], job={
            'computingsite': 'NERSC_Perlmutter_epic', 'jobstatus': 'failed',
            'starttime': '2026-08-22T08:00:00', 'endtime': '2026-08-22T11:26:00',
            'taskbuffererrordiag': (
                'The worker was failed while the job was running : 57447231     '
                'epic-scor+ regular_m+      m3763        256  NODE_FAIL      1:0 ')})
        starting = diagnosis_from_log_texts([], job={
            'computingsite': 'NERSC_Perlmutter_epic', 'jobstatus': 'failed',
            'taskbuffererrordiag': (
                'The worker was finished while the job was starting : 57286719     '
                'epic-scor+ regular_m+      m3763        256  COMPLETED      0:0 ')})

        self.assertEqual(early['phase'], 'worker_execution')
        self.assertEqual(early['operation'], 'worker_walltime')
        self.assertEqual(early['cause_layer'], 'compute')
        self.assertEqual(early['cause_entity'], 'NERSC_Perlmutter_epic')
        self.assertEqual(early['cause_confidence'], 'confirmed')
        self.assertEqual(
            early['failure_summary'],
            'Killed at NERSC_Perlmutter_epic when its 4-hour batch job hit the limit. '
            'This job started 1 minute into the batch job and had run 3h57m. '
            'It cannot finish in a 4-hour batch job.')
        self.assertEqual(
            late['failure_summary'],
            'Killed at NERSC_Perlmutter_epic when its 4-hour batch job hit the limit. '
            'This job was started with only 1h19m of the batch job left. '
            'Its 1h19m of processing is lost.')
        self.assertEqual(completed['operation'], 'worker_ended')
        self.assertEqual(completed['cause_confidence'], 'supported')
        self.assertEqual(
            completed['failure_summary'],
            'Killed at NERSC_Perlmutter_epic: its batch job exited normally after 2h00m '
            'while this job was still running, and took the job down with it. '
            'Its 1h30m of processing is lost. Not a time limit, not a node failure; '
            'why the batch job exited early is not known.')
        self.assertEqual(node_fail['operation'], 'node_failure')
        self.assertEqual(node_fail['cause_confidence'], 'confirmed')
        self.assertEqual(
            node_fail['failure_summary'],
            'Killed at NERSC_Perlmutter_epic: the node it was running on failed '
            '(Slurm NODE_FAIL). Its 3h26m of processing is lost.')
        self.assertEqual(
            starting['failure_summary'],
            'Never ran: its batch job at NERSC_Perlmutter_epic ended before this job started.')

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
