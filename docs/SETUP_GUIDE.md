# Setup and Installation Guide

This guide walks you through setting up the `swf-monitor` for local development.

## Quick Start

For experienced developers who want to get running immediately:

```bash
# Clone and setup
git clone https://github.com/your-username/swf-monitor.git
cd swf-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure environment  
cp .env.example .env
# Edit .env with your SECRET_KEY and DB_PASSWORD

# Setup database and run
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` to access the monitoring dashboard.

*For detailed step-by-step instructions, continue reading below.*

## Prerequisites

* Python 3.9+
* PostgreSQL

## Installation Steps

### 1. Clone and Setup Environment

```bash
git clone https://github.com/your-username/swf-monitor.git
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

Edit the `.env` file and set your `SECRET_KEY` and `DB_PASSWORD`.

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

## Running the Application

### Start Development Server

```bash
python manage.py runserver
```

The web interface will be available at `http://127.0.0.1:8000/`.

### Optional: Start ActiveMQ Listener

If using ActiveMQ for agent heartbeats:

```bash
python manage.py listen_activemq
```

## Production Deployment

When deploying to production:

1. **Disable Debug Mode**: Set `DEBUG=False` in `.env`
2. **Configure Allowed Hosts**: Set `SWF_ALLOWED_HOSTS` to your domain names
   ```
   SWF_ALLOWED_HOSTS=swf-monitor.example.com,www.swf-monitor.example.com
   ```

## Testing

Run the full test suite:

```bash
python manage.py test
```

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