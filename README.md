# swf-monitor

Monitoring and information service for the ePIC streaming workflow testbed. For information
on the testbed and its software see the umbrella repository for the testbed [swf-testbed](https://github.com/bnlnpps/swf-testbed).

This is a web service providing system monitoring and comprehensive
information about the testbed's state, both via browser-based dashboards and a
json based REST API.

This module will manage the databases used by the testbed, and offer a REST API
for other agents in the system to report status and retrieve information.

## Implementation notes

- Django-based web service providing a browser UI and a REST json API,
  leveraging the PanDA monitor.
- Postgres as the back end database, as for PanDA and Rucio.
- Receives information from agents in the system via REST or ActiveMQ
  messages.
- Interfaces with OpenSearch/Grafana for monitoring dashboards.

## Agent Status Update API

Agents can update their status and heartbeat using the following REST endpoint:

**POST** `/api/monitoreditems/update_status/`

**Request body (JSON):**
```
{
  "name": "<agent_name>",
  "status": "<status>",
  "last_heartbeat": "<ISO8601 datetime, optional>"
}
```
- `name` (string, required): The unique name of the agent (must match an existing MonitoredItem)
- `status` (string, required): One of `UNKNOWN`, `OK`, `WARNING`, `ERROR`
- `last_heartbeat` (string, optional): ISO8601 datetime string (e.g., `2025-06-18T12:00:00Z`)

**Response:**
- 200 OK: Returns the updated agent record
- 400 Bad Request: Missing required fields
- 404 Not Found: Agent not found

## Running the API Tests

To run the API tests for agent status update:

```
python manage.py test monitor_app
```

This will run the tests in `monitor_app/tests.py` which cover success, missing fields, and agent not found cases for the update_status endpoint.

