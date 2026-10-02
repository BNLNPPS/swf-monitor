"""Alarm: tls_served_chain.

Every client of the monitor, the agents, the MCP endpoint and the external
proxy among them, verifies the chain the server sends. This alarm opens a TLS
connection to each listed endpoint the way such a client does and fires when
the served chain does not verify, which is what a certificate replaced
without its intermediate, or without a reload, produces at the next reload.

On 2026-10-02 pandaserver02's certificate was replaced by one issued from a
new intermediate while Apache's chain file still named the old one; the
first reload made every verifying client fail with "unable to verify the
first certificate", the prod-ops agent among them.
"""
from __future__ import annotations

import socket
import ssl

from ..common import Detection

PARAMS = {
    # host or host:port, comma separated; port defaults to 443.
    "hosts": ("pandaserver02.sdcc.bnl.gov, pandaserver01.sdcc.bnl.gov:25443, "
              "nprucio01.sdcc.bnl.gov, epic-devcloud.org"),
    # Verify as the agents do: the default store (SSL_CERT_FILE, the combined
    # root bundle), never a directory of intermediates such as
    # /etc/grid-security/certificates, which would supply a missing
    # intermediate and pass the very chain the agents reject.
    "timeout_seconds": 15,
}


def _targets(raw):
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        yield host, int(port or 443)


def detect(client, params):
    context = ssl.create_default_context()
    timeout = float(params.get("timeout_seconds", 15))
    for host, port in _targets(params.get("hosts", "")):
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                with context.wrap_socket(sock, server_hostname=host):
                    pass
        except ssl.SSLCertVerificationError as exc:
            yield Detection(
                dedupe_key=f"tls_chain:{host}:{port}",
                subject=f"TLS chain does not verify at {host}:{port}: {exc.verify_message}",
                body_context=(
                    f"A verifying client cannot establish TLS to {host}:{port} "
                    f"({exc.verify_message}). Every agent and tool that verifies the "
                    "server fails the same way. Check the served chain with "
                    f"openssl s_client -connect {host}:{port} and the server's "
                    "certificate and chain files."),
                extra_data={"host": host, "port": port, "error": str(exc)[:300]},
            )
        except (OSError, ssl.SSLError):
            # Unreachable or a non-verification handshake failure: not this
            # alarm's question, and a transient network fault must not fire.
            continue
