#!/usr/bin/env python3
"""
Fetch and cache the payload log for one PanDA job.

Resolves the job's Rucio log tarball (`*.log.tgz`) to a replica PFN, copies it
with `xrdcp` using the long-lived proxy, and extracts the log members into the
managed scratch cache so the swf-monitor job view can serve them as text.

Runs as the account that can read the Rucio proxy and run xrdcp (wenauseic) —
the web view only ever reads the world-readable output, never the proxy. The
view/drain-worker invokes this with the log DID it already knows from
`study_job` (scope + lfn + jeditaskid + pandaid).

See docs/EPICPROD_OPS.md.

Config (env, with defaults matching the bots / rucio MCP):
  RUCIO_URL, RUCIO_ACCOUNT, RUCIO_VO, X509_USER_PROXY,
  REQUESTS_CA_BUNDLE (TLS verify), SWF_TMP_DIR
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile

RUCIO_URL = os.environ.get("RUCIO_URL", "https://nprucio01.sdcc.bnl.gov:443")
RUCIO_ACCOUNT = os.environ.get("RUCIO_ACCOUNT", "panda")
RUCIO_VO = os.environ.get("RUCIO_VO", "eic")
X509_PROXY = os.environ.get("X509_USER_PROXY", "/data/wenauseic/longproxy-for-rucio")
CA_BUNDLE = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or True
SWF_TMP_DIR = os.environ.get("SWF_TMP_DIR", "/data/swf-tmp")

# Log members worth caching for the operator-facing view.
KEEP = {"payload.stdout", "payload.stderr", "pilotlog.txt", "pandatracerlog.txt"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg, code=1):
    log(f"ERROR: {msg}")
    sys.exit(code)


def _rucio_token():
    import requests
    headers = {"X-Rucio-Account": RUCIO_ACCOUNT}
    if RUCIO_VO:
        headers["X-Rucio-VO"] = RUCIO_VO
    r = requests.get(f"{RUCIO_URL}/auth/x509", headers=headers,
                     cert=(X509_PROXY, X509_PROXY), verify=CA_BUNDLE, timeout=20)
    r.raise_for_status()
    tok = r.headers.get("X-Rucio-Auth-Token")
    if not tok:
        fail("no X-Rucio-Auth-Token in /auth/x509 response")
    return tok


def resolve_pfn(scope, name):
    import requests
    r = requests.post(
        f"{RUCIO_URL}/replicas/list",
        headers={"X-Rucio-Auth-Token": _rucio_token(),
                 "Content-Type": "application/json",
                 "Accept": "application/x-json-stream"},
        json={"dids": [{"scope": scope, "name": name}]},
        verify=CA_BUNDLE, timeout=60,
    )
    r.raise_for_status()
    pfn, state = None, None
    for line in r.text.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        pfns = rec.get("pfns") or {}
        for url in pfns:                       # prefer the xrootd door
            if url.startswith("root://"):
                pfn = url
                break
        if not pfn and pfns:
            pfn = next(iter(pfns))
        states = rec.get("states") or {}
        state = ",".join(f"{k}={v}" for k, v in states.items())
    if not pfn:
        fail(f"no replica PFN for {scope}:{name}")
    log(f"replica: {pfn}  [{state}]")
    return pfn


def xrdcp(pfn, dest):
    env = dict(os.environ, X509_USER_PROXY=X509_PROXY)
    log(f"xrdcp -> {dest}")
    p = subprocess.run(["xrdcp", "-f", "--nopbar", pfn, dest],
                       env=env, capture_output=True, text=True)
    if p.returncode != 0:
        fail(f"xrdcp failed (rc={p.returncode}): {p.stderr.strip()}")


def extract(tgz, jobdir):
    """Extract the kept members (basename only — no path traversal) into a
    .partial dir, then atomically rename into place. World-readable output."""
    tmp = jobdir + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    kept = []
    with tarfile.open(tgz, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            base = os.path.basename(m.name)
            if base not in KEEP:
                continue
            src = tf.extractfile(m)
            if src is None:
                continue
            out = os.path.join(tmp, base)
            with open(out, "wb") as f:
                shutil.copyfileobj(src, f)
            os.chmod(out, 0o644)
            kept.append(base)
    if not kept:
        shutil.rmtree(tmp, ignore_errors=True)
        fail("tarball contained none of the expected log members")
    os.chmod(tmp, 0o2775)
    shutil.rmtree(jobdir, ignore_errors=True)
    os.rename(tmp, jobdir)
    log(f"cached: {jobdir}  ({', '.join(sorted(kept))})")


def main():
    ap = argparse.ArgumentParser(description="Fetch + cache a PanDA job payload log.")
    ap.add_argument("--scope", required=True)
    ap.add_argument("--lfn", required=True, help="log tarball LFN (…log.tgz)")
    ap.add_argument("--jeditaskid", required=True)
    ap.add_argument("--pandaid", required=True)
    ap.add_argument("--force", action="store_true", help="re-fetch even if cached")
    a = ap.parse_args()

    jobdir = os.path.join(SWF_TMP_DIR, "panda-logs", str(a.jeditaskid), str(a.pandaid))
    if os.path.exists(os.path.join(jobdir, "payload.stdout")) and not a.force:
        log(f"already cached: {jobdir}")
        return
    if not os.path.exists(X509_PROXY):
        fail(f"x509 proxy not found: {X509_PROXY}")

    pfn = resolve_pfn(a.scope, a.lfn)
    dldir = os.path.join(SWF_TMP_DIR, "downloads")
    os.makedirs(dldir, exist_ok=True)
    tgz = os.path.join(dldir, f"{a.lfn}.{a.pandaid}")
    try:
        xrdcp(pfn, tgz)
        os.makedirs(os.path.dirname(jobdir), exist_ok=True)
        extract(tgz, jobdir)
    finally:
        try:
            os.remove(tgz)
        except OSError:
            pass


if __name__ == "__main__":
    main()
