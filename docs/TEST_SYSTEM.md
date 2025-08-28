# SWF Monitor Test System

## Overview

The SWF Monitor test system provides comprehensive coverage of Django web interface, REST APIs, database models, and system integrations. The test suite includes 88+ tests organized into focused modules for maintainability and AI-friendly navigation.

## Test Categories

### 🌐 **Web Interface Tests**
- **UI Views**: Django template rendering, form handling, navigation
- **Authentication**: Login/logout flows, permission enforcement
- **User Experience**: Dashboard functionality, data visualization

### 🔌 **API Tests**
- **REST Endpoints**: CRUD operations for all models
- **Authentication**: Token-based and session authentication
- **Data Validation**: Input sanitization, error handling
- **Integration**: End-to-end API workflows

### 🔒 **Security Tests**
- **Django HTTPS Authentication**: Token and session-based security
- **Permission Enforcement**: Role-based access control
- **SSL/TLS Configuration**: Certificate validation, secure connections

### 🔧 **Integration Tests**
- **Django Dual Server**: HTTP (8002) and HTTPS (8443) endpoints
- **ActiveMQ SSL**: Message broker connectivity and certificate handling
- **Database**: Model relationships, migrations, data integrity
- **REST Logging**: End-to-end logging workflow validation
- **SSE Streaming**: Server-Sent Events message broadcasting and filtering

## Directory Structure

```
src/monitor_app/tests/
├── __init__.py                              # Package marker
├── conftest.py                              # Shared fixtures and configuration
├── test_system_agent_api.py                # System agent CRUD operations (7 tests)
├── test_applog_api.py                       # Application log API testing (3 tests)
├── test_applog_ui.py                        # Application log UI views (3 tests)
├── test_monitor_app_ui.py                   # Monitor app UI functionality (8 tests)
├── test_log_summary_api.py                  # Log summary API endpoint (1 test)
├── test_run_api.py                          # Run management CRUD operations (7 tests)
├── test_stf_file_api.py                     # STF file management API (9 tests)
├── test_subscriber_api.py                   # Subscriber management API (9 tests)
├── test_message_queue_dispatch_api.py       # Message queue dispatch testing (8 tests)
├── test_rest_logging_integration.py        # End-to-end REST logging tests (7 tests)
├── test_activemq_ssl_connection.py          # ActiveMQ SSL connectivity (4 tests)
├── test_django_https_authentication.py     # Django HTTPS auth unit tests (7 tests)
├── test_django_dual_server_integration.py  # Live server integration tests (5 tests)
├── test_mcp_rest.py                         # MCP REST API endpoints (6 tests)
├── test_sse_stream.py                       # SSE message streaming tests (5 tests)
└── test_rest_logging.py                     # REST logging utilities (1 test)
```

## Running Tests

### Recommended Method
```bash
./run_tests.py
```

