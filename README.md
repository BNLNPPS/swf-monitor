# swf-monitor

Monitoring and information service for the ePIC streaming workflow testbed. For information
on the testbed and its software see the umbrella repository for the testbed [swf-testbed](https://github.com/bnlnpps/swf-testbed).

This is a web service providing system monitoring and comprehensive
information about the testbed's state, both via browser-based dashboards and a
json based REST API.

This module will manage the databases used by the testbed, and offer a REST API
for other agents in the system to report status and retrieve information.

## Implementation notes

- Django-based web service providing a browser UI and a REST json API,
  leveraging the PanDA monitor.
- Postgres as the back end database, as for PanDA and Rucio.
- Receives information from agents in the system via REST or ActiveMQ
  messages.
- Interfaces with OpenSearch/Grafana for monitoring dashboards.

## Agent Status Update API

Agents can update their status and heartbeat using the following REST endpoint:

**POST** `/api/monitoreditems/update_status/`

**Request body (JSON):**
```
{
  "name": "<agent_name>",
  "status": "<status>",
  "last_heartbeat": "<ISO8601 datetime, optional>"
}
```
- `name` (string, required): The unique name of the agent (must match an existing MonitoredItem)
- `status` (string, required): One of `UNKNOWN`, `OK`, `WARNING`, `ERROR`
- `last_heartbeat` (string, optional): ISO8601 datetime string (e.g., `2025-06-18T12:00:00Z`)

**Response:**
- 200 OK: Returns the updated agent record
- 400 Bad Request: Missing required fields
- 404 Not Found: Agent not found

## REST API Endpoints

The service provides a standard REST API for managing monitored items.

### List and Create Monitored Items

- **GET** `/api/monitoreditems/`
  - **Description:** Retrieves a list of all monitored items.
  - **Response:** `200 OK` with a list of monitored item objects.

- **POST** `/api/monitoreditems/`
  - **Description:** Creates a new monitored item.
  - **Request Body:** A JSON object representing the item (e.g., `{"name": "new-agent", "description": "...", "status": "OK"}`).
  - **Response:** `201 Created` with the newly created item object.

### Retrieve, Update, and Delete a Monitored Item

- **GET** `/api/monitoreditems/{id}/`
  - **Description:** Retrieves a single monitored item by its ID.
  - **Response:** `200 OK` with the item object.

- **PUT** `/api/monitoreditems/{id}/`
  - **Description:** Updates all fields of a specific monitored item.
  - **Request Body:** A JSON object with all required fields for the item.
  - **Response:** `200 OK` with the updated item object.

- **PATCH** `/api/monitoreditems/{id}/`
  - **Description:** Partially updates a specific monitored item.
  - **Request Body:** A JSON object with the fields to be updated.
  - **Response:** `200 OK` with the updated item object.

- **DELETE** `/api/monitoreditems/{id}/`
  - **Description:** Deletes a specific monitored item.
  - **Response:** `204 No Content`.

### Custom Agent Status Update

The custom agent status update can be performed using the standard Monitored Item endpoints. Agents should report their status as part of the monitored item's data. The status can be one of `UNKNOWN`, `OK`, `WARNING`, or `ERROR`.

## Authentication

Write operations (POST, PUT, PATCH, DELETE) to the REST API require token authentication. Read operations (GET) are publicly accessible.

### Generating a Token

You can generate an API token for a user with the following management command:

```bash
python manage.py get_token <username>
```

If the user does not exist, you can create them at the same time:

```bash
python manage.py get_token <username> --create-user
```

### Using the Token

To authenticate your API requests, you must include the token in the `Authorization` header:

```
Authorization: Token <your_token_here>
```

For example, using `curl`:

```bash
curl -X POST -H "Authorization: Token <your_token_here>" -H "Content-Type: application/json" -d '{"name": "new-agent"}' http://localhost:8000/api/monitoreditems/
```

## Browser-Based Interface

The project includes a browser-based interface for monitoring and managing items. 

- **Read-Only Access**: By default, all users can view the list of monitored items and their status in a read-only mode.
- **Authenticated Access**: Users who log in can create, update, and delete monitored items directly through the web interface.

### Login and Logout

- To log in, navigate to the `/login/` URL and enter your credentials.
- A "Login" link is available in the navigation bar.
- Once logged in, a "Logout" link will appear in the navigation bar.

### User Roles and Administration

The application uses Django's built-in authentication and permissions system to manage user access. There are two main roles:

- **Standard Users**: Can log in to the web interface to manage monitored items.
- **Admin Users**: Have all the permissions of a standard user, plus access to the Django admin site to manage users and their permissions.

#### Creating an Admin User

To create a user with admin privileges, run the following command:

```bash
python manage.py createsuperuser
```

You will be prompted to set a username, email, and password. This user will have access to the admin site.

#### Managing Users

Admin users can manage users and their permissions by:
1.  Logging into the web interface.
2.  Clicking the "Admin" link in the navigation bar.
3.  Using the Django admin interface to add, edit, or delete users.

## REST API Authentication

