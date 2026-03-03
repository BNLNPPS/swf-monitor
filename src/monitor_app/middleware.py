"""Authentication middleware for MCP OAuth 2.1 integration."""

import logging

from django.conf import settings
from django.http import JsonResponse

from .auth0 import get_bearer_token, validate_token

logger = logging.getLogger(__name__)


class MCPAuthMiddleware:
    """
    Middleware for MCP endpoint authentication.

    Bearer token present: validate via Auth0, reject if invalid.
    No token: allow through (Claude Code, local clients).
    Non-MCP paths: pass through.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        script_name = getattr(settings, 'FORCE_SCRIPT_NAME', None) or ""
        mcp_path = f"{script_name}/mcp"

        if not (request.path == mcp_path or request.path.startswith(mcp_path + "/")):
            return self.get_response(request)

        token = get_bearer_token(request)

        if token:
            payload = validate_token(token)
            if payload:
                request.auth0_payload = payload
                request.auth0_user = payload.get("sub")
                return self.get_response(request)
            else:
                return self._unauthorized_response(request, "Invalid or expired token")

        # No token — allow through (Claude Code, local clients)
        return self.get_response(request)

    def _unauthorized_response(self, request, message: str):
        """Return 401 for invalid token."""
        response = JsonResponse({"error": "unauthorized", "message": message}, status=401)
        response["WWW-Authenticate"] = self._www_authenticate_header(request)
        return response

    def _www_authenticate_header(self, request) -> str:
        """Build WWW-Authenticate header."""
        scheme = "https" if request.is_secure() else "http"
        host = request.get_host()
        script_name = getattr(settings, 'FORCE_SCRIPT_NAME', None) or ""
        resource_metadata_url = f"{scheme}://{host}{script_name}/.well-known/oauth-protected-resource"

        return (
            f'Bearer realm="{settings.AUTH0_API_IDENTIFIER}", '
            f'resource_metadata="{resource_metadata_url}"'
        )
