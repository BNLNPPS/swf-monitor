# API Reference Guide

Complete reference for the swf-monitor REST API and WebSocket services.

## API Documentation

The API is documented using OpenAPI (Swagger). View interactive documentation:

* **Swagger UI**: `http://127.0.0.1:8000/api/schema/swagger-ui/`
* **ReDoc**: `http://127.0.0.1:8000/api/schema/redoc/`

## Database Schema

Auto-generated schema diagram: **[testbed-schema.dbml](../testbed-schema.dbml)**

### Core Models

The system uses Django models to track agents, runs, data files, and messaging:

- **SystemAgent**: Agent instances with status and heartbeat tracking
- **AppLog**: Centralized logging from all agents and services  
- **Run**: Experimental runs containing multiple STF files
- **StfFile**: Super Time Frame files with processing status
- **MessageQueueDispatch**: Message queue operations and delivery tracking
- **Subscriber**: Message queue subscribers and their configurations

### Key Relationships

- **Run → StfFile**: One-to-many (each run contains multiple STF files)
- **StfFile → MessageQueueDispatch**: One-to-many (each file triggers multiple dispatches)
- **SystemAgent, Subscriber, AppLog**: Independent entities for monitoring

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

Include the token in the `Authorization` header:

```bash
# Create a new agent
curl -X POST -H "Authorization: Token <your_token>" \
     -H "Content-Type: application/json" \
     -d '{"instance_name": "new-agent", "agent_type": "test", "status": "OK"}' \
     http://127.0.0.1:8000/api/systemagents/

# Update agent status
curl -X PATCH -H "Authorization: Token <your_token>" \
     -H "Content-Type: application/json" \
     -d '{"status": "ERROR"}' \
     http://127.0.0.1:8000/api/systemagents/<agent_id>/
```

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

## Model Control Protocol (MCP)

### WebSocket Service

Connect to WebSocket at: `ws://127.0.0.1:8000/ws/mcp/`

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

# Setup
logger = setup_rest_logging(
    app_name='my_agent',
    instance_name='agent_001',
    base_url='http://monitor-server:8000'
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
- `listen_activemq` - Listen for ActiveMQ heartbeats
- `populate_agents` - Populate initial agent data

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

For detailed API schemas and examples, see the interactive documentation at `/api/schema/swagger-ui/`.