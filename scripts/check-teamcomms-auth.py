#!/usr/bin/env python3
"""Check the SWF proxy contract without network or database writes."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")

import django
django.setup()
logging.getLogger("httpx").setLevel(logging.WARNING)

from django.conf import settings
import httpx
from monitor_app.teamcomms_auth import (
    PUBLIC_PREFIX, READ_SCOPES, WRITE_SCOPES, RemoteAuthentication, TrustedProxy,
)
from teamcomms.service.access import AccessError


async def expect_denied(call, status):
    try:
        await call()
    except AccessError as error:
        assert error.status == status, (error.status, status)
    else:
        raise AssertionError("Request was admitted")


async def main():
    payload = b'{"request":{}}'
    scope = {"type": "http", "method": "POST", "path": PUBLIC_PREFIX + "/mcp/",
             "query_string": b"one=a%20b", "headers": [(b"x-teamcomms-auth-ref", b"reference")],
             "teamcomms.body_sha256": hashlib.sha256(payload).hexdigest()}
    record = {"subject": "12", "username": "member", "name": "Member", "kind": "human",
              "auth_method": "session", "csrf_verified": True,
              "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
              "method": "POST", "path": "/mcp/", "query_string": "one=a%20b",
              "body_sha256": hashlib.sha256(payload).hexdigest()}
    current = deepcopy(record)
    remote_status = 200

    def respond(request):
        assert request.headers["authorization"] == "Bearer synthetic-service-key"
        return httpx.Response(remote_status, json=current)

    with tempfile.NamedTemporaryFile(mode="w") as token_file:
        token_file.write("synthetic-service-key")
        token_file.flush()
        settings.SWF_TEAMCOMMS_SERVICE_TOKEN_FILE = token_file.name
        settings.SWF_TEAMCOMMS_INTROSPECTION_URL = "https://authority.invalid/introspect/"
        settings.SWF_TEAMCOMMS_PUBLIC_HOST = "epic-devcloud.org"
        auth = RemoteAuthentication()
        await auth.client.aclose()
        auth.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with patch("monitor_app.teamcomms_auth.database_call", AsyncMock(
                    return_value=(READ_SCOPES | WRITE_SCOPES, "member"))):
                checked = deepcopy(scope)
                identity = await auth.resolve_request(checked)
                assert identity.subject == "12" and identity.session_authenticated
                assert await auth.check_request_csrf(checked, payload, identity)
                for key, value in [("method", "GET"), ("path", "/api/whoami"),
                                   ("query_string", "one=a+b"), ("body_sha256", "0" * 64)]:
                    current = {**record, key: value}
                    await expect_denied(lambda: auth.resolve_request(deepcopy(scope)), 401)
                current = {**record, "expires_at": "2020-01-01T00:00:00+00:00"}
                await expect_denied(lambda: auth.resolve_request(deepcopy(scope)), 401)
                current = {**record, "csrf_verified": False}
                checked = deepcopy(scope)
                identity = await auth.resolve_request(checked)
                assert not await auth.check_request_csrf(checked, payload, identity)
                current = {**record, "kind": "ai", "subject": "ai:12", "auth_method": "token",
                           "operator": {"subject": "12", "username": "member", "name": "Member"}}
                identity = await auth.resolve_request(deepcopy(scope))
                assert identity.operator.subject == "12" and not identity.session_authenticated
                current["operator"]["username"] = "someone-else"
                await expect_denied(lambda: auth.resolve_request(deepcopy(scope)), 503)
                for kind in ("program", "connector"):
                    service = {**record, "kind": kind, "subject": kind + ":12",
                               "account_subject": "12", "auth_method": "token"}
                    current = deepcopy(service)
                    identity = await auth.resolve_request(deepcopy(scope))
                    assert identity.kind == kind and identity.subject == kind + ":12"
                    assert identity.operator is None and not identity.session_authenticated
                    assert identity.scopes == READ_SCOPES | WRITE_SCOPES
                    for key, value in [("auth_method", "session"), ("subject", kind + ":13"),
                                       ("subject", "ai:12"), ("account_subject", "13"),
                                       ("account_subject", "012"), ("account_subject", "１２"),
                                       ("account_subject", "0"), ("account_subject", 12),
                                       ("operator", {})]:
                        current = {**service, key: value}
                        await expect_denied(lambda: auth.resolve_request(deepcopy(scope)), 503)
                    current = {key: value for key, value in service.items()
                               if key != "account_subject"}
                    await expect_denied(lambda: auth.resolve_request(deepcopy(scope)), 503)
                current = record
                for remote_status in (401, 403, 503):
                    await expect_denied(lambda: auth.revalidate_request(deepcopy(scope), identity), remote_status)
                remote_status = 200
                duplicate = deepcopy(scope)
                duplicate["headers"].append((b"x-teamcomms-auth-ref", b"another"))
                await expect_denied(lambda: auth.resolve_request(duplicate), 401)
        finally:
            await auth.client.aclose()

    captured = []

    async def capture(incoming, receive, send):
        captured.append(incoming)

    proxy = TrustedProxy(capture)
    incoming = {"type": "http", "client": ("127.0.0.1", 42), "path": "/swf-monitor/teamcomms/mcp/",
                "headers": [(b"x-teamcomms-auth-ref", b"reference"), (b"host", b"localhost"),
                            (b"x-forwarded-host", b"epic-devcloud.org"), (b"x-forwarded-proto", b"https")]}
    await proxy(incoming, AsyncMock(), AsyncMock())
    assert captured[0]["path"] == "/prod/teamcomms/mcp/"
    assert captured[0]["scheme"] == "https"
    assert dict(captured[0]["headers"])[b"host"] == b"epic-devcloud.org"
    incoming["client"] = ("192.0.2.5", 42)
    sent = AsyncMock()
    await proxy(incoming, AsyncMock(), sent)
    assert sent.call_args_list[0].args[0]["status"] == 401 and len(captured) == 1
    print("TeamComms proxy binding, identity, CSRF and revalidation checks passed (no DB writes).")


if __name__ == "__main__":
    asyncio.run(main())
