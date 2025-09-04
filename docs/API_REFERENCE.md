# API Reference Guide

Complete reference for the swf-monitor REST API and WebSocket services.

## API Documentation

The API is documented using OpenAPI (Swagger). View interactive documentation:

* **Swagger UI**: `https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/schema/swagger-ui/`
* **ReDoc**: `https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/schema/redoc/`

## Database Schema

Auto-generated schema diagram: **[testbed-schema.dbml](../testbed-schema.dbml)**

### Core Models

The system uses Django models to track agents, runs, data files, and messaging:

- **SystemAgent**: Agent instances with status and heartbeat tracking
- **AppLog**: Centralized logging from all agents and services  
- **Run**: Experimental runs containing multiple STF files
- **StfFile**: Super Time Frame files with processing status
- **FastMonFile**: Fast monitoring time frame sample files metadata
- **MessageQueueDispatch**: Message queue operations and delivery tracking
- **Subscriber**: Message queue subscribers and their configurations
- **PersistentState**: System state persistence for workflow tracking
- **PandaQueue**: PanDA queue configuration for job submission
- **RucioEndpoint**: Rucio data management endpoint definitions

## ActiveMQ Integration

### Automatic Connection Management

The monitor includes built-in ActiveMQ integration that starts automatically when Django launches. This integration:

- **Automatic Startup**: Connects to ActiveMQ when `python manage.py runserver` starts
- **Smart Initialization**: Only connects during normal operation, not during management commands like `migrate` or `test`
- **Configuration-Driven**: Requires `ACTIVEMQ_HOST` environment variable to be set
- **SSL Support**: Handles SSL certificate configuration for secure connections
- **Graceful Cleanup**: Automatically disconnects when Django shuts down

### Implementation Details

- **Connection Manager**: `ActiveMQConnectionManager` (singleton) in `monitor_app/activemq_connection.py`
- **App Integration**: Initialized via `MonitorAppConfig.ready()` in `monitor_app/apps.py`
- **Message Processing**: Handles agent heartbeats and workflow messages
- **Thread Safety**: Uses threading locks for safe singleton operation

### Configuration

Set these environment variables for ActiveMQ integration:

```bash
export ACTIVEMQ_HOST='your-activemq-host'
export ACTIVEMQ_PORT=61612
export ACTIVEMQ_USER='username'
export ACTIVEMQ_PASSWORD='password'
export ACTIVEMQ_USE_SSL=True
export ACTIVEMQ_SSL_CA_CERTS='/path/to/ca-cert.pem'
```

No separate management command is needed - the integration is fully automatic.

## Authentication

### Token-Based Authentication

For programmatic access, the API uses token-based authentication for write operations.

#### Generate a Token

```bash
# Get token for existing user
python manage.py get_token <username>

# Create new user and token
python manage.py get_token <new_username> --create-user
```

#### Use the Token

Include the token in the `Authorization` header.

### Production HTTPS Access

When connecting to the production monitor at `https://pandasserver02.sdcc.bnl.gov/swf-monitor/`, clients need the SSL certificate chain for verification.

#### SSL Certificate Setup

Set the certificate bundle path before making requests:

```bash
export REQUESTS_CA_BUNDLE=/opt/swf-monitor/current/full-chain.pem
```

**Note**: The certificate bundle is deployed automatically by the production deployment script and contains the InCommon RSA IGTF Server CA chain required for pandasserver02.sdcc.bnl.gov certificate validation.

## REST API Endpoints

### System Agents
- `GET /api/systemagents/` - List all agents
- `POST /api/systemagents/` - Create new agent
- `GET /api/systemagents/{id}/` - Get specific agent
- `PATCH /api/systemagents/{id}/` - Update agent
- `DELETE /api/systemagents/{id}/` - Delete agent

### Application Logs
- `GET /api/logs/` - List logs with filtering
- `POST /api/logs/` - Create log entry
- `GET /api/logs/summary/` - Get log summary by app/instance

### Runs
- `GET /api/runs/` - List experimental runs
- `POST /api/runs/` - Create new run
- `GET /api/runs/{id}/` - Get specific run
- `PATCH /api/runs/{id}/` - Update run

### STF Files
- `GET /api/stf-files/` - List STF files
- `POST /api/stf-files/` - Register new STF file
- `GET /api/stf-files/{id}/` - Get specific file
- `PATCH /api/stf-files/{id}/` - Update file status

### Message Queue Dispatches
- `GET /api/message-dispatches/` - List dispatches
- `POST /api/message-dispatches/` - Create dispatch record

### Subscribers
- `GET /api/subscribers/` - List subscribers
- `POST /api/subscribers/` - Create subscriber
- `PATCH /api/subscribers/{id}/` - Update subscriber

### Fast Monitoring Files
- `GET /api/fastmon-files/` - List fast monitoring files
- `POST /api/fastmon-files/` - Register new fast monitoring file
- `GET /api/fastmon-files/{id}/` - Get specific file
- `PATCH /api/fastmon-files/{id}/` - Update file metadata

### Workflows
- `GET /api/workflows/` - List STF workflows
- `POST /api/workflows/` - Create new workflow
- `GET /api/workflows/{id}/` - Get specific workflow
- `PATCH /api/workflows/{id}/` - Update workflow status

