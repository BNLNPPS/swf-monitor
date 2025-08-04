# swf-monitor

**Monitoring and information service for the ePIC streaming workflow testbed.**

## System Overview

The application is built on Django infrastructure and comprises two main web apps, real-time messaging, and PostgreSQL backend.

### Core Components

1. **Monitor App (`monitor_app`)**: Primary user-facing component
   * **Browser UI**: Server-side rendered dashboard for viewing agent statuses with Django session authentication
   * **REST API**: Programmatic interface with token-based authentication and OpenAPI schema

2. **MCP App (`mcp_app`)**: Real-time communication layer
   * **WebSocket Service**: Django Channels implementation of Model Control Protocol (MCP)
   * **REST Endpoints**: HTTP alternative to WebSocket for MCP commands

3. **ActiveMQ Integration**: Management command (`listen_activemq`) for agent communications via message queue

4. **PostgreSQL Database**: Primary data store for agents, logs, runs, and application state

### Architecture

The monitor serves as the central hub in a distributed agent-based workflow:

```
┌─ swf-daqsim-agent (scheduler/generator)
│   ↓ ActiveMQ messages  
├─ [swf-data-agent] → [swf-processing-agent] → [swf-fastmon-agent]
│   ↓ status updates & logs
└─ swf-monitor (dashboard/database)
```

## Key Features

- 🖥️ **Real-time Dashboard** - Agent status monitoring with live updates
- 🔗 **REST API** - Complete CRUD operations with OpenAPI documentation  
- ⚡ **WebSocket Service** - Bidirectional real-time communication via MCP
- 📊 **Centralized Logging** - Agent log collection with `swf-common-lib` integration
- 🔐 **Authentication** - Token-based API access and web session management
- 📈 **Message Queue Integration** - ActiveMQ connectivity for workflow coordination
- 🧪 **Comprehensive Testing** - 65 tests across API, UI, and integration scenarios

## Documentation

📚 **Complete technical documentation in [`docs/`](docs/) directory:**

| Guide | Description | Use Case |
|-------|-------------|----------|
| **[Setup Guide](docs/SETUP_GUIDE.md)** | Installation, configuration, and development setup | Getting started |
| **[API Reference](docs/API_REFERENCE.md)** | REST API, WebSocket, database schema, authentication | Integration |
| **[MCP Implementation](docs/MCP_REST_IMPLEMENTATION.md)** | Model Control Protocol REST API details | Agent communication |
| **[Development Roadmap](docs/DEVELOPMENT_ROADMAP.md)** | Future plans, architecture, workflow design | Contributors |
| **[Test System](docs/TEST_SYSTEM.md)** | Testing approach, structure, and best practices | Quality assurance |

### Quick Links
- **Interactive API Docs**: [Swagger UI](http://127.0.0.1:8000/api/schema/swagger-ui/) | [ReDoc](http://127.0.0.1:8000/api/schema/redoc/)
- **Database Schema**: [testbed-schema.dbml](testbed-schema.dbml) (auto-generated)
- **Parent Project**: [swf-testbed documentation](../swf-testbed/README.md)

## Quick Examples

### Basic Setup
```bash
# See docs/SETUP_GUIDE.md for complete installation
python manage.py runserver          # Start web interface  
python manage.py listen_activemq    # Start message queue listener
```

### API Usage
```bash
# Generate token and create agent
python manage.py get_token username --create-user
curl -H "Authorization: Token <token>" -H "Content-Type: application/json" \
     -d '{"instance_name": "my-agent", "agent_type": "test", "status": "OK"}' \
     http://127.0.0.1:8000/api/systemagents/
```

### Agent Integration
```python
# Centralized logging with swf-common-lib
from swf_common_lib.rest_logging import setup_rest_logging
logger = setup_rest_logging(app_name='my_agent', instance_name='agent_001')
logger.info("Agent started")  # Automatically sent to monitor
```

## Testing

See [Test System documentation](docs/TEST_SYSTEM.md) for comprehensive testing guide.

## Development

### Requirements
- **Python 3.9+** 
- **PostgreSQL** for data persistence
- **ActiveMQ** for agent messaging (optional)

### Architecture Notes
- **Django** web framework with **Channels** for WebSocket support
- **Model Control Protocol (MCP)** for standardized agent communication
- **Token-based REST API** with comprehensive OpenAPI documentation
- **Shared service architecture** between WebSocket and REST implementations

See [Setup Guide](docs/SETUP_GUIDE.md) for detailed development environment configuration.

---

*For complete technical documentation and implementation details, see the [`docs/`](docs/) directory.*