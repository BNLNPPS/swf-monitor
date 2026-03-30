"""Authentication middleware and DRF backends for MCP OAuth 2.1 and tunnel proxy."""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from rest_framework.authentication import BaseAuthentication

from .auth0 import get_bearer_token, validate_token

logger = logging.getLogger(__name__)

LOCALHOST_IPS = {'127.0.0.1', '::1'}


def _is_localhost(request):
    return request.META.get('REMOTE_ADDR', '') in LOCALHOST_IPS


class TunnelAuthentication(BaseAuthentication):
    """DRF authentication backend for SSH tunnel (localhost) requests.

    Authenticates via X-Remote-User header on localhost requests, bypassing
    CSRF. Must be listed BEFORE SessionAuthentication in authentication_classes
    so DRF uses it first for tunnel requests and never reaches CSRF checks.

    Falls back to a generic 'swf-remote-proxy' user if no header is present.
    Returns None (skip) for non-localhost requests, letting the next backend try.
    """

    def authenticate(self, request):
        if not _is_localhost(request):
            return None
        User = get_user_model()
        remote_user = request.META.get('HTTP_X_REMOTE_USER', '').strip()
        if remote_user:
            user, created = User.objects.get_or_create(
                username=remote_user,
                defaults={'is_active': True},
            )
            if created:
                logger.info(f"Auto-created user '{remote_user}' from tunnel proxy")
        else:
            user, _ = User.objects.get_or_create(
                username='swf-remote-proxy',
                defaults={'is_active': True},
            )
        return (user, None)


class TunnelAuthMiddleware:
    """Auto-authenticate requests from localhost (SSH tunnel proxy).

    Must be placed after AuthenticationMiddleware in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated and _is_localhost(request):
            # Reuse same logic as TunnelAuthentication DRF backend
            auth = TunnelAuthentication()
            result = auth.authenticate(request)
            if result:
                request.user = result[0]
        return self.get_response(request)


def tunnel_context(request):
    """Template context processor: sets is_tunnel for localhost requests."""
    return {'is_tunnel': _is_localhost(request)}


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
