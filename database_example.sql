INSERT INTO monitor_app_systemagent (instance_name, agent_type, description, status, last_heartbeat, agent_url, created_at, updated_at) VALUES
('Data-Reader-1', 'DataReader', 'Reads data from the detector', 'OK', NOW(), 'http://localhost:8001', NOW(), NOW()),
('Data-Reader-2', 'DataReader', 'Reads data from the detector', 'WARNING', NOW(), 'http://localhost:8002', NOW(), NOW()),
('Event-Builder', 'EventBuilder', 'Builds events from data chunks', 'OK', NOW(), 'http://localhost:8003', NOW(), NOW()),
('Data-Processor', 'DataProcessor', 'Processes event data', 'ERROR', NOW(), 'http://localhost:8004', NOW(), NOW());
