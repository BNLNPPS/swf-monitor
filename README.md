# swf-monitor

**Monitoring and information service for the ePIC streaming workflow testbed.**

## System Overview

The application is built on Django infrastructure and comprises three main components, real-time messaging, and PostgreSQL backend.

### Core Components

1. **Monitor App (`monitor_app`)**: Primary user-facing component
   * **Browser UI**: Server-side rendered dashboard for viewing agent statuses with Django session authentication
   * **REST API**: Programmatic interface with token-based authentication and OpenAPI schema
   * **MCP Integration**: Model Context Protocol endpoint for LLM interaction

2. **EMI (`emi`)**: ePIC Metadata Interface for production metadata management
   * **Tag System**: Immutable, versioned parameter sets for physics, event generation, simulation, and reconstruction
   * **Dataset Composition**: Standardized naming from locked tags with automatic block management for Rucio
   * **REST API + Web UI**: Full CRUD with draft/locked lifecycle enforcement

3. **ActiveMQ Integration**: Built-in message queue connectivity
   * **Automatic Listening**: Connects to ActiveMQ automatically when Django starts
   * **SSE REST Forwarding**: Server-Sent Events streaming of ActiveMQ messages via HTTPS

4. **PostgreSQL Database**: Data store for all persistent system information including agents, logs, runs, STF files, FastMon files, workflows, metadata tags, and application state

## Key Features

- 🖥️ **Real-time Dashboard** - Agent status monitoring with live updates
- 🔗 **REST API** - Complete CRUD operations with OpenAPI documentation  
- 🤖 **MCP Integration** - Model Context Protocol for natural language interaction via LLM
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
| **[MCP Integration](docs/MCP.md)** | Model Context Protocol for LLM interaction | Natural language queries |
| **[EMI](docs/EMI.md)** | ePIC Metadata Interface — tags, datasets, production metadata | Production metadata management |
| **[Test System](docs/TEST_SYSTEM.md)** | Testing approach, structure, and best practices | Quality assurance |

### Quick Links
- **Production Monitor**: [https://pandaserver02.sdcc.bnl.gov/swf-monitor/](https://pandaserver02.sdcc.bnl.gov/swf-monitor/)
- **Interactive API Docs**: [Swagger UI](https://pandasserver02.sdcc.bnl.gov/swf-monitor/api/schema/swagger-ui/) | [ReDoc](https://pandasserver02.sdcc.bnl.gov/swf-monitor/api/schema/redoc/)
- **Database Schema**: [testbed-schema.dbml](testbed-schema.dbml) (auto-generated, view at [dbdiagram.io](https://dbdiagram.io))
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
- **Model Context Protocol (MCP)** for LLM-based system interaction
- **Token-based REST API** with comprehensive OpenAPI documentation

See [Setup Guide](docs/SETUP_GUIDE.md) for detailed development environment configuration.

---

*For complete technical documentation and implementation details, see the [`docs/`](docs/) directory.*