-- Test data for SWF Monitor application
-- This file contains sample data for all tables in the SWF monitoring system

-- SystemAgent test data
INSERT INTO swf_systemagent (instance_name, agent_type, description, status, last_heartbeat, agent_url, created_at, updated_at) VALUES
('Data-Reader-1', 'DataReader', 'Reads data from the detector', 'OK', NOW(), 'http://localhost:8001', NOW(), NOW()),
('Data-Reader-2', 'DataReader', 'Reads data from the detector', 'WARNING', NOW(), 'http://localhost:8002', NOW(), NOW()),
('Event-Builder', 'EventBuilder', 'Builds events from data chunks', 'OK', NOW(), 'http://localhost:8003', NOW(), NOW()),
('Data-Processor', 'DataProcessor', 'Processes event data', 'ERROR', NOW(), 'http://localhost:8004', NOW(), NOW()),
('Fast-Monitor-E1', 'FastMonitor', 'E1 fast monitoring agent', 'OK', NOW(), 'http://localhost:8005', NOW(), NOW()),
('Fast-Monitor-E2', 'FastMonitor', 'E2 fast monitoring agent', 'OK', NOW(), 'http://localhost:8006', NOW(), NOW()),
('STF-Agent-1', 'STFAgent', 'Super Time Frame processing agent', 'OK', NOW(), 'http://localhost:8007', NOW(), NOW()),
('STF-Agent-2', 'STFAgent', 'Super Time Frame processing agent', 'WARNING', NOW(), 'http://localhost:8008', NOW(), NOW());

-- Run test data
INSERT INTO runs (run_number, start_time, end_time, run_conditions) VALUES
(100001, '2024-01-15 10:00:00', '2024-01-15 14:30:00', '{"beam_energy": "5 GeV", "magnetic_field": "1.5T", "detector_config": "physics"}'),
(100002, '2024-01-15 15:00:00', '2024-01-15 18:45:00', '{"beam_energy": "5 GeV", "magnetic_field": "1.5T", "detector_config": "physics"}'),
(100003, '2024-01-16 09:30:00', '2024-01-16 13:15:00', '{"beam_energy": "10 GeV", "magnetic_field": "1.5T", "detector_config": "physics"}'),
(100004, '2024-01-16 14:00:00', NULL, '{"beam_energy": "10 GeV", "magnetic_field": "1.5T", "detector_config": "physics"}'),
(100005, '2024-01-17 08:00:00', '2024-01-17 12:30:00', '{"beam_energy": "5 GeV", "magnetic_field": "0T", "detector_config": "cosmics"}');

-- STF Files test data
INSERT INTO stf_files (file_id, run_id, machine_state, file_url, file_size_bytes, checksum, status, metadata, created_at) VALUES
('11111111-1111-1111-1111-111111111111', 1, 'physics', 'https://data.epic.bnl.gov/stf/run100001/stf_001.dat', 1073741824, 'sha256:abc123def456', 'processed', '{"quality_score": 0.95, "event_count": 50000}', '2024-01-15 10:05:00'),
('22222222-2222-2222-2222-222222222222', 1, 'physics', 'https://data.epic.bnl.gov/stf/run100001/stf_002.dat', 1073741824, 'sha256:def456ghi789', 'processed', '{"quality_score": 0.92, "event_count": 48000}', '2024-01-15 10:10:00'),
('33333333-3333-3333-3333-333333333333', 1, 'physics', 'https://data.epic.bnl.gov/stf/run100001/stf_003.dat', 1073741824, 'sha256:ghi789jkl012', 'done', '{"quality_score": 0.98, "event_count": 52000}', '2024-01-15 10:15:00'),
('44444444-4444-4444-4444-444444444444', 2, 'physics', 'https://data.epic.bnl.gov/stf/run100002/stf_001.dat', 1073741824, 'sha256:jkl012mno345', 'processing', '{"quality_score": 0.89, "event_count": 45000}', '2024-01-15 15:05:00'),
('55555555-5555-5555-5555-555555555555', 2, 'physics', 'https://data.epic.bnl.gov/stf/run100002/stf_002.dat', 1073741824, 'sha256:mno345pqr678', 'registered', '{"quality_score": 0.93, "event_count": 49000}', '2024-01-15 15:10:00'),
('66666666-6666-6666-6666-666666666666', 3, 'physics', 'https://data.epic.bnl.gov/stf/run100003/stf_001.dat', 2147483648, 'sha256:pqr678stu901', 'failed', '{"quality_score": 0.65, "event_count": 30000, "error": "checksum_mismatch"}', '2024-01-16 09:35:00'),
('77777777-7777-7777-7777-777777777777', 4, 'physics', 'https://data.epic.bnl.gov/stf/run100004/stf_001.dat', 1073741824, 'sha256:stu901vwx234', 'processing', '{"quality_score": 0.91, "event_count": 47000}', '2024-01-16 14:05:00'),
('88888888-8888-8888-8888-888888888888', 5, 'cosmics', 'https://data.epic.bnl.gov/stf/run100005/stf_001.dat', 536870912, 'sha256:vwx234yza567', 'processed', '{"quality_score": 0.88, "event_count": 25000}', '2024-01-17 08:05:00');

