"""Alarm: storage_headroom.

Production output is written to storage behind xrootd doors whose space is
finite. This alarm asks each listed door for the space of its path
(``xrdfs <door> query space <path>``: total and free) and fires when the
free fraction falls below the threshold, before writes start failing.

On 2026-10-02 the EIC area behind BNL-XRD filled; thousands of finished jobs
lost their output to "HTTP 507 Insufficient Storage" before the door probe
reported it down.
"""
from __future__ import annotations

import os
import subprocess

from ..common import Detection

PARAMS = {
    # "name=door|path" items, comma separated.
    "doors": "BNL-XRD=root://epicxrd1.sdcc.bnl.gov:1094|/eic/EPIC",
    # Fire below this free fraction of the total.
    "min_free_fraction": 0.10,
    "proxy": "/data/wenauseic/longproxy-for-rucio",
    "timeout_seconds": 60,
    # The catalog's quota: every RSE the production account has a finite
    # limit on, against the RSE's used bytes, read with the public read
    # account. The RSEs hold production data almost alone.
    "rucio_url": "https://rucio-server.jlab.org:443",
    "rucio_quota_account": "eicprod",
    "rucio_read_account": "eicread",
    # dCache WebDAV doors that report their quota (RFC 4331 properties):
    # "name=url" items, comma separated; the pilot log store among them.
    "webdav_doors": "BNL_PROD_DISK_1=https://dcintdoor.sdcc.bnl.gov:443/pnfs/sdcc.bnl.gov/eic/epic/disk/",
    "webdav_capath": "/etc/grid-security/certificates",
}

_QUOTA_PROPFIND = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                   b'<d:quota-available-bytes/><d:quota-used-bytes/></d:prop></d:propfind>')


def _webdav_quota(url, proxy, capath, timeout):
    """(used_bytes, available_bytes) a WebDAV door reports for ``url``."""
    import re
    import ssl
    import urllib.request
    context = ssl.create_default_context(capath=capath or None)
    context.load_cert_chain(proxy)
    req = urllib.request.Request(url, data=_QUOTA_PROPFIND, method="PROPFIND",
                                 headers={"Depth": "0", "Content-Type": "application/xml"})
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        body = resp.read().decode()
    used = re.search(r"quota-used-bytes>(\d+)<", body)
    avail = re.search(r"quota-available-bytes>(\d+)<", body)
    if not (used and avail):
        return None
    return int(used.group(1)), int(avail.group(1))


def _rucio_quota(params, timeout):
    """[(rse, used_bytes, limit_bytes)] for every RSE with a finite limit."""
    import json
    import urllib.request
    url = params.get("rucio_url")
    reader = params.get("rucio_read_account")
    if not url or not reader:
        return []
    auth = urllib.request.Request(f"{url}/auth/userpass", headers={
        "X-Rucio-Account": reader, "X-Rucio-Username": reader, "X-Rucio-Password": reader})
    with urllib.request.urlopen(auth, timeout=timeout) as resp:
        token = resp.headers.get("X-Rucio-Auth-Token")

    def get(path, accept):
        req = urllib.request.Request(f"{url}{path}", headers={"X-Rucio-Auth-Token": token, "Accept": accept})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()

    limits = json.loads(get(f"/accounts/{params['rucio_quota_account']}/limits/local", "application/json"))
    out = []
    for rse, limit in limits.items():
        if not isinstance(limit, (int, float)) or limit in (float("inf"),) or limit <= 0:
            continue
        used = None
        for line in get(f"/rses/{rse}/usage", "application/x-json-stream").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("source") == "rucio":
                    used = int(row.get("used") or 0)
        if used is not None:
            out.append((rse, used, int(limit)))
    return out


def _doors(raw):
    for item in str(raw).split(","):
        name, _, rest = item.strip().partition("=")
        door, _, path = rest.partition("|")
        if name and door and path:
            yield name.strip(), door.strip(), path.strip()


