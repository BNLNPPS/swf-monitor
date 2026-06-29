#!/usr/bin/env python3
"""
Submit one PCS ProdTask to PanDA via the client-API EVGEN path — the
credentialed "doer" behind the prod-ops agent's ``submit_evgen_task``
capability, and usable standalone.

This is the production reproduction of the proven condor-side recipe (eic/
job_submission_condor, spec only): a noInput+noOutput task whose containerized
payload xrootd-streams the EVGEN input from JLab and self-registers RECO to JLab
Rucio. It is the EVGEN counterpart of scripts/submit-prod-task.py (the prun
doer, now sidelined); like it, PCS stays the single source of truth — the
submission spec is *fetched* from the monitor's artifact endpoint
(``/pcs/api/prod-tasks/command/?name=<name>&fmt=evgen``), not rebuilt here.

Flow:
  1. GET the EVGEN spec for ``--task-name`` from the monitor.
  2. Assemble the submission sandbox (the one-row-per-job CSV manifest, the
     ``environment-*.sh`` the payload sources, the in-job dispatcher, and the
     JLab x509 proxy the payload uses to register output).
  3. Run the submission kernel (scripts/evgen_panda_submit.py) in a shell that
     has sourced the panda-client environment, reusing the cached OIDC token
     (never deleting it, which would force an interactive device flow).
  4. Parse ``jediTaskID=<N>`` and POST it to
     ``/pcs/api/prod-tasks/record-submission/`` so the ProdTask records its
     panda_task_id and flips to 'submitted'.
  5. Best-effort: write expected output inventory from the exact submitted spec.

Every failure is surfaced (stderr + non-zero exit); nothing is swallowed. Exit
codes match submit-prod-task.py so the agent handler treats both doers alike:
0 success, 7 submitted-but-unrecorded (idempotent re-record), other non-zero
failure.

Standalone:
    python scripts/submit-evgen-task.py --task-name <ProdTask.name> --proxy <x509>
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

RECORD_ATTEMPTS = 3
RECORD_BACKOFF = 2          # seconds, multiplied by attempt number

DEFAULT_PCLIENT_SETUP = os.path.expanduser("~/pclient/run/setup.sh")
DEFAULT_AUTH_VO = "EIC.production"
SUBMIT_TMP_ROOT = os.path.join(os.environ.get("SWF_TMP_DIR", "/data/swf-tmp"), "submit-evgen")
BG_CONFIG_BASE = "https://eicweb.phy.anl.gov/EIC/campaigns/datasets/-/raw/{dataset_tag}/config_data"

# The submission kernel + the in-job dispatcher live beside this doer.
HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_SCRIPT = os.path.join(HERE, "evgen_panda_submit.py")
DISPATCHER_SCRIPT = os.path.join(HERE, "evgen_job_dispatcher.py")
MANAGE_PY = os.path.join(os.path.dirname(HERE), "src", "manage.py")

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
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(query)}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if owner:
        req.add_header("X-Remote-User", owner)
    if token:
        req.add_header("Authorization", f"Token {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _stage_bg_files(env, sandbox):
    """Copy or fetch BG_FILES into the worker sandbox when background mixing uses it."""
    bg_files = (env.get('BG_FILES') or '').strip()
    if not bg_files:
        return
    bg_base = os.path.basename(bg_files)
    if not bg_base:
        raise RuntimeError(f"invalid BG_FILES value: {bg_files!r}")
    target = os.path.join(sandbox, bg_base)

    if os.path.isfile(bg_files):
        if os.path.abspath(bg_files) != os.path.abspath(target):
            shutil.copy(bg_files, target)
    else:
        dataset_tag = env.get('DATASET_TAG') or os.environ.get('DATASET_TAG', 'main')
        url = (
            f"{BG_CONFIG_BASE.format(dataset_tag=urllib.parse.quote(dataset_tag, safe=''))}/"
            f"{urllib.parse.quote(bg_files, safe='')}"
        )
        _log(f"staging BG_FILES from {url}")
        urllib.request.urlretrieve(url, target)
    env['BG_FILES'] = bg_base


def _assemble_sandbox(spec, proxy_path, root):
    """Build the submission dir: spec.json + a sandbox/ holding the worker-facing
    files (manifest, env, dispatcher, proxy). Returns the submission dir path.

    The kernel and spec live OUTSIDE sandbox/ so they are not shipped to the
    worker; only the four worker files travel in the tarball.
    """
    os.makedirs(root, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix=f"{spec['csvBase']}.", dir=root)
    sandbox = os.path.join(workdir, "sandbox")
    os.makedirs(sandbox)

    csv_base = spec['csvBase']
    # One-row-per-job manifest (file,ext,nevents,ichunk).
    with open(os.path.join(sandbox, f"{csv_base}.csv"), "w") as f:
        f.write("\n".join(spec['csvRows']) + "\n")

    # environment-*.sh — the payload run.sh sources it by glob. The proxy
    # basename rides here; run.sh reads X509_USER_PROXY back.
    proxy_base = os.path.basename(proxy_path)
    env = dict(spec.get('env') or {})
    env['X509_USER_PROXY'] = proxy_base
    _stage_bg_files(env, sandbox)
    with open(os.path.join(sandbox, f"environment-{csv_base}.sh"), "w") as f:
        for k, v in env.items():
            f.write(f'export {k}={v}\n')

    # In-job dispatcher (named to match spec['exec']) and the proxy.
    shutil.copy(DISPATCHER_SCRIPT, os.path.join(sandbox, "evgen_job_dispatcher.py"))
    shutil.copy(proxy_path, os.path.join(sandbox, proxy_base))

    with open(os.path.join(workdir, "spec.json"), "w") as f:
        json.dump(spec, f, indent=2)
    return workdir


def _sync_expected_inventory(task_name, spec_path):
    """Best-effort local DB update for expected file inventory.

    This runs only after record-submission succeeds, so the PCS task has the
    JEDI id needed for expected rows. Failure here must not turn a successful
    submission into a failed submission.
    """
    if not os.path.isfile(MANAGE_PY):
        _log(f"WARNING: cannot sync expected inventory; manage.py not found: {MANAGE_PY}")
        return
    cmd = [
        sys.executable, MANAGE_PY, "sync_epicprod_inventory",
        "--prod-task", task_name,
        "--spec-file", spec_path,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        _log("WARNING: expected inventory sync timed out after 60s")
        return
    for line in (p.stdout or "").splitlines():
        _log(f"  inventory-sync: {line}")
    for line in (p.stderr or "").splitlines():
        _log(f"  inventory-sync: {line}")
    if p.returncode != 0:
        _log(f"WARNING: expected inventory sync failed rc={p.returncode}")


def _record_submission_failure(args, reason):
    if not args.panda_tasks_id or not args.swf_monitor_url:
        return
    try:
        _api_post_json(
            args.swf_monitor_url,
            "/pcs/api/prod-tasks/record-submission-failure/",
            {"name": args.task_name},
            {"panda_tasks_id": args.panda_tasks_id, "reason": reason},
            args.token,
            owner=args.owner,
        )
    except Exception as e:
        _log(f"WARNING: could not record submission failure: {e}")


def main():
    ap = argparse.ArgumentParser(description="Submit a PCS ProdTask to PanDA (client-API EVGEN).")
    ap.add_argument("--task-name", required=True, help="ProdTask.name to submit")
    ap.add_argument("--panda-tasks-id", help="Allocated PCS PandaTasks association id")
    ap.add_argument("--swf-monitor-url",
                    default=os.environ.get("SWF_MONITOR_URL", "").rstrip("/"),
                    help="swf-monitor base URL incl. /swf-monitor app path")
    ap.add_argument("--token",
                    default=os.environ.get("SWFMON_TOKEN") or os.environ.get("SWF_MONITOR_TOKEN", ""),
                    help="DRF token for the record-submission POST")
    ap.add_argument("--owner", default=os.environ.get("SWF_TASK_OWNER", ""),
                    help="ProdTask.created_by; sent as X-Remote-User for the owner-gated record write")
    ap.add_argument("--proxy",
                    default=os.environ.get("EVGEN_X509_PROXY", ""),
                    help="JLab eicprod x509 proxy shipped in the sandbox for output "
                         "registration (EVGEN_X509_PROXY) — the same credential the condor "
                         "template ships. NOT X509_USER_PROXY/longproxy-for-rucio, which is "
                         "the BNL Rucio metadata credential and would not write to JLab.")
    ap.add_argument("--auth-vo", default=DEFAULT_AUTH_VO,
                    help="PANDA_AUTH_VO for the submission (default EIC.production)")
    ap.add_argument("--pclient-setup", default=DEFAULT_PCLIENT_SETUP,
                    help="panda-client environment setup to source")
    ap.add_argument("--timeout", type=int, default=300,
                    help="seconds before the submission run is abandoned")
    args = ap.parse_args()

    if not args.swf_monitor_url:
        _log("ERROR: no --swf-monitor-url / SWF_MONITOR_URL")
        return 2
    if not args.proxy or not os.path.isfile(args.proxy):
        # The payload registers RECO to JLab Rucio with this proxy; without it
        # the job cannot write output. Fail loudly rather than submit a job
        # destined to fail at output.
        _log(f"ERROR: JLab x509 proxy not found (--proxy / EVGEN_X509_PROXY): {args.proxy!r}")
        return 2

    # 1. Fetch the EVGEN spec from PCS (single source of truth).
    spec_query = {"name": args.task_name, "fmt": "evgen"}
    if args.panda_tasks_id:
        spec_query["panda_tasks_id"] = args.panda_tasks_id
    try:
        raw = _api_get(args.swf_monitor_url, "/pcs/api/prod-tasks/command/",
                       spec_query, args.token)
    except Exception as e:
        _log(f"ERROR: could not fetch EVGEN spec for '{args.task_name}': {e}")
        _record_submission_failure(args, f"could not fetch EVGEN spec: {e}")
        return 3
    try:
        spec = json.loads(raw)
    except ValueError:
        _log(f"ERROR: spec endpoint did not return JSON:\n{raw[:500]}")
        _record_submission_failure(args, "spec endpoint did not return JSON")
        return 3
    if not spec.get('outDS') or not spec.get('csvRows'):
        _log(f"ERROR: incomplete EVGEN spec for '{args.task_name}': {raw[:500]}")
        _record_submission_failure(args, "incomplete EVGEN spec")
        return 3
    if args.owner and not spec.get('userName'):
        spec['userName'] = args.owner
    _log(f"EVGEN spec for {args.task_name}: outDS={spec['outDS']} "
         f"nJobs={spec.get('nJobs')} skipScout={spec.get('skipScout')}")

    # 2. Assemble the sandbox.
    try:
        workdir = _assemble_sandbox(spec, args.proxy, SUBMIT_TMP_ROOT)
    except Exception as e:
        _log(f"ERROR: could not assemble sandbox: {e}")
        _record_submission_failure(args, f"could not assemble sandbox: {e}")
        return 3
    _log(f"sandbox: {workdir}")

    # 3. Run the kernel under the panda-client environment (cached OIDC token).
    runner = os.path.join(workdir, "run-submit.sh")
    with open(runner, "w") as f:
        f.write("#!/bin/bash\nset -e\n")
        f.write(f"source {args.pclient_setup}\n")
        f.write(f"export PANDA_AUTH_VO={args.auth_vo}\n")
        f.write(f"python3 {KERNEL_SCRIPT} --spec spec.json --workdir sandbox\n")
    try:
        p = subprocess.run(["bash", runner], cwd=workdir, capture_output=True,
                           text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        _log(f"ERROR: submission timed out after {args.timeout}s")
        _record_submission_failure(args, f"submission timed out after {args.timeout}s")
        return 4
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        _log(f"  evgen-submit: {line}")
    if p.returncode != 0:
        _log(f"ERROR: kernel exited rc={p.returncode}")
        _record_submission_failure(args, f"kernel exited rc={p.returncode}")
        return 5

    # 4. Parse the JEDI task ID.
    m = JEDITASKID_RE.search(out)
    if not m:
        _log("ERROR: submission succeeded but no jediTaskID in output")
        _record_submission_failure(args, "submission succeeded but no jediTaskID in output")
        return 6
    jedi_task_id = int(m.group(1))
    _log(f"SUBMITTED {args.task_name} -> jediTaskID={jedi_task_id}")

    # 5. Record the outcome back to PCS (idempotent; retry a transient failure).
    last_err = None
    for attempt in range(1, RECORD_ATTEMPTS + 1):
        try:
            body = {"jedi_task_id": jedi_task_id}
            if args.panda_tasks_id:
                body["panda_tasks_id"] = args.panda_tasks_id
            if spec.get("outDS"):
                body["panda_task_name"] = spec["outDS"]
            _api_post_json(args.swf_monitor_url, "/pcs/api/prod-tasks/record-submission/",
                           {"name": args.task_name}, body,
                           args.token, owner=args.owner)
            _log(f"recorded jediTaskID={jedi_task_id} on ProdTask {args.task_name}")
            _sync_expected_inventory(args.task_name, os.path.join(workdir, "spec.json"))
            print(jedi_task_id)
            return 0
        except Exception as e:
            last_err = e
            _log(f"record-submission POST attempt {attempt}/{RECORD_ATTEMPTS} failed: {e}")
            if attempt < RECORD_ATTEMPTS:
                time.sleep(RECORD_BACKOFF * attempt)
    _log(f"WARNING: submitted (jediTaskID={jedi_task_id}) but record-submission "
         f"POST failed after {RECORD_ATTEMPTS} attempts: {last_err}")
    print(jedi_task_id)
    return 7


if __name__ == "__main__":
    sys.exit(main())