This script automatically:
- Activates the virtual environment (uses swf-testbed's .venv if available)
- Runs pytest with proper Django configuration
- Provides detailed test output and coverage

### Alternative Methods
```bash
# Django's built-in test runner
python manage.py test

# Direct pytest (requires manual environment setup)
pytest

# Run specific test files
./run_tests.py src/monitor_app/tests/test_django_https_authentication.py

# Run with verbose output
python -m pytest -v -s
```

## Test Types and Patterns

### Authentication Patterns

**🚨 CRITICAL FOR AI DEVELOPERS: Two distinct authentication patterns exist**

#### Pattern A: APITestCase + force_authenticate() (Unit Tests)
**Use for**: Testing individual API endpoints within Django's test framework
**Authentication**: Uses Django's `force_authenticate()` - bypasses actual token validation
**Network**: No real HTTP requests - uses Django's test client

```python
class SystemAgentAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password=os.getenv('SWF_TESTUSER_PASSWORD'))
        self.client.force_authenticate(user=self.user)  # Bypasses token auth
    
    def test_create_agent(self):
        response = self.client.post('/api/systemagents/', data, format='json')
        self.assertEqual(response.status_code, 201)
```

#### Pattern B: TestCase + Real Tokens + HTTP Requests (Integration Tests)
**Use for**: Testing against running Django servers (dual server, live integration)
**Authentication**: Creates real Django User and Token objects, uses actual HTTP authentication
**Network**: Makes real HTTP requests to running servers

```python
class AgentMonitorIntegrationTest(TestCase):
    def setUp(self):
        # Create REAL Django user and token in test database
        self.user = User.objects.create_user(
            username='testuser', 
            password=os.getenv('SWF_TESTUSER_PASSWORD')  # Never hardcode passwords!
        )
        self.token = Token.objects.create(user=self.user)
        
        # Configure real HTTP session
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Token {self.token.key}'})
    
    def test_live_server_request(self):
        response = self.session.get('https://localhost:8443/api/systemagents/')
        self.assertEqual(response.status_code, 200)
```

**⚠️ SECURITY REQUIREMENT: Never hardcode passwords**
- Always use `os.getenv('SWF_TESTUSER_PASSWORD')` for test passwords
- Test will skip if environment variable not set
- This prevents password leakage in version control

### Unit Tests
**Purpose**: Test individual components in isolation
**Pattern**: APITestCase with force_authenticate() (Pattern A)
**Example**: `test_system_agent_api.py`

### Integration Tests
**Purpose**: Test interactions between components and external systems
**Pattern**: Live server requests with real HTTP calls
**Example**: `test_django_dual_server_integration.py`

```python
class DjangoDualServerIntegrationTest(TestCase):
    def test_django_https_server_authenticated_request(self):
        headers = {'Authorization': f'Token {self.api_token}'}
        response = requests.get(
            f"{self.django_https_url}/api/systemagents/",
            headers=headers,
            verify=False,
            timeout=5
        )
        self.assertEqual(response.status_code, 200)
```

### API Tests
**Purpose**: Validate REST API endpoints and data handling
**Pattern**: APITestCase with JSON payloads
**Example**: `test_system_agent_api.py`

```python
class SystemAgentAPITests(APITestCase):
    def test_create_system_agent(self):
        data = {'instance_name': 'test-agent', 'agent_type': 'test'}
        response = self.client.post('/api/systemagents/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
```

## Test Configuration

### pytest.ini
```ini
[pytest]
DJANGO_SETTINGS_MODULE = swf_monitor_project.settings
python_files = tests.py test_*.py *_tests.py
pythonpath = src
markers =
    django_db: mark a test as requiring the Django database
```

### conftest.py
Shared fixtures and configuration for all tests:
- Database setup and teardown
- Common test data creation
- Authentication helpers
- Mock configurations

## Test Data Management

### Fixtures
- **Real data**: Uses Django's fixture system for static reference data
- **Generated data**: Creates test-specific data in setUp() methods
- **Isolated**: Each test gets a clean database state

### Test Database
- **Automatic creation**: Django creates `test_swfdb` database
- **Migration application**: All migrations run before tests
- **Cleanup**: Database destroyed after test completion

## Best Practices

### File Organization
1. **One test class per file**: Maintains focus and clarity
2. **Descriptive naming**: `test_<component>_<type>.py` pattern
3. **Logical grouping**: Related functionality in same file
4. **Manageable size**: Each file under 200 lines for AI-friendly navigation

### Test Writing
1. **Clear test names**: Describe what is being tested
2. **AAA pattern**: Arrange, Act, Assert structure
3. **Isolated tests**: Each test is independent
4. **Meaningful assertions**: Check behavior, not implementation

### Naming Conventions
```python
# Good: Describes what is being tested
def test_django_https_authenticated_request_with_token(self):
    """Test that authenticated Django HTTPS requests with token work correctly."""

# Bad: Vague test name
def test_auth(self):
    """Test authentication."""
```

## Coverage Areas

### Django Framework
- Model validation and relationships
- View rendering and form processing
- URL routing and middleware
- Template rendering and context

### REST API
- CRUD operations for all models
- Authentication and authorization
- Input validation and error handling
- Response formatting and status codes

### Security
- HTTPS certificate validation
- Token-based authentication
- Permission enforcement
- SQL injection prevention

### External Integrations
- ActiveMQ SSL connections
- Database connectivity
- Message queue dispatch
- WebSocket communication

## Continuous Integration

The test suite is designed for CI/CD environments:
- **No external dependencies**: Tests run in isolation
- **Predictable**: Consistent results across environments
- **Fast execution**: Optimized for rapid feedback
- **Clear reporting**: Detailed test output and failure messages

## Troubleshooting

### Common Issues

**Test Discovery Problems**
```bash
# Ensure proper Python path
export PYTHONPATH=/direct/eic+u/wenauseic/github/swf-monitor/src
```

**Database Connection Errors**
```bash
# Check PostgreSQL is running
systemctl status postgresql
# Verify test database permissions
psql -U admin -d test_swfdb
```

**Import Errors**
```bash
# Verify virtual environment
source /eic/u/wenauseic/github/swf-testbed/.venv/bin/activate
# Check Django settings
python -c "import django; django.setup(); print('Django OK')"
```

### Test-Specific Issues

**Integration Tests Failing**
- Ensure Django servers are running for live server tests
- Check environment variables are properly set
- Verify SSL certificates exist for HTTPS tests

**Authentication Tests Failing**
- Confirm API tokens are properly configured
- Check user permissions and groups
- Verify session middleware is enabled

## Adding New Tests

### 1. Choose Test Type
- **Unit test**: Test single component in isolation
- **API test**: Test REST endpoint functionality  
- **Integration test**: Test component interactions
- **UI test**: Test web interface behavior

### 2. Create Test File
```python
# src/monitor_app/tests/test_new_feature_api.py
from django.test import TestCase
from rest_framework.test import APITestCase
from monitor_app.models import YourModel

class NewFeatureAPITests(APITestCase):
    def setUp(self):
        """Set up test data."""
        pass
    
    def test_new_feature_functionality(self):
        """Test the new feature works correctly."""
        # Arrange
        # Act  
        # Assert
        pass
```

### 3. Follow Naming Convention
- File: `test_<component>_<type>.py`
- Class: `<Component><Type>Tests`
- Methods: `test_<specific_behavior>`

### 4. Run and Validate
```bash
# Run just your new test
./run_tests.py src/monitor_app/tests/test_new_feature_api.py

# Run full suite to ensure no regressions
./run_tests.py
```

## SSE (Server-Sent Events) Testing

### Overview
The SSE streaming tests (`test_sse_stream.py`) validate real-time message broadcasting functionality used by the monitor to push workflow events to connected clients.

### Test Architecture
The SSE tests use **Django's test infrastructure** rather than external HTTP connections, providing:
- **Fast execution** (< 5 seconds vs 70+ second timeouts)
- **Reliable results** (no network timing issues)
- **Proper isolation** (no interference between tests)

### Test Classes

#### TestSSEBroadcaster
Tests the core message broadcasting logic:
- **Message Broadcasting**: Validates messages reach connected clients
- **Message Filtering**: Ensures filtering by message type and agent works
- **Client Management**: Tests client connection/disconnection lifecycle
- **Channel Layer Integration**: Validates Redis-backed cross-process messaging

#### TestSSEEndpoint  
Tests HTTP endpoint behavior:
- **Authentication**: Verifies token-based auth is required
- **Response Format**: Validates SSE content-type headers
- **Status Endpoint**: Tests broadcaster status reporting

### Key Features Tested

1. **Message Filtering**
   ```python
   filters = {'msg_types': ['data_ready'], 'agents': ['data-agent']}
   # Only matching messages reach the client
   ```

2. **Client Queue Management**
   ```python
   client_queue = broadcaster.add_client(client_id, request, filters)
   # Messages queued for delivery to specific clients
   ```

3. **Channel Layer Fanout**
   ```python
   async_to_sync(channel_layer.group_send)('workflow_events', {...})
   # Redis-based message distribution across processes
   ```

### Test Execution
```bash
# Run SSE tests specifically
./run_tests.py src/monitor_app/tests/test_sse_stream.py

# Run with verbose output to see detailed SSE operations
python -m pytest monitor_app/tests/test_sse_stream.py -v
```

### Architecture Lessons Learned
- **Avoid external connections in tests** - use Django's test client instead
- **Test core logic directly** - don't rely on network timing
- **Mock timing-dependent components** - background threads, async operations
- **Use TransactionTestCase** for tests that need real database transactions

---

This comprehensive test system ensures the SWF Monitor maintains high quality, security, and reliability while providing clear guidance for developers and AI assistants working with the codebase.

## History

The current organized test structure was created through a comprehensive refactoring effort. For details about this transformation, see [Test Refactoring Report](TEST_REFACTORING_REPORT.md).