def _space(door, path, proxy, timeout):
    env = dict(os.environ, X509_USER_PROXY=proxy)
    out = subprocess.run(["xrdfs", door, "query", "space", path], capture_output=True,
                         text=True, timeout=timeout, env=env)
    if out.returncode != 0:
        return None
    fields = dict(kv.split("=", 1) for kv in out.stdout.strip().split("&") if "=" in kv)
    try:
        return int(fields["oss.space"]), int(fields["oss.free"])
    except (KeyError, ValueError):
        return None


def detect(client, params):
    minimum = float(params.get("min_free_fraction", 0.10))
    proxy = params.get("proxy", "")
    timeout = float(params.get("timeout_seconds", 60))
    for name, door, path in _doors(params.get("doors", "")):
        try:
            space = _space(door, path, proxy, timeout)
        except (OSError, subprocess.SubprocessError):
            space = None
        if not space or space[0] <= 0:
            continue  # unanswered: the door probe owns reachability
        total, free = space
        fraction = free / total
        if fraction < minimum:
            yield Detection(
                dedupe_key=f"storage:{name}",
                subject=(f"{name} storage {100 * (1 - fraction):.1f}% full: "
                         f"{free / 1e12:.1f} of {total / 1e12:.1f} TB free"),
                body_context=(
                    f"The space behind {door} {path} has {free / 1e12:.1f} TB free of "
                    f"{total / 1e12:.1f} TB ({100 * fraction:.1f}%, threshold "
                    f"{100 * minimum:.0f}%). Jobs writing there fail once it is full; "
                    "free space or redirect output before then."),
                extra_data={"rse": name, "door": door, "path": path, "total_bytes": total,
                            "free_bytes": free, "free_fraction": round(fraction, 4)},
            )

    for item in str(params.get("webdav_doors", "")).split(","):
        name, _, url = item.strip().partition("=")
        if not (name and url):
            continue
        try:
            quota = _webdav_quota(url.strip(), proxy, params.get("webdav_capath"), timeout)
        except (OSError, ValueError):
            quota = None
        if not quota or sum(quota) <= 0:
            continue
        used, avail = quota
        fraction = avail / (used + avail)
        if fraction < minimum:
            yield Detection(
                dedupe_key=f"storage:{name.strip()}",
                subject=(f"{name.strip()} storage {100 * (1 - fraction):.1f}% full: "
                         f"{avail / 1e12:.1f} of {(used + avail) / 1e12:.1f} TB free"),
                body_context=(
                    f"The quota behind {url.strip()} has {avail / 1e12:.1f} TB available of "
                    f"{(used + avail) / 1e12:.1f} TB ({100 * fraction:.1f}%, threshold "
                    f"{100 * minimum:.0f}%). Writes there fail once it is full."),
                extra_data={"rse": name.strip(), "url": url.strip(), "used_bytes": used,
                            "available_bytes": avail, "free_fraction": round(fraction, 4)},
            )

    try:
        quotas = _rucio_quota(params, timeout)
    except (OSError, ValueError, KeyError):
        quotas = []  # the catalog unanswered: no verdict this tick
    for rse, used, limit in quotas:
        fraction = (limit - used) / limit
        if fraction < minimum:
            yield Detection(
                dedupe_key=f"quota:{rse}",
                subject=(f"{rse} quota {100 * used / limit:.0f}% used: "
                         f"{used / 1e12:.1f} of {limit / 1e12:.1f} TB"),
                body_context=(
                    f"Production data at {rse} is {used / 1e12:.1f} TB against the "
                    f"{params.get('rucio_quota_account')} quota of {limit / 1e12:.1f} TB in the "
                    f"catalog at {params.get('rucio_url')}. Writes there fail once the "
                    "space behind it is full; free space or redirect output before then."),
                extra_data={"rse": rse, "used_bytes": used, "limit_bytes": limit,
                            "free_fraction": round(fraction, 4)},
            )
