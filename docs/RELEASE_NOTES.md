# Release Notes

## v30 (2025-02-03)

### Auth0 OAuth 2.1 Authentication for Claude.ai MCP

Added secure OAuth 2.1 authentication for remote MCP connections from Claude.ai, using [Auth0](https://auth0.com/) as the identity provider.

**Why Auth0?**
- Industry-standard OAuth 2.1 / OpenID Connect implementation
- Handles JWT token issuance, validation, and key rotation
- Supports the OAuth 2.1 authorization code flow required by Claude.ai's remote MCP

**How it works:**
1. Claude.ai discovers OAuth metadata via `/.well-known/oauth-protected-resource`
2. User authenticates with Auth0 (redirected to Auth0's login page)
3. Auth0 issues JWT access token to Claude.ai
4. Claude.ai includes Bearer token in MCP requests
5. Django middleware validates JWT against Auth0's JWKS endpoint

**Configuration:**
```bash
AUTH0_DOMAIN=your-tenant.us.auth0.com
AUTH0_CLIENT_ID=your-client-id
AUTH0_CLIENT_SECRET=your-client-secret
AUTH0_API_IDENTIFIER=https://your-server/swf-monitor/mcp
```

**Access modes:**
- **Claude.ai (remote)**: Requires OAuth authentication via Auth0
- **Claude Code (local)**: POST requests pass through without auth for local development

**Network requirement:** Claude.ai connects from Anthropic's servers, so the MCP endpoint must be accessible from the public internet.

### MCP Tool Naming Convention

Renamed all 29 MCP tools with `swf_` service prefix for multi-server discovery:
- `list_agents` → `swf_list_agents`
- `get_system_state` → `swf_get_system_state`
- etc.

This follows MCP best practices for environments where multiple MCP servers are connected. The prefix enables clean tool discovery and avoids naming collisions.

Reference: https://www.philschmid.de/mcp-best-practices

### Pagination Metadata for List Tools

All list tools now return pagination metadata to help LLMs manage context:

```json
{
  "items": [...],
  "total_count": 1523,
  "has_more": true,
  "monitor_urls": [...]
}
```

- `total_count`: Total matching records in database
- `has_more`: Boolean indicating results are truncated

This helps LLMs understand when query results are incomplete and whether to refine filters.

### New MCP Tool: swf_send_message

Send messages to the workflow monitoring stream:

```python
swf_send_message(
    message="Test message",
    message_type="announcement",  # or "test", custom types
    metadata={"key": "value"}     # optional
)
```

Use cases:
- Testing the message pipeline end-to-end
- Sending announcements to colleagues monitoring the stream
- Debugging SSE relay functionality

### Bug Fixes

- **Fixed monitor URLs in MCP responses**: Tool responses were returning localhost URLs instead of production URLs. Now correctly returns URLs based on deployment configuration.

### Documentation

- Updated `docs/MCP.md` with all swf_ prefixed tool names
- Documented Auth0 OAuth 2.1 configuration and flow
- Added pagination metadata documentation
- Noted that `.env` files are not deployed from git (must be configured on server)