### Workflow Stages
- `GET /api/workflow-stages/` - List agent workflow stages
- `POST /api/workflow-stages/` - Create workflow stage
- `GET /api/workflow-stages/{id}/` - Get specific stage
- `PATCH /api/workflow-stages/{id}/` - Update stage status

### Workflow Messages
- `GET /api/workflow-messages/` - List workflow messages
- `POST /api/workflow-messages/` - Create workflow message
- `GET /api/workflow-messages/{id}/` - Get specific message

### System State
- `GET /api/state/next-run-number/` - Get next available run number

## Server-Sent Events (SSE) Streaming

### Overview
The monitor provides real-time message streaming via Server-Sent Events by forwarding ActiveMQ messages to receivers via HTTPS REST. This allows receivers to be geographically distributed anywhere with internet access, without requiring distributed ActiveMQ infrastructure - only HTTPS connectivity is needed.

### Endpoints

#### Stream Messages
- **URL**: `GET /api/messages/stream/`
- **Authentication**: Token required
- **Protocol**: HTTPS (port 443)
- **Content-Type**: `text/event-stream`

#### Query Parameters
- `msg_types`: Comma-separated message types to filter (e.g., `stf_gen,data_ready`)
- `agents`: Comma-separated agent names to filter (e.g., `daq-simulator,data-agent`)
- `run_ids`: Comma-separated run IDs to filter (e.g., `run-001,run-002`)

#### Example Usage
```bash
curl -H "Authorization: Token YOUR_TOKEN" \
     "https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/messages/stream/?msg_types=stf_gen,data_ready&agents=daq-simulator"
```

#### Stream Status
- **URL**: `GET /api/messages/stream/status/`
- **Authentication**: Token required
- **Returns**: Current broadcaster status and connected client count

```json
{
    "connected_clients": 2,
    "client_ids": ["uuid1", "uuid2"],
    "client_filters": {...}
}
```

### Message Format
SSE events use the following format:
```
event: message_type
data: {"msg_type": "stf_gen", "processed_by": "daq-simulator", "run_id": "run-001", ...}

event: heartbeat
data: {"timestamp": 1640995200.0}

event: connected
data: {"client_id": "uuid", "status": "connected"}
```

### Architecture
- **Message Routing**: ActiveMQ messages are relayed to SSE clients via Redis channel layer
- **Client Management**: Each client gets a dedicated message queue with configurable filtering
- **Scalability**: Redis-backed channel layer supports multiple Django processes
- **Reliability**: Automatic client cleanup and connection management

## Model Control Protocol (MCP)


### REST Endpoints

- `POST /api/mcp/heartbeat/` - Process agent heartbeat
- `POST /api/mcp/discover-capabilities/` - Get available commands
- `POST /api/mcp/agent-liveness/` - Get agent liveness status

### Message Format

```json
{
  "mcp_version": "1.0",
  "message_id": "unique-uuid",
  "command": "command_name",
  "payload": {
    "key": "value"
  }
}
```

### Available Commands

#### discover_capabilities
Returns available MCP commands and descriptions.
- **Request payload**: `{}`
- **Response**: Dictionary of command names and descriptions

#### get_agent_liveness
Reports agent liveness based on recent heartbeats.
- **Request payload**: `{}`
- **Response**: Dictionary mapping agent names to 'alive'/'dead' status

#### heartbeat (Notification)
Agent sends to signal it's active (no response expected).
- **Payload**: `{"name": "agent-name", "timestamp": "iso-8601-timestamp", "status": "OK"}`

## Agent Integration

### REST Logging

Agents can send logs using the `swf-common-lib` package:

```python
import logging
from swf_common_lib.rest_logging import setup_rest_logging

# Setup - the infrastructure handles URLs automatically
logger = setup_rest_logging(
    app_name='my_agent',
    instance_name='agent_001'
)

# Use standard Python logging
logger.info("Agent started")
logger.error("Processing failed")
```

### Logging Endpoint

- **URL**: `/api/logs/`
- **Method**: POST
- **Authentication**: None required
- **Content-Type**: `application/json`

Example log entry:
```json
{
    "app_name": "data_agent",
    "instance_name": "agent_001", 
    "timestamp": "2025-01-15T10:30:00.000Z",
    "level": 20,
    "levelname": "INFO",
    "message": "Processing file batch 1/10",
    "module": "data_processor",
    "funcname": "process_batch",
    "lineno": 45,
    "process": 1234,
    "thread": 5678
}
```

## Management Commands

- `createsuperuser` - Create admin user
- `get_token <username> [--create-user]` - Generate API token

## Error Handling

### HTTP Status Codes
- `200` - Success
- `201` - Created
- `400` - Bad Request
- `401` - Unauthorized
- `403` - Forbidden
- `404` - Not Found
- `500` - Internal Server Error

### MCP Error Codes
- `4000` - Invalid request format
- `4001` - Missing mcp_version field
- `4002` - Unsupported MCP version
- `4004` - Unknown command
- `5000` - Internal server error

For detailed API schemas and examples, see the interactive documentation at `https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/schema/swagger-ui/`.