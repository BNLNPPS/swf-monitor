# swf-monitor

Monitoring and information service for the ePIC streaming workflow testbed. This service provides a browser-based dashboard, a REST API, and a WebSocket service for monitoring the state of the testbed.

## Getting Started

This guide will walk you through setting up the `swf-monitor` for local development.

### Prerequisites

- Python 3.9+
- PostgreSQL

### Installation and Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/swf-monitor.git
    cd swf-monitor
    ```

2.  **Create and activate a Python virtual environment:**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```

3.  **Install the required packages:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure your environment variables:**
    -   Copy the example environment file:
        ```bash
        cp .env.example .env
        ```
    -   Edit the `.env` file and set your `SECRET_KEY` and `DB_PASSWORD`.
        *You can generate a new `SECRET_KEY` using an online generator or by running `python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'`.*

5.  **Set up the PostgreSQL database:**
    -   Log in to PostgreSQL and create the database and user specified in your `.env` file.
        ```sql
        CREATE DATABASE swfdb;
        CREATE USER admin WITH PASSWORD 'your_db_password';
        ALTER ROLE admin SET client_encoding TO 'utf8';
        ALTER ROLE admin SET default_transaction_isolation TO 'read committed';
        ALTER ROLE admin SET timezone TO 'UTC';
        GRANT ALL PRIVILEGES ON DATABASE swfdb TO admin;
        ```

6.  **Run the database migrations:**
    ```bash
    python manage.py migrate
    ```

### Create an Admin User

To access the admin interface and manage the application, you need to create a superuser account:

```bash
python manage.py createsuperuser
```

Follow the prompts to set a username, email, and password.

### Running the Application

1. **Start the Django development server:**

    ```bash
    python manage.py runserver
    ```

    The web interface will be available at `http://127.0.0.1:8000/`.

2. **(Optional) Start the ActiveMQ listener:**

    If you are using ActiveMQ for agent heartbeats, open a new terminal and run:

    ```bash
    python manage.py listen_activemq
    ```

### Preparing for Production

When you are ready to deploy the application to a production environment, you should take the following steps to ensure it is secure:

1. **Disable Debug Mode**: In your `.env` file, set `DEBUG=False`. This is a critical security measure.

2. **Configure Allowed Hosts**: In your `.env` file, set `ALLOWED_HOSTS` to a comma-separated list of the domain names that will serve your application. For example: `ALLOWED_HOSTS=swf-monitor.example.com,www.swf-monitor.example.com`.

## Testing

The project includes a comprehensive test suite to ensure functionality and stability.

### Running the Tests

To run the full test suite, use `pytest`:

```bash
pytest
```

All tests should pass.

### Test Coverage

The test suite covers the following key areas of the application:

- **REST API**:
  - Full CRUD (Create, Read, Update, Delete) operations for monitored items.
  - Token-based authentication for write operations.
- **WebSocket Service**:
  - Authentication checks to ensure only logged-in users can connect.
  - Core commands like `get_all_statuses` and `get_agent_status`.
- **Browser-Based UI**:
  - Authentication flow (login/logout visibility).
  - Form-based CRUD operations for monitored items.
  - Access control to ensure only logged-in users can modify data.
- **Management Commands**:
  - The `get_token` command, including user creation.

## Usage

### Browser Interface

- **Monitor Dashboard**: Access `http://127.0.0.1:8000/` to see the main dashboard.
- **Login**: Click the "Login" link and use the credentials you created. Once logged in, you will be able to create, edit, and delete monitored items.
- **Admin Panel**: If you are logged in as an admin user, click the "Admin" link to access the Django admin site, where you can manage users and permissions.

### API Access and Authentication

For programmatic access, the service provides a REST API that uses token-based authentication for write operations.

1. **Generate a Token**:

    Use the `get_token` management command to generate a token for a user.

    ```bash
    # Get a token for an existing user
    python manage.py get_token <username>

    # Or create a new user and token at the same time
    python manage.py get_token <new_username> --create-user
    ```

2. **Use the Token**:

    Include the token in the `Authorization` header of your API requests.

    ```bash
    # Create a new agent
    curl -X POST -H "Authorization: Token <your_token_here>" \
         -H "Content-Type: application/json" \
         -d '{"name": "new-api-agent", "status": "OK"}' \
         http://127.0.0.1:8000/api/monitoreditems/

    # Update an agent's status using PATCH
    # First, get the ID of the agent you want to update
    curl -X PATCH -H "Authorization: Token <your_token_here>" \
         -H "Content-Type: application/json" \
         -d '{"status": "ERROR"}' \
         http://127.0.0.1:8000/api/monitoreditems/<agent_id>/
    ```

### API Documentation

The API is documented using OpenAPI (Swagger). You can view the interactive API documentation in your browser:

- **Swagger UI**: `http://127.0.0.1:8000/api/schema/swagger-ui/`
- **ReDoc**: `http://127.0.0.1:8000/api/schema/redoc/`

---

## Reference

### REST API Endpoints

(Detailed endpoint documentation follows...)

### MCP WebSocket Service

(Details about the WebSocket service...)

### Management Commands

- `createsuperuser`: Create an admin user.
- `get_token <username> [--create-user]`: Generate an API token.
- `listen_activemq`: Listen for heartbeats on an ActiveMQ topic.
- `populate_agents`: Populate the database with initial agent data.

