# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Testing
- `./run_tests.sh` - Run Django tests with proper environment setup
- Tests use pytest framework and Django test infrastructure
- Test runner manages virtual environment automatically

### Django Management  
**CRITICAL: Use swf-testbed virtual environment (run install.sh from swf-testbed first)**
- `python src/manage.py runserver` - Start development server (after PARENT_DIR set)
- `python src/manage.py migrate` - Run database migrations
- `python src/manage.py createsuperuser` - Create admin user
- `python src/manage.py collectstatic` - Collect static files

### Installation and Dependencies
**CRITICAL: Run install.sh from swf-testbed directory to set up environment**
- Dependencies in `requirements.txt` (installed by testbed install.sh)
- Virtual environment managed by swf-testbed at `$PARENT_DIR/swf-testbed/.venv/`
- PARENT_DIR environment variable set by install.sh for proper coordination

## Architecture Overview

### Django Web Application
This is the monitoring and data management component of the SWF testbed system. It provides:
- Web interface for system monitoring and data visualization
- REST API for data access and reporting
- Real-time messaging integration with ActiveMQ
- Database models for storing system metadata and logs

### Key Components
- **Django App**: Web interface and API endpoints
- **ActiveMQ Integration**: Real-time message processing and monitoring
- **Database Models**: PostgreSQL-backed data storage for system metrics
- **Static Assets**: Web interface styling and client-side functionality
- **ASGI Support**: WebSocket and async capabilities via Daphne

### Multi-Repository Integration
- **swf-testbed**: Uses this as monitoring backend
- **swf-common-lib**: Shared utilities for logging and common functionality
- Part of coordinated multi-repository development workflow

## Configuration and Environment

### Database Configuration
- PostgreSQL backend with configurable connection parameters
- Environment variables in `.env` file (copy from `.env.example`)
- Database settings: `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`

### ActiveMQ Integration
- Message broker connection via environment variables
- Settings: `ACTIVEMQ_HOST`, `ACTIVEMQ_PORT`, `ACTIVEMQ_USER`, `ACTIVEMQ_PASSWORD`
- Real-time listener service for processing system messages

### Django Settings
- `SECRET_KEY` - Django secret key (generate new for production)
- `DEBUG` - Debug mode flag
- `ALLOWED_HOSTS` - Allowed hostnames for production

## Development Practices

### Multi-Repository Coordination
- Always use infrastructure branches: `infra/baseline-v1`, `infra/baseline-v2`, etc.
- Coordinate changes with sibling repositories (swf-testbed, swf-common-lib)
- Never push directly to main - always use branches and pull requests
- Run tests across all repositories with `../swf-testbed/run_all_tests.sh`

### Django Best Practices
- Models in respective app directories
- URL configuration follows Django patterns
- Template organization for monitoring interfaces
- Static file management for production deployment

### Testing Strategy
- Django TestCase classes for model and view testing
- Mock external dependencies (ActiveMQ, external APIs)
- Database fixtures for consistent test data
- Integration tests for full workflow validation

## Key Files and Directories

### Core Django Files
- `src/manage.py` - Django management script
- `src/swf_monitor_project/settings.py` - Main Django settings
- `src/swf_monitor_project/urls.py` - URL routing configuration
- `src/swf_monitor_project/asgi.py` - ASGI configuration for async support

### Application Structure
- `monitor_app/` - Main monitoring Django application
- `mcp_app/` - Model Context Protocol services
- `templates/` - HTML templates for web interface
- `static/` - CSS, JavaScript, and other static assets

### Configuration
- `pyproject.toml` - Package configuration and dependencies
- `requirements.txt` - Additional pip dependencies
- `.env.example` - Environment variable template
- `database_example.sql` - Example database setup

### Scripts and Utilities
- `scripts/load_fake_logs.py` - Load sample data for development
- `run_tests.sh` - Test execution script

## External Dependencies

### Core Technologies
- **Django**: Web framework and ORM
- **PostgreSQL**: Primary database backend
- **ActiveMQ**: Message broker integration
- **Daphne**: ASGI server for WebSocket support

### Python Dependencies
- `django` - Web framework
- `psycopg2-binary` - PostgreSQL adapter
- `daphne` - ASGI server
- `swf-common-lib` - Shared utilities (logging, etc.)

## Service Integration

### Supervisor Configuration
When run as part of the full testbed, this application runs multiple processes:
- `swf-monitor-web` - Django development server (port 8000)
- `swf-monitor-daphne` - ASGI server for WebSockets (port 8001)
- `swf-monitor-activemq` - ActiveMQ message listener service

### API Endpoints
Provides REST API for system monitoring and data access used by other components in the testbed ecosystem.

### Security Considerations
- Environment-based secrets management
- Database credential isolation
- Production deployment considerations for `ALLOWED_HOSTS` and `DEBUG` settings