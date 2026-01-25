"""Authentication middleware for MCP OAuth 2.1 integration."""

import logging

from django.conf import settings
from django.http import JsonResponse

from .auth0 import get_bearer_token, validate_token

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    """
    Middleware for MCP endpoint authentication.

    Authentication modes:
    1. Bearer token present: Validate via Auth0, allow if valid
    2. No token + MCP path: Return 401 with OAuth metadata for discovery
    3. No token + other paths: Pass through (existing behavior)

    This allows:
    - Claude.ai: OAuth flow via Auth0
    - Claude Code: Direct access without auth (local config)
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Build the MCP path prefix accounting for FORCE_SCRIPT_NAME
        script_name = getattr(settings, 'FORCE_SCRIPT_NAME', None) or ""
        mcp_path = f"{script_name}/mcp"

        # Only apply to MCP endpoints (with or without trailing slash)
        if not (request.path == mcp_path or request.path.startswith(mcp_path + "/")):
            return self.get_response(request)

        # Check for Bearer token
        token = get_bearer_token(request)

        if token:
            # Validate the token
            payload = validate_token(token)
            if payload:
                # Token valid - attach user info to request and proceed
                request.auth0_payload = payload
                request.auth0_user = payload.get("sub")
                return self.get_response(request)
            else:
                # Invalid token - return 401
                return self._unauthorized_response(request, "Invalid or expired token")

        # No token present - determine behavior by request method:
        # - POST: Claude Code making tool calls, allow through
        # - GET: Claude.ai doing OAuth discovery, return 401 with metadata
        if request.method == "POST":
            # Claude Code - allow through without auth
            return self.get_response(request)

        # GET request - if Auth0 configured, trigger OAuth discovery
        auth0_domain = getattr(settings, 'AUTH0_DOMAIN', None)
        if auth0_domain:
            return self._oauth_required_response(request)

        # Auth0 not configured - allow all through
        return self.get_response(request)

    def _unauthorized_response(self, request, message: str):
        """Return 401 for invalid token."""
        response = JsonResponse({"error": "unauthorized", "message": message}, status=401)
        response["WWW-Authenticate"] = self._www_authenticate_header(request)
        return response

    def _oauth_required_response(self, request):
        """Return 401 with OAuth metadata for discovery."""
        response = JsonResponse(
            {
                "error": "authorization_required",
                "message": "OAuth 2.1 authentication required",
            },
            status=401,
        )
        response["WWW-Authenticate"] = self._www_authenticate_header(request)
        return response

    def _www_authenticate_header(self, request) -> str:
        """Build WWW-Authenticate header for OAuth discovery."""
        # Get the base URL for resource metadata
        scheme = "https" if request.is_secure() else "http"
        host = request.get_host()
        script_name = getattr(settings, 'FORCE_SCRIPT_NAME', None) or ""
        resource_metadata_url = f"{scheme}://{host}{script_name}/.well-known/oauth-protected-resource"

        return (
            f'Bearer realm="{settings.AUTH0_API_IDENTIFIER}", '
            f'resource_metadata="{resource_metadata_url}"'
        )
