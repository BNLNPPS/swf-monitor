# swf-monitor

**The common monitor, web, and database services of the swf platform.**

swf-monitor serves every workflow domain of the swf platform — the streaming workflow testbed and the epicprod production
system today — with browser pages, REST APIs, MCP tools, and the database-backed state beneath them. Production
applications ship from the [swf-epicprod](https://github.com/BNLNPPS/swf-epicprod) repository and run installed
within this application's runtime. The swf platform implements the ePIC Workflow Management System
(WFMS); the official system-level documentation is at
<https://epic-wfms-docs.readthedocs.io>; this repository's `docs/` carry the platform implementation detail
beneath it, and `swf-epicprod/docs/` carry the production-domain documentation.

## System Overview

The application is built on Django with a PostgreSQL backend and real-time messaging.

### Core Components

1. **Monitor App (`monitor_app`)**: The platform services
   * **Browser UI**: Server-side rendered pages with Django session authentication
   * **REST API**: Programmatic interface with token-based authentication and OpenAPI schema
   * **MCP Server**: Model Context Protocol tools for LLM interaction
   * **Platform machinery**: the action stream (structured action logging with a live view), SysConfig, the alarms engine, and cached system status

2. **Installed production applications**: the production domain (PCS and successors) ships from
   [swf-epicprod](https://github.com/BNLNPPS/swf-epicprod) as installable Django applications listed in this
   project's `INSTALLED_APPS`

3. **ActiveMQ Integration**: Built-in message queue connectivity
   * **Automatic Listening**: Connects to ActiveMQ automatically when Django starts
   * **SSE Forwarding**: Server-Sent Events streaming of ActiveMQ messages via HTTPS (Django Channels with a Redis layer)

4. **PostgreSQL Database**: Data store for all persistent system information including agents, logs, runs, STF files, FastMon files, workflows, configuration tags, and application state

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
| **[MCP Integration](docs/MCP.md)** | Model Context Protocol server overview and links to client, tool, and bot docs | Natural language queries |
| **[MCP Tool Reference](docs/MCP_TOOL_REFERENCE.md)** | Full MCP tool catalog, parameters, returns, and example prompts | Tool integration |
| **[MCP Client Setup](docs/MCP_CLIENTS.md)** | Claude Code and Claude Desktop MCP configuration | Local MCP clients |
| **[PanDA Mattermost Bot](docs/PANDA_BOT.md)** | PanDA bot MCP-client architecture and runtime configuration | Production monitoring chat |
| **[Action Stream](docs/ACTION_STREAM.md)** | Structured action logging: sublevel/live axes, live policy, live view | Operational record |
| **[AI Proposals](docs/AI_PROPOSALS.md)** | LLM proposes, human approves, deterministic execution | AI-assisted operations |
| **[External Access](docs/EXTERNAL_ACCESS.md)** | The swf-remote proxy contract, including write-action triggers | External face |
| **[System Status](docs/SYSTEM_STATUS.md)** | Cached production/system health, ops-agent refresh, and red nav indicator | Operations monitoring |
| **[Test System](docs/TEST_SYSTEM.md)** | Testing approach, structure, and best practices | Quality assurance |

The production-domain documentation — PCS, the task catalog, production
operations, campaigns, assessments — is in
[swf-epicprod/docs](https://github.com/BNLNPPS/swf-epicprod/tree/main/docs).

### Quick Links
- **Production Monitor**: [https://pandaserver02.sdcc.bnl.gov/swf-monitor/](https://pandaserver02.sdcc.bnl.gov/swf-monitor/)
- **Interactive API Docs**: [Swagger UI](https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/schema/swagger-ui/) | [ReDoc](https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/schema/redoc/)
- **Database Schema**: [testbed-schema.dbml](testbed-schema.dbml) (auto-generated, view at [dbdiagram.io](https://dbdiagram.io))
- **Peer applications**: [swf-testbed](../swf-testbed/README.md) (streaming workflow testbed) | [swf-epicprod](https://github.com/BNLNPPS/swf-epicprod) (production domain)

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
