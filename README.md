# swf-monitor

**Monitoring and information service for the ePIC streaming workflow testbed.**

## System Overview

The application is built on Django infrastructure and comprises two main web apps, real-time messaging, and PostgreSQL backend.

### Core Components

1. **Monitor App (`monitor_app`)**: Primary user-facing component
   * **Browser UI**: Server-side rendered dashboard for viewing agent statuses with Django session authentication
   * **REST API**: Programmatic interface with token-based authentication and OpenAPI schema

2. **MCP App (`mcp_app`)**: Real-time communication layer
   * **REST Endpoints**: HTTP alternative to WebSocket for MCP commands

3. **ActiveMQ Integration**: Built-in message queue connectivity
   * **Automatic Listening**: Connects to ActiveMQ automatically when Django starts
   * **SSE REST Forwarding**: Server-Sent Events streaming of ActiveMQ messages via HTTPS

4. **PostgreSQL Database**: Data store for all persistent system information including agents, logs, runs, STF files, FastMon files, workflows, and application state

## Key Features

- 🖥️ **Real-time Dashboard** - Agent status monitoring with live updates
- 🔗 **REST API** - Complete CRUD operations with OpenAPI documentation  
- 🔄 **MCP REST API** - Model Control Protocol endpoints for agent communication
- 📡 **SSE Message Streaming** - Real-time ActiveMQ message forwarding via HTTPS with Django Channels and Redis
- 📊 **Centralized Logging** - Agent log collection with `swf-common-lib` integration
- 🔐 **Authentication** - Token-based API access and web session management
- 📈 **ActiveMQ Integration** - Automatic message queue connectivity and monitoring
- 🧪 **Comprehensive Testing** - 88+ tests across API, UI, and integration scenarios

## Documentation

📚 **Complete technical documentation in [`docs/`](docs/) directory:**

| Guide | Description | Use Case |
|-------|-------------|----------|
| **[Setup Guide](docs/SETUP_GUIDE.md)** | Installation, configuration, and development setup | Getting started |
| **[Production Deployment](docs/PRODUCTION_DEPLOYMENT.md)** | Complete Apache production deployment guide | Production operations |
| **[API Reference](docs/API_REFERENCE.md)** | REST API, WebSocket, database schema, authentication | Integration |
| **[MCP Implementation](docs/MCP_REST_IMPLEMENTATION.md)** | Model Control Protocol REST API details | Agent communication |
| **[Test System](docs/TEST_SYSTEM.md)** | Testing approach, structure, and best practices | Quality assurance |

### Quick Links
- **Production Monitor**: [https://pandaserver02.sdcc.bnl.gov/swf-monitor/](https://pandaserver02.sdcc.bnl.gov/swf-monitor/)
- **Interactive API Docs**: [Swagger UI](https://pandasserver02.sdcc.bnl.gov/swf-monitor/api/schema/swagger-ui/) | [ReDoc](https://pandasserver02.sdcc.bnl.gov/swf-monitor/api/schema/redoc/)
- **Database Schema**: [testbed-schema.dbml](testbed-schema.dbml) (auto-generated)
- **Parent Project**: [swf-testbed documentation](../swf-testbed/README.md)

## Quick Examples

### Basic Setup
```bash
# See docs/SETUP_GUIDE.md for complete installation
python manage.py runserver          # Start web interface (includes ActiveMQ integration)
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