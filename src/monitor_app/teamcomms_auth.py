"""Devcloud authentication and monitor authorization for embedded TeamComms.

The public proxy supplies an opaque reference to a single authenticated request.
Devcloud revalidates its underlying session or token on every introspection.
The monitor retains the authoritative component permissions and database.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from starlette.responses import JSONResponse
from teamcomms.service.access import AccessError
from teamcomms.service.dispatch import database_call
from teamcomms.service.embedded import HostAuthentication, HostIdentity

from .authority import get_authority, is_ops

logger = logging.getLogger(__name__)
BACKEND_PREFIX = "/swf-monitor/teamcomms"
PUBLIC_PREFIX = "/prod/teamcomms"
TEAM_ID = "8873a572-a319-4a90-bfc9-76a9b76e30fb"
READ_SCOPES = frozenset({"directory:read", "entries:read", "comms:read", "dialog:read"})
WRITE_SCOPES = frozenset({"entries:write", "sessions:write", "comms:write", "dialog:write"})


def one_header(scope, name):
    values = [value.decode("latin1") for key, value in scope.get("headers", [])
              if key.lower() == name]
    if len(values) != 1 or not values[0]:
        raise AccessError("TeamComms proxy authentication required", 401)
    return values[0]


def component_permissions(username):
    account = get_user_model().objects.filter(username=username).only("is_active").first()
    if account is not None and not account.is_active:
        raise AccessError("Monitor account is inactive", 401)
    authority = get_authority(username)
    return READ_SCOPES | WRITE_SCOPES, "admin" if is_ops(authority) else "member"


class RemoteAuthentication(HostAuthentication):
    def __init__(self):
        super().__init__(provider="swf-remote", team_id=TEAM_ID,
                         resolve=self.resolve_request, revalidate=self.revalidate_request,
                         check_csrf=self.check_request_csrf)
        self.client = httpx.AsyncClient(timeout=3.0, follow_redirects=False, trust_env=False)

    async def authenticate(self, scope, body):
        # TC has already bounded the body to 64 KiB. Bind token requests as well
        # as cookie requests before the base class maps or admits an identity.
        scope["teamcomms.body_sha256"] = hashlib.sha256(body).hexdigest()
        return await super().authenticate(scope, body)

    async def introspect(self, scope):
        reference = one_header(scope, b"x-teamcomms-auth-ref")
        if len(reference) > 256:
            raise AccessError("Invalid TeamComms authentication reference", 401)
        endpoint = settings.SWF_TEAMCOMMS_INTROSPECTION_URL
        secret_file = settings.SWF_TEAMCOMMS_SERVICE_TOKEN_FILE
        try:
            secret = Path(secret_file).read_text().strip() if secret_file else ""
        except OSError:
            raise AccessError("TeamComms service credential unavailable", 503) from None
        target = urlsplit(endpoint)
        if (target.scheme != "https" or not target.hostname or target.username
                or target.password or target.fragment or not secret):
            raise AccessError("TeamComms authentication is not configured", 503)
        try:
            response = await self.client.post(endpoint, json={"reference": reference},
                                              headers={"Authorization": f"Bearer {secret}"})
        except httpx.HTTPError as error:
            logger.warning("TeamComms introspection unavailable (%s)", type(error).__name__)
            raise AccessError("Authentication authority unavailable", 503) from None
        if response.status_code in (401, 403):
            raise AccessError("Authentication expired, revoked or denied", response.status_code)
        if response.status_code != 200:
            raise AccessError("Authentication authority unavailable", 503)
        try:
            data = response.json()
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError("Expiry requires a timezone")
            if expires <= datetime.now(timezone.utc):
                raise AccessError("Authentication reference expired", 401)
            suffix = scope["path"].removeprefix(PUBLIC_PREFIX)
            if (data["method"] != scope["method"] or data["path"] != suffix
                    or data["query_string"] != scope.get("query_string", b"").decode("latin1")
                    or not hmac.compare_digest(data["body_sha256"], scope["teamcomms.body_sha256"])):
                raise AccessError("Authentication reference does not match request", 401)
            if data["auth_method"] not in ("session", "token"):
                raise ValueError("Unknown authentication method")
            if not isinstance(data["csrf_verified"], bool):
                raise ValueError("Invalid CSRF attestation")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise AccessError("Invalid authentication authority response", 503) from None
        scope["teamcomms.attestation"] = data
        return data

    async def resolve_request(self, scope):
        data = await self.introspect(scope)
        try:
            subject, username, name = data["subject"], data["username"], data["name"]
            if not all(isinstance(value, str) and value for value in (subject, username, name)):
                raise ValueError("Invalid identity")
            session_authenticated = data["auth_method"] == "session"
            scopes, role = await database_call(component_permissions, username)
            operator = None
            if data["kind"] == "ai":
                owner = data["operator"]
                if (session_authenticated or subject != "ai:" + owner["subject"]
                        or owner["username"] != username or not owner["subject"].isdigit()):
                    raise ValueError("Invalid AI/operator binding")
                operator = HostIdentity(subject=owner["subject"], name=owner["name"],
                                        scopes=scopes, role=role, session_authenticated=False)
            elif data["kind"] != "human" or not subject.isdigit() or data.get("operator"):
                raise ValueError("Invalid human binding")
            identity = HostIdentity(subject=subject, name=name, kind=data["kind"],
                                    scopes=scopes, role=role,
                                    session_authenticated=session_authenticated, operator=operator)
            identity.validate()
            return identity
        except (ValueError, TypeError, KeyError, AttributeError):
            raise AccessError("Invalid authentication authority identity", 503) from None

    async def revalidate_request(self, scope, original_identity):
        return await self.resolve_request(scope)

    async def check_request_csrf(self, scope, body, identity):
        return scope["teamcomms.attestation"]["csrf_verified"] is True


class TrustedProxy:
    """Accept the designated loopback hop and restore the public URL for TC."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            if (scope.get("client") or ("", 0))[0] not in ("127.0.0.1", "::1"):
                raise AccessError("TeamComms requires the authenticated devcloud proxy", 401)
            host = one_header(scope, b"x-forwarded-host")
            if host != settings.SWF_TEAMCOMMS_PUBLIC_HOST or one_header(scope, b"x-forwarded-proto") != "https":
                raise AccessError("Invalid TeamComms proxy origin", 401)
            one_header(scope, b"x-teamcomms-auth-ref")
            path = scope["path"]
            if not (path == BACKEND_PREFIX or path.startswith(BACKEND_PREFIX + "/")):
                raise AccessError("Invalid TeamComms proxy path", 404)
        except AccessError as error:
            return await JSONResponse({"error": str(error)}, status_code=error.status,
                                      headers={"Cache-Control": "no-store"})(scope, receive, send)
        scope = dict(scope)
        scope["path"] = PUBLIC_PREFIX + path[len(BACKEND_PREFIX):]
        raw = scope.get("raw_path", path.encode("utf8"))
        scope["raw_path"] = PUBLIC_PREFIX.encode() + raw[len(BACKEND_PREFIX):]
        scope["root_path"] = ""
        scope["scheme"] = "https"
        scope["server"] = (host, 443)
        scope["headers"] = [(key, value) for key, value in scope["headers"] if key.lower() != b"host"]
        scope["headers"].append((b"host", host.encode("ascii")))
        await self.app(scope, receive, send)
