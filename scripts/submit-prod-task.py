#!/usr/bin/env python3
"""
Submit one PCS ProdTask to PanDA — the credentialed "doer" behind the
prod-ops agent's ``submit_task`` capability, and usable standalone.

This is the automated counterpart to the manual production recipe in
docs/EPICPROD_OPS.md: it runs the same ``prun`` an operator runs, but
non-interactively, reusing the cached long-lived OIDC token (it never
deletes ``$PANDA_CONFIG_ROOT/.token``, which would force an interactive
device flow). PCS remains the single source of truth — the prun command
is fetched from the monitor's own artifact endpoint
(``/pcs/api/prod-tasks/command/?name=<name>&fmt=panda``), not rebuilt here.

Flow:
  1. GET the prun command for ``--task-name`` from the monitor.
  2. Run it in a clean sandbox dir under the panda-client environment
     (source the pclient setup, export PANDA_AUTH_VO), non-interactive.
  3. Parse ``jediTaskID=<N>`` from prun's output.
  4. POST the outcome back to ``/pcs/api/prod-tasks/record-submission/``
     so the ProdTask records its panda_task_id and flips to 'submitted'.

Every failure is surfaced (stderr + non-zero exit); nothing is swallowed.

Standalone:
    python scripts/submit-prod-task.py --task-name <ProdTask.name>
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

# Where the cached OIDC token and pclient live; the agent runs as the same
# user, so these resolve identically under systemd.
DEFAULT_PCLIENT_SETUP = os.path.expanduser("~/pclient/run/setup.sh")
DEFAULT_AUTH_VO = "EIC.production"
# Sandbox root on the large /data volume (prun packages the cwd).
SUBMIT_TMP_ROOT = os.path.join(os.environ.get("SWF_TMP_DIR", "/data/swf-tmp"), "submit")

JEDITASKID_RE = re.compile(r"jediTaskID=(\d+)")


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _api_get(base, path, query, token):
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Token {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _api_post_json(base, path, query, body, token, owner=None):
    import json
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # record-submission is owner-gated (IsOwnerOrReadOnly: created_by ==
    # request.user.username). On-host, TunnelAuthentication trusts X-Remote-User
    # on localhost requests, so we authenticate as the task owner without a
    # secret. Off-host, a DRF token for that owner is the fallback.
    if owner:
        req.add_header("X-Remote-User", owner)
    if token:
        req.add_header("Authorization", f"Token {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def main():
    ap = argparse.ArgumentParser(description="Submit a PCS ProdTask to PanDA via prun.")
    ap.add_argument("--task-name", required=True, help="ProdTask.name to submit")
    ap.add_argument("--swf-monitor-url",
                    default=os.environ.get("SWF_MONITOR_URL", "").rstrip("/"),
                    help="swf-monitor base URL incl. /swf-monitor app path")
    ap.add_argument("--token",
                    default=os.environ.get("SWFMON_TOKEN") or os.environ.get("SWF_MONITOR_TOKEN", ""),
                    help="DRF token for the monitor API record-submission POST "
                         "(SWFMON_TOKEN; read-only command GET needs none)")
    ap.add_argument("--owner", default=os.environ.get("SWF_TASK_OWNER", ""),
                    help="ProdTask.created_by (Django username); sent as X-Remote-User "
                         "so the on-host tunnel authenticates the owner-gated record write")
    ap.add_argument("--auth-vo", default=DEFAULT_AUTH_VO,
                    help="PANDA_AUTH_VO for the submission (default EIC.production)")
    ap.add_argument("--pclient-setup", default=DEFAULT_PCLIENT_SETUP,
                    help="panda-client environment setup to source")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds before the prun run is abandoned")
    args = ap.parse_args()

    if not args.swf_monitor_url:
        _log("ERROR: no --swf-monitor-url / SWF_MONITOR_URL")
        return 2

    # 1. Fetch the prun command from PCS (single source of truth).
    try:
        prun_cmd = _api_get(args.swf_monitor_url, "/pcs/api/prod-tasks/command/",
                            {"name": args.task_name, "fmt": "panda"}, args.token).strip()
    except Exception as e:
        _log(f"ERROR: could not fetch prun command for '{args.task_name}': {e}")
        return 3
    if not prun_cmd.startswith("prun"):
        _log(f"ERROR: artifact endpoint did not return a prun command:\n{prun_cmd[:500]}")
        return 3
    _log(f"prun command for {args.task_name}:\n{prun_cmd}")

    # 2. Run prun in a clean sandbox under the pclient environment. The cached
    #    token is reused (never deleted) so this stays non-interactive.
    os.makedirs(SUBMIT_TMP_ROOT, exist_ok=True)
    sandbox = tempfile.mkdtemp(prefix=f"{args.task_name}.", dir=SUBMIT_TMP_ROOT)
    runner = os.path.join(sandbox, "run-prun.sh")
    with open(runner, "w") as f:
        f.write("#!/bin/bash\nset -e\n")
        f.write(f"source {args.pclient_setup}\n")
        f.write(f"export PANDA_AUTH_VO={args.auth_vo}\n")
        f.write(prun_cmd + "\n")
    _log(f"sandbox: {sandbox}")
    try:
        p = subprocess.run(["bash", runner], cwd=sandbox, capture_output=True,
                           text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        _log(f"ERROR: prun timed out after {args.timeout}s")
        return 4
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        _log(f"  prun: {line}")
    if p.returncode != 0:
        _log(f"ERROR: prun exited rc={p.returncode}")
        return 5

    # 3. Parse the JEDI task ID.
    m = JEDITASKID_RE.search(out)
    if not m:
        _log("ERROR: prun succeeded but no jediTaskID in output")
        return 6
    jedi_task_id = int(m.group(1))
    _log(f"SUBMITTED {args.task_name} -> jediTaskID={jedi_task_id}")

    # 4. Record the outcome back to PCS.
    try:
        _api_post_json(args.swf_monitor_url, "/pcs/api/prod-tasks/record-submission/",
                       {"name": args.task_name}, {"jedi_task_id": jedi_task_id},
                       args.token, owner=args.owner)
    except Exception as e:
        # The task IS submitted; only the bookkeeping POST failed. Surface it
        # loudly (operator can re-record) but report the task ID we got.
        _log(f"WARNING: submitted (jediTaskID={jedi_task_id}) but record-submission "
             f"POST failed: {e}")
        return 7
    _log(f"recorded jediTaskID={jedi_task_id} on ProdTask {args.task_name}")
    print(jedi_task_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
