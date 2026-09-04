"""Alarm: tls_cert_expiry.

The PanDA and OSG service hosts carry one-year InCommon IGTF host
certificates that renew by hand. An expired certificate reaches
collaborators first, as a browser error on every log link, and a
certificate served without its intermediate fails in any browser
without the grid CA bundle. This alarm reads the certificate each
listed host serves and raises a ping when it is inside the warning
window, expired, unreadable, or served without its chain. The event
clears on the tick after the certificate is renewed.

Severity is ``ping``: a reminder that an action is due, not an outage.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from ..common import Detection

PARAMS = {
    # host or host:port, comma separated; port defaults to 443.
    "hosts": ("osgsub01.sdcc.bnl.gov, pandaharvester01.sdcc.bnl.gov, "
              "pandamon01.sdcc.bnl.gov, pandaserver02.sdcc.bnl.gov, "
              "pandaserver01.sdcc.bnl.gov:25443"),
    # Ping this many days before expiry.
    "warn_days": 7,
    # Per-host connect and handshake limit, seconds.
    "timeout_s": 8,
}

SEVERITY = "ping"


def _targets(raw):
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        yield host, int(port or 443)


def _served_chain(host, port, timeout):
    """The PEM certificates the server sends, leaf first, read through
    ``openssl s_client`` without verification: the point is to read
    what is served, expired or not, and how much of the chain comes
    with it."""
    out = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:{port}",
         "-servername", host, "-showcerts"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    text = out.stdout.decode(errors="replace")
    blocks = []
    current = None
    for line in text.splitlines():
        if line.startswith("-----BEGIN CERTIFICATE-----"):
            current = [line]
        elif current is not None:
            current.append(line)
            if line.startswith("-----END CERTIFICATE-----"):
                blocks.append("\n".join(current) + "\n")
                current = None
    if not blocks:
        tail = (out.stderr.decode(errors="replace").strip().splitlines()
                or ["no certificate in the handshake"])[-1]
        raise RuntimeError(tail[:200])
    return blocks[0], blocks


def _cert_facts(pem, timeout):
    """Subject, issuer, and notAfter of one PEM certificate, read
    through the openssl command line: the engine venv carries no X.509
    parser and the host always has openssl."""
    out = subprocess.run(
        ["openssl", "x509", "-noout", "-subject", "-issuer", "-enddate",
         "-nameopt", "RFC2253"],
        input=pem.encode(), capture_output=True, timeout=timeout, check=True)
    facts = {}
    for line in out.stdout.decode(errors="replace").splitlines():
        key, _, value = line.partition("=")
        facts[key.strip()] = value.strip()
    not_after = datetime.strptime(facts["notAfter"], "%b %d %H:%M:%S %Y %Z")
    return {
        "subject": facts.get("subject", ""),
        "issuer": facts.get("issuer", ""),
        "not_after": not_after.replace(tzinfo=timezone.utc),
    }


def _cn(name):
    for part in name.split(","):
        if part.strip().startswith("CN="):
            return part.strip()[3:]
    return name


def detect(client, params):
    warn_days = int(params.get("warn_days", 7))
    timeout = int(params.get("timeout_s", 8))
    now = datetime.now(timezone.utc)
    for host, port in _targets(params.get("hosts", PARAMS["hosts"])):
        key = f"cert:{host}:{port}"
        label = host if port == 443 else f"{host}:{port}"
        try:
            leaf, chain = _served_chain(host, port, timeout)
            facts = _cert_facts(leaf, timeout)
        except Exception as e:  # noqa: BLE001 - the failure is the finding
            yield Detection(
                dedupe_key=key,
                subject=f"{label}: certificate unreadable ({e})",
                body_context=(
                    f"The TLS handshake with {label} did not yield a "
                    f"certificate: {e}. Until it does, expiry cannot be "
                    "judged; the host or its web service may be down."),
                extra_data={"severity": SEVERITY, "host": host,
                            "port": port, "error": str(e)[:300]},
            )
            continue
        days_left = (facts["not_after"] - now).total_seconds() / 86400
        expiry = facts["not_after"].strftime("%Y-%m-%d")
        issuer = _cn(facts["issuer"])
        self_signed = _cn(facts["subject"]) == issuer
        chain_complete = self_signed or len(chain) > 1
        problems = []
        if days_left < 0:
            problems.append(f"expired {int(-days_left)} days ago ({expiry})")
        elif days_left <= warn_days:
            problems.append(f"expires in {int(days_left)} days ({expiry})")
        if not chain_complete:
            problems.append("served without its intermediate")
        if not problems:
            continue
        yield Detection(
            dedupe_key=key,
            subject=f"{label}: certificate {'; '.join(problems)}",
            body_context=(
                f"Certificate for {label}, issued by {issuer}, valid to "
                f"{expiry} ({int(days_left)} days from now). "
                + ("Renew the host certificate; the event clears on the "
                   "tick after the new certificate is served. "
                   if days_left <= warn_days else "")
                + ("The server sends only the leaf, so a browser without "
                   "the grid CA bundle cannot verify it; include the "
                   "intermediate in the served chain. "
                   if not chain_complete else "")),
            extra_data={"severity": SEVERITY, "host": host, "port": port,
                        "days_left": round(days_left, 1),
                        "not_after": facts["not_after"].isoformat(),
                        "issuer": issuer,
                        "chain_complete": chain_complete},
        )
