# Setup and Installation Guide

This guide walks you through setting up the `swf-monitor` for local development.

## Quick Start

For experienced developers who want to get running immediately:

```bash
# Clone and setup
git clone https://github.com/BNLNPPS/swf-monitor.git
cd swf-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure environment  
cp .env.example .env
# Edit .env with your SECRET_KEY, DB_PASSWORD, and SWF_TESTUSER_PASSWORD

# Setup database and users
python manage.py migrate
python manage.py createsuperuser
python manage.py setup_testuser
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` to access the monitoring dashboard.

*For detailed step-by-step instructions, continue reading below.*

## Installation Steps

### 1. Clone and Setup Environment

```bash
git clone https://github.com/BNLNPPS/swf-monitor.git
cd swf-monitor

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy the example environment file:
```bash
cp .env.example .env
```

Edit the `.env` file and set your `SECRET_KEY`, `DB_PASSWORD`, and `SWF_TESTUSER_PASSWORD`.

*Generate a new `SECRET_KEY` using:*
```bash
python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

### 3. Setup PostgreSQL Database

Log in to PostgreSQL and create the database:

```sql
CREATE DATABASE swfdb;
CREATE USER admin WITH PASSWORD 'your_db_password';
ALTER ROLE admin SET client_encoding TO 'utf8';
ALTER ROLE admin SET default_transaction_isolation TO 'read committed';
ALTER ROLE admin SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE swfdb TO admin;
```

### 4. Run Database Migrations

```bash
python manage.py migrate
```

### 5. Create Admin User

Create a superuser account for admin access:

```bash
python manage.py createsuperuser
```

Follow the prompts to set username, email, and password.

### 6. Create Test User (For Development/Testing)

Create a test user account for automated testing and development:

```bash
python manage.py setup_testuser
```

This creates a user named `testuser` that can be used for:
- Automated testing scripts
- WebSocket authentication testing
- Development workflow testing

*Note: The testuser is a regular user (not staff) for testing normal user functionality.*

## Running the Application

### Start Development Server

```bash
python manage.py runserver
```

The web interface will be available at `http://127.0.0.1:8000/`.


### HTTPS Development Testing

When running the local dual server via `start_django_dual.sh`, the HTTPS endpoint is served by Daphne. On some hosts, `localhost` resolves to IPv6 (`::1`) while Daphne listens on IPv4 (`0.0.0.0`). This can cause HTTPS handshakes to stall. Use `https://127.0.0.1:8443` for local testing.

## Production Deployment

For complete production deployment instructions including Apache setup, SSL configuration, and deployment automation, see the comprehensive **[Production Deployment Guide](PRODUCTION_DEPLOYMENT.md)**.

**Quick Reference:**
- Initial setup: Run `sudo ./setup-apache-deployment.sh` 
- Deploy updates: `sudo /opt/swf-monitor/bin/deploy-swf-monitor.sh branch main`
- Configuration: Edit `/opt/swf-monitor/config/env/production.env`

## Testing

Run the full test suite:

```bash
./run_tests.py
```

This script automatically:
- Activates the virtual environment (uses swf-testbed's .venv if available)
- Runs pytest with proper Django configuration
- Provides detailed test output and coverage

Alternatively, you can use Django's built-in test runner:

```bash
python manage.py test
```

*Note: `./run_tests.py` is the recommended approach as it handles environment setup automatically and provides better test output.*

All tests should pass.

## Troubleshooting

### Common Issues

- **Database connection errors**: Check PostgreSQL is running and credentials in `.env`
- **Migration errors**: Ensure database user has proper permissions
- **Import errors**: Verify virtual environment is activated

### Getting Help

- Check the [main documentation](README.md) for architectural overview
- Review [API documentation](MCP_REST_IMPLEMENTATION.md) for integration details
- See the [testbed documentation](../../swf-testbed/README.md) for system context