-- Subscribers test data
INSERT INTO subscribers (subscriber_name, fraction, description, is_active, created_at, updated_at) VALUES
('analysis-farm-1', 1.0, 'Main analysis farm for physics data processing', true, '2024-01-15 08:00:00', '2024-01-15 08:00:00'),
('fast-monitor-e1', 0.1, 'E1 detector fast monitoring system', true, '2024-01-15 08:00:00', '2024-01-15 08:00:00'),
('fast-monitor-e2', 0.1, 'E2 detector fast monitoring system', true, '2024-01-15 08:00:00', '2024-01-15 08:00:00'),
('calibration-system', 0.05, 'Detector calibration and quality assurance', true, '2024-01-15 08:00:00', '2024-01-15 08:00:00'),
('backup-storage', 1.0, 'Backup storage system for all data', true, '2024-01-15 08:00:00', '2024-01-15 08:00:00'),
('test-subscriber', 0.01, 'Test subscriber for development', false, '2024-01-15 08:00:00', '2024-01-16 10:00:00');

-- Message Queue Dispatches test data
INSERT INTO message_queue_dispatches (dispatch_id, stf_file_id, dispatch_time, message_content, is_successful, error_message) VALUES
('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', '2024-01-15 10:10:00', '{"file_id": "11111111-1111-1111-1111-111111111111", "run_number": 100001, "file_url": "https://data.epic.bnl.gov/stf/run100001/stf_001.dat", "status": "processed", "machine_state": "physics"}', true, NULL),
('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '22222222-2222-2222-2222-222222222222', '2024-01-15 10:15:00', '{"file_id": "22222222-2222-2222-2222-222222222222", "run_number": 100001, "file_url": "https://data.epic.bnl.gov/stf/run100001/stf_002.dat", "status": "processed", "machine_state": "physics"}', true, NULL),
('cccccccc-cccc-cccc-cccc-cccccccccccc', '33333333-3333-3333-3333-333333333333', '2024-01-15 10:20:00', '{"file_id": "33333333-3333-3333-3333-333333333333", "run_number": 100001, "file_url": "https://data.epic.bnl.gov/stf/run100001/stf_003.dat", "status": "done", "machine_state": "physics"}', true, NULL),
('dddddddd-dddd-dddd-dddd-dddddddddddd', '44444444-4444-4444-4444-444444444444', '2024-01-15 15:10:00', '{"file_id": "44444444-4444-4444-4444-444444444444", "run_number": 100002, "file_url": "https://data.epic.bnl.gov/stf/run100002/stf_001.dat", "status": "processing", "machine_state": "physics"}', false, 'Connection timeout to message broker'),
('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee', '66666666-6666-6666-6666-666666666666', '2024-01-16 09:40:00', '{"file_id": "66666666-6666-6666-6666-666666666666", "run_number": 100003, "file_url": "https://data.epic.bnl.gov/stf/run100003/stf_001.dat", "status": "failed", "machine_state": "physics"}', false, 'Failed to validate message format'),
('ffffffff-ffff-ffff-ffff-ffffffffffff', '88888888-8888-8888-8888-888888888888', '2024-01-17 08:10:00', '{"file_id": "88888888-8888-8888-8888-888888888888", "run_number": 100005, "file_url": "https://data.epic.bnl.gov/stf/run100005/stf_001.dat", "status": "processed", "machine_state": "cosmics"}', true, NULL);

-- App Logs test data
INSERT INTO swf_applog (app_name, instance_name, timestamp, level, level_name, message, module, func_name, line_no, process, thread, extra_data) VALUES
('STF-Agent', 'STF-Agent-1', '2024-01-15 10:00:00', 20, 'INFO', 'STF Agent started successfully', 'stf_agent', 'main', 45, 12345, 140123456789, '{"version": "1.0.0", "config": "physics"}'),
('STF-Agent', 'STF-Agent-1', '2024-01-15 10:05:00', 20, 'INFO', 'Processing STF file: stf_001.dat', 'stf_processor', 'process_file', 123, 12345, 140123456789, '{"file_size": 1073741824, "run_number": 100001}'),
('STF-Agent', 'STF-Agent-1', '2024-01-15 10:10:00', 20, 'INFO', 'STF file processed successfully', 'stf_processor', 'process_file', 156, 12345, 140123456789, '{"processing_time": 300, "event_count": 50000}'),
('STF-Agent', 'STF-Agent-2', '2024-01-15 10:08:00', 30, 'WARNING', 'High memory usage detected', 'monitor', 'check_resources', 78, 12346, 140123456790, '{"memory_usage": 85, "threshold": 80}'),
('Fast-Monitor', 'Fast-Monitor-E1', '2024-01-15 10:15:00', 20, 'INFO', 'E1 detector monitoring active', 'e1_monitor', 'start_monitoring', 34, 12347, 140123456791, '{"detector_status": "online", "rate": "10kHz"}'),
('Fast-Monitor', 'Fast-Monitor-E2', '2024-01-15 10:15:00', 20, 'INFO', 'E2 detector monitoring active', 'e2_monitor', 'start_monitoring', 34, 12348, 140123456792, '{"detector_status": "online", "rate": "12kHz"}'),
('Data-Reader', 'Data-Reader-1', '2024-01-15 10:20:00', 40, 'ERROR', 'Failed to read data chunk', 'data_reader', 'read_chunk', 89, 12349, 140123456793, '{"chunk_id": 1234, "error_code": "TIMEOUT"}'),
('Data-Reader', 'Data-Reader-2', '2024-01-15 10:25:00', 20, 'INFO', 'Data reading completed', 'data_reader', 'read_data', 125, 12350, 140123456794, '{"chunks_read": 1000, "total_size": 10737418240}'),
('Event-Builder', 'Event-Builder', '2024-01-15 10:30:00', 20, 'INFO', 'Event building started for run 100001', 'event_builder', 'build_events', 67, 12351, 140123456795, '{"run_number": 100001, "expected_events": 50000}'),
('Event-Builder', 'Event-Builder', '2024-01-15 10:35:00', 50, 'CRITICAL', 'Event builder crashed', 'event_builder', 'process_event', 234, 12351, 140123456795, '{"error": "segmentation_fault", "event_id": 12345}'),
('Data-Processor', 'Data-Processor', '2024-01-15 11:00:00', 40, 'ERROR', 'Calibration data unavailable', 'calibration', 'load_calibration', 45, 12352, 140123456796, '{"calibration_set": "physics_2024", "last_update": "2024-01-14"}'),
('STF-Agent', 'STF-Agent-1', '2024-01-16 09:35:00', 40, 'ERROR', 'Checksum mismatch for STF file', 'stf_validator', 'validate_checksum', 89, 12345, 140123456789, '{"expected": "sha256:pqr678stu901", "actual": "sha256:corrupted", "file_id": "66666666-6666-6666-6666-666666666666"}'),
('Fast-Monitor', 'Fast-Monitor-E1', '2024-01-17 08:05:00', 30, 'WARNING', 'Detector rate below threshold', 'e1_monitor', 'check_rate', 56, 12347, 140123456791, '{"current_rate": "2kHz", "threshold": "5kHz", "machine_state": "cosmics"}');