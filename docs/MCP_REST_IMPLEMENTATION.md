# MCP REST API Implementation

## Overview

This document describes the REST API implementation for the Model Control Protocol (MCP) service, providing HTTP endpoints that mirror the existing WebSocket capabilities.

## Architecture

### Shared Service Layer
- **MCPService**: Core business logic shared between WebSocket and REST implementations
- Located in `mcp_app/views.py`
- Handles capabilities discovery, agent liveness checking, and heartbeat processing

### REST Endpoints

All MCP REST endpoints are available under `/api/mcp/` and follow the MCP message protocol.

#### 1. Discover Capabilities
- **URL**: `POST /api/mcp/discover-capabilities/`
- **Purpose**: Lists all available MCP commands and their descriptions
- **Request Format**:
  ```json
  {
    "mcp_version": "1.0",
    "message_id": "unique-id",
    "command": "discover_capabilities",
    "payload": {}
  }
  ```
- **Response Format**:
  ```json
  {
    "mcp_version": "1.0",
    "message_id": "response-id",
    "in_reply_to": "request-message-id",
    "status": "success",
    "payload": {
      "discover_capabilities": "Lists all available commands...",
      "get_agent_liveness": "Returns a report of all agents...",
      "heartbeat": "A notification sent by an agent..."
    }
  }
  ```

#### 2. Agent Liveness
- **URL**: `POST /api/mcp/agent-liveness/`
- **Purpose**: Returns liveness status for all registered agents
- **Request Format**:
  ```json
  {
    "mcp_version": "1.0",
    "message_id": "unique-id",
    "command": "get_agent_liveness",
    "payload": {}
  }
  ```
- **Response Format**:
  ```json
  {
    "mcp_version": "1.0",
    "message_id": "response-id",
    "in_reply_to": "request-message-id", 
    "status": "success",
    "payload": {
      "agent-1": "alive",
      "agent-2": "dead",
      "agent-3": "alive"
    }
  }
  ```

#### 3. Heartbeat
- **URL**: `POST /api/mcp/heartbeat/`
- **Purpose**: Process agent heartbeat notifications to update status
- **Request Format**:
  ```json
  {
    "mcp_version": "1.0",
    "message_id": "unique-id",
    "command": "heartbeat",
    "payload": {
      "name": "agent-name",
      "timestamp": "2025-07-31T19:30:00.000Z",
      "status": "OK"
    }
  }
  ```
- **Response Format**:
  ```json
  {
    "status": "success"
  }
  ```

## Implementation Details

### File Structure
```
mcp_app/
├── serializers.py       # Request/response serializers
├── views.py             # REST endpoints and MCPService
├── urls.py              # URL routing
├── consumers.py         # WebSocket consumer (refactored to use MCPService)
└── tests.py             # Unit tests
```

### Key Components

#### Serializers (`serializers.py`)
- **MCPRequestSerializer**: Base serializer for MCP requests
- **HeartbeatRequestSerializer**: Validates heartbeat payloads
- **DiscoverCapabilitiesRequestSerializer**: Validates capability requests
- **GetAgentLivenessRequestSerializer**: Validates liveness requests
- **MCPResponseSerializer**: Formats MCP responses
- **MCPErrorResponseSerializer**: Formats error responses

#### Views (`views.py`)
- **MCPService**: Core service class with shared business logic
- **heartbeat()**: Process agent heartbeat notifications
- **discover_capabilities()**: Return available capabilities
- **get_agent_liveness()**: Return agent liveness status

#### URL Configuration
- **mcp_app/urls.py**: MCP-specific URL patterns
- **swf_monitor_project/urls.py**: Integration with main URL configuration

### Error Handling

The REST API implements comprehensive error handling with standardized MCP error responses:

- **4000**: Invalid request format or missing required fields
- **4001**: Missing 'mcp_version' field
- **4002**: Unsupported MCP version
- **4004**: Unknown command
- **5000**: Internal server errors

### Authentication

Currently configured with `AllowAny` permissions for R&D/demo purposes. Can be updated to use Django REST Framework authentication for production.

## Testing

### Manual Testing
Use the provided test script:
```bash
python test_mcp_rest.py
```

### Integration with Swagger/OpenAPI
The REST endpoints are documented using `drf-spectacular` and available at:
- **Swagger UI**: `/api/schema/swagger-ui/`
- **ReDoc**: `/api/schema/redoc/`
- **Schema**: `/api/schema/`

## Compatibility

The REST implementation maintains full compatibility with the existing WebSocket MCP implementation:
- Same message protocol
- Same business logic (shared via MCPService)
- Same error codes and responses
- Same agent data models

## Next Steps

1. **Production Authentication**: Implement proper authentication/authorization
2. **Rate Limiting**: Add rate limiting for production use
3. **Caching**: Consider caching for frequently accessed endpoints
4. **Monitoring**: Add metrics and logging for REST endpoint usage
5. **Client Libraries**: Develop client libraries for different programming languages

## Usage Examples

### Python (using requests)
```python
import requests
import uuid

# Discovery capabilities
response = requests.post('http://localhost:8002/api/mcp/discover-capabilities/', json={
    "mcp_version": "1.0",
    "message_id": str(uuid.uuid4()),
    "command": "discover_capabilities",
    "payload": {}
})
capabilities = response.json()['payload']
```

### curl
```bash
# Send heartbeat
curl -X POST http://localhost:8002/api/mcp/heartbeat/ \
  -H "Content-Type: application/json" \
  -d '{
    "mcp_version": "1.0",
    "message_id": "test-123",
    "command": "heartbeat",
    "payload": {
      "name": "my-agent",
      "timestamp": "2025-07-31T19:30:00.000Z",
      "status": "OK"
    }
  }'
```

This REST API implementation provides a robust, standards-compliant HTTP interface for the MCP service while maintaining compatibility with existing WebSocket clients.