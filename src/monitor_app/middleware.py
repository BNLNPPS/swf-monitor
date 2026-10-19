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


def is_tunnel_request(request):
    """True when the request arrived through the localhost SSH proxy hop."""
    return _is_localhost(request)


class TunnelAuthentication(BaseAuthentication):
    """DRF authentication backend for SSH tunnel (localhost) requests.

    Authenticates via X-Remote-User header on localhost requests, bypassing
    CSRF. Must be listed BEFORE SessionAuthentication in authentication_classes
    so DRF uses it first for tunnel requests and never reaches CSRF checks.

    Returns None (skip) for non-localhost requests, and for localhost requests
    carrying no X-Remote-User, letting the next backend try. It used to fall
    back to a generic 'swf-remote-proxy' user in that second case, which made
    any localhost request without the header — a valid token, a garbage
    token, nothing at all — act as that user before a token was ever read;
    the hourly production-record writer ran under it (docs/AUTHORITY.md).
    swf-remote's own service calls name their identity explicitly.
    """

    def authenticate(self, request):
        if not _is_localhost(request):
            return None
        remote_user = request.META.get('HTTP_X_REMOTE_USER', '').strip()
        if not remote_user:
            return None
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=remote_user,
            defaults={'is_active': True},
        )
        if created:
            logger.info(f"Auto-created user '{remote_user}' from tunnel proxy")
        return (user, None)


class TunnelAuthMiddleware:
    """Auto-authenticate requests from localhost (SSH tunnel proxy).

    Must be placed after AuthenticationMiddleware in MIDDLEWARE.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _is_localhost(request):
            # Tunnel/localhost requests authenticate via X-Remote-User over the
            # trusted SSH tunnel, not a browser session, so session CSRF is
            # meaningless for them (mirrors TunnelAuthentication for DRF). Exempt
            # them so proxied form POSTs (e.g. the catalog Update-from-CSV button,
            # whose token the proxy can't forward) are not rejected. Direct
            # browser users (non-localhost) keep full CSRF protection.
            request._dont_enforce_csrf_checks = True
        if not request.user.is_authenticated and _is_localhost(request):
            remote_user = request.META.get('HTTP_X_REMOTE_USER', '').strip()
            if remote_user:
                User = get_user_model()
                user, created = User.objects.get_or_create(
                    username=remote_user,
                    defaults={'is_active': True},
                )
                if created:
                    logger.info(f"Auto-created user '{remote_user}' from tunnel proxy")
                request.user = user
            # No X-Remote-User → leave request anonymous (proxy user not logged in)
        return self.get_response(request)


def tunnel_context(request):
    """Template context processor: sets is_tunnel for localhost requests."""
    return {'is_tunnel': is_tunnel_request(request)}


# Writes that must work for anyone, or that carry a stricter check of their
# own: signing in and out, changing a password, and the two authority
# endpoints, which admit only the swf-remote service identity or an
# administrator (viewdir/authority_api.py, viewdir/user_admin.py).
AUTHORITY_EXEMPT_URL_NAMES = frozenset({
    'login', 'logout', 'password_change', 'password_change_done',
    'user-authority', 'user-rights',
})

_UNSAFE_METHODS = frozenset({'POST', 'PATCH', 'PUT', 'DELETE'})
_JSON_PATH_PREFIXES = ('/api/', '/pcs/api/', '/mcp')


class AuthorityGateMiddleware:
    """The one rule: a person who writes must hold authority.

    Runs after TunnelAuthMiddleware, so a request that carries a person —
    a browser session, or the identity swf-remote forwards over the tunnel
    — is authenticated by now. On an unsafe method that person must
    ``may_act`` (monitor_app.authority), or the request is refused with the
    joining procedure. A request carrying no person — machinery on a
    service token, or nothing — is left to the view's own authentication,
    exactly as before; the gate adds protection for people and removes
    nothing else.

    Enforcement is the SysConfig knob ``authority_enforce``. While it is
    false the gate observes: every refusal it would have made is logged
    and the request proceeds, so live traffic proves the rule before it
    bites. docs/AUTHORITY.md.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method in _UNSAFE_METHODS:
            refusal = self._refusal(request)
            if refusal is not None:
                return refusal
        return self.get_response(request)

    @staticmethod
    def _wants_json(request):
        path = request.path_info
        if path.startswith(_JSON_PATH_PREFIXES):
            return True
        accept = request.META.get('HTTP_ACCEPT', '')
        return ('application/json' in accept
                or request.content_type == 'application/json')

    def _refusal(self, request):
        from django.urls import Resolver404, resolve

        from .authority import enforcing, may_act, refusal_text

        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return None
        # Exemption is by resolved URL name; a path that resolves to nothing
        # is not exempt — the check still runs, and the view 404s after.
        try:
            if resolve(request.path_info).url_name in AUTHORITY_EXEMPT_URL_NAMES:
                return None
        except Resolver404:
            pass
        if may_act(user.username):
            return None

        text = refusal_text(user.username)
        if not enforcing():
            logger.warning(
                'authority (observing, not enforced): would refuse %s %s '
                'by %s — %s', request.method, request.path_info,
                user.username, text.splitlines()[0])
            return None
        logger.info('authority: refused %s %s by %s', request.method,
                    request.path_info, user.username)
        if self._wants_json(request):
            return JsonResponse({'error': text, 'authority': 'refused'},
                                status=403)
        from django.shortcuts import render
        return render(request, 'monitor_app/authority_refused.html',
                      {'refusal': text}, status=403)


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

        transport_response = self._validate_mcp_transport(request)
        if transport_response:
            return transport_response

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

    def _validate_mcp_transport(self, request):
        """Keep MCP as finite JSON POST request/response; no MCP GET/SSE."""
        if request.method != "POST":
            response = JsonResponse(
                {
                    "error": "MCP endpoint accepts POST JSON-RPC only",
                    "allowed_methods": ["POST"],
                },
                status=405,
            )
            response["Allow"] = "POST"
            return response

        accept = request.META.get("HTTP_ACCEPT", "")
        if any(
            part.split(";", 1)[0].strip().lower() == "text/event-stream"
            for part in accept.split(",")
        ):
            return JsonResponse(
                {"error": "MCP server-pushed event streams are not supported"},
                status=406,
            )

        return None

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
