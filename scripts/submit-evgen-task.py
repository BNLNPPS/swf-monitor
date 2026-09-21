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
  2. Create the output datasets in JLab Rucio, each with its replication
     rule and metadata, so the jobs register files into datasets that
     exist (swf-epicprod docs/RUCIO_REGISTRATION_CONTRACT.md § 2); the
     created DIDs go on the submission record.
  3. Assemble the submission sandbox (the one-row-per-job CSV manifest, the
     ``environment-*.sh`` the payload sources, the in-job dispatcher, and the
     JLab x509 proxy the payload uses to register output).
  4. Run the submission kernel (scripts/evgen_panda_submit.py) in a shell that
     has sourced the panda-client environment, reusing the cached OIDC token
     (never deleting it, which would force an interactive device flow).
  5. Parse ``jediTaskID=<N>`` and POST it to
     ``/pcs/api/prod-tasks/record-submission/`` so the ProdTask records its
     panda_task_id and flips to 'submitted'.
  6. Best-effort: write expected output inventory from the exact submitted spec.

Every failure is surfaced (stderr + non-zero exit); nothing is swallowed. Exit
codes match submit-prod-task.py so the agent handler treats both doers alike:
0 success, 7 submitted-but-unrecorded (idempotent re-record), 8 output
datasets not created (nothing submitted), other non-zero failure.

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


def _payload_dir():
    """The epicprod payload as installed in this interpreter's swf_epicprod
    package (frozen into the deployed venv at deploy), shipped whole in the
    sandbox as payload/ (swf-epicprod docs/EPICPROD_PAYLOAD.md). Located
    without importing the package, which needs no Django here."""
    import importlib.util
    spec = importlib.util.find_spec("swf_epicprod")
    if spec is None or not spec.origin:
        raise RuntimeError("swf_epicprod is not installed in this interpreter")
    path = os.path.join(os.path.dirname(spec.origin), "payload")
    if not os.path.isfile(os.path.join(path, "run.sh")):
        raise RuntimeError(f"epicprod payload not found at {path}")
    return path


def _payload_version(payload_dir):
    try:
        with open(os.path.join(payload_dir, "VERSION")) as f:
            return f.readline().strip()
    except OSError:
        return ""

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


REPORTING_ENV_FILE = os.environ.get(
    "JOB_REPORTING_ENV", os.path.expanduser("~/.epic-job-reporter.env"))
# Queues whose log stage-out is an object store outside the Rucio catalog
# (swf-epicprod docs/NPPS0_TEST_QUEUE.md): the pilot puts the log in S3, the
# server then tries to register it at the queue's Rucio log storage and the
# adder fails the job on that, whatever the payload did (DDM 200, 2026-09-11,
# every reproduction on npps0). A task sent there carries no log dataset.
OBJECT_STORE_LOG_QUEUES = {"BNL_NPPS_GPU"}
REPORTING_KEYS = ("REPORT_OUT_BUCKET", "REPORT_OUT_REGION",
                  "REPORT_OUT_ACCESS_KEY_ID", "REPORT_OUT_SECRET_ACCESS_KEY",
                  "REPORT_OUT_ENDPOINT")


def _reporting_env():
    """The settings a job needs to send its report out of the worker
    (swf-epicprod docs/JOB_REPORTING.md).

    They are read from a file held by the operating account, never from
    the task specification, so the web tier and the specification carry
    no credential; this doer is the only place they enter a sandbox. A
    missing file is not an error: jobs then run without this channel,
    which is how it is turned off.
    """
    values = {}
    try:
        with open(REPORTING_ENV_FILE) as f:
            for line in f:
                line = line.strip().removeprefix("export ").strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in REPORTING_KEYS:
                    values[key] = value.strip().strip('"').strip("'")
    except OSError as e:
        _log(f"job reporting not configured ({REPORTING_ENV_FILE}): {e}")
        return {}
    if "REPORT_OUT_BUCKET" not in values:
        _log(f"job reporting not configured: no bucket in {REPORTING_ENV_FILE}")
        return {}
    _log(f"job reporting to {values['REPORT_OUT_BUCKET']} "
         f"({len(values)} settings from {REPORTING_ENV_FILE})")
    return values


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
    env.update(_reporting_env())
    with open(os.path.join(sandbox, f"environment-{csv_base}.sh"), "w") as f:
        for k, v in env.items():
            f.write(f'export {k}={v}\n')

    # In-job dispatcher (named to match spec['exec']) and the proxy.
    shutil.copy(DISPATCHER_SCRIPT, os.path.join(sandbox, "evgen_job_dispatcher.py"))
    shutil.copy(proxy_path, os.path.join(sandbox, proxy_base))

    # The epicprod payload, whole, from the frozen package: the dispatcher
    # runs sandbox/payload/run.sh. Its version rides on the submission
    # record.
    payload_dir = _payload_dir()
    shutil.copytree(payload_dir, os.path.join(sandbox, "payload"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    spec['payload_version'] = _payload_version(payload_dir)
    _log(f"payload {spec['payload_version'] or '(no VERSION)'} from {payload_dir}")

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


def _load_sibling(name):
    """A sibling script as a module (they have no package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        name.replace('-', '_').replace('.py', ''), os.path.join(HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _output_lifetime_s(spec, args):
    """The lifetime of a run that has one: a canary's (the dispatcher's
    payload-canary setting) or a trial's; None for production."""
    if args.canary_stamp:
        return int(_load_sibling("evgen_job_dispatcher.py").CANARY_LIFETIME_S)
    if args.trial or spec.get('trial'):
        return int(args.trial_lifetime_days) * 86400
    return None


def _outputs_to_create(spec, args):
    """The datasets this submission's jobs register into. A production or
    trial submission carries them on its spec (PCS composes them with
    their metadata, swf_epicprod.output_datasets); a payload canary
    writes everything into the one flat dataset the dispatcher's
    payload-canary mode names, with no metadata, as the payload does."""
    if args.canary_stamp:
        d = _load_sibling("evgen_job_dispatcher.py")
        return [{'level': 'CANARY', 'dataset': f"/{d.CANARY_DATASET_ROOT}/{args.canary_stamp}",
                 'metadata': None}]
    return list(spec.get('outputs') or [])


def _precreate_outputs(spec, args):
    """Create the submission's output datasets in JLab Rucio before the
    task goes to PanDA (swf-epicprod docs/RUCIO_REGISTRATION_CONTRACT.md
    § 2): each with one replication rule as the payload's upload made
    it (the production account, one copy, the output RSE, DATASET
    grouping, the run's lifetime where it has one) and its metadata. An
    existing dataset is accepted: a missing rule is added, absent
    metadata set, and metadata that differs from the task's stops the
    submission, since the same output name would hold different
    physics. Returns the record of what was done, one entry per
    dataset, for the submission record; raises RuntimeError with the
    reason when the submission must not proceed."""
    outputs = _outputs_to_create(spec, args)
    if not outputs:
        raise RuntimeError("the spec names no output dataset to create")
    from rucio.common.exception import DataIdentifierNotFound
    from swf_epicprod.payload.register_to_rucio import validate_metadata
    evgen = _load_sibling("register-evgen-rucio.py")
    scope = evgen.RUCIO_SCOPE
    rse = str((spec.get('env') or {}).get('OUT_RSE') or 'EIC-XRD')
    lifetime = _output_lifetime_s(spec, args)
    try:
        client = evgen.rucio_client(args.proxy)
    except Exception as e:                                    # noqa: BLE001
        raise RuntimeError(f"JLab Rucio not reachable for the output datasets: {e}")
    record = []
    for out in outputs:
        name = str(out['dataset'])
        meta = out.get('metadata') or None
        if meta:
            validate_metadata(meta)
        rule = {'account': client.account, 'copies': 1, 'rse_expression': rse,
                'grouping': 'DATASET', 'lifetime': lifetime}
        entry = {'dataset': f"{scope}:{name}", 'level': out.get('level'), 'rse': rse}
        try:
            # Existence is read first: the JLab server applies the metadata
            # of an add_dataset call to a dataset that already exists before
            # it answers that it exists, so creation is attempted only on a
            # dataset that is not there.
            try:
                existing = client.get_metadata(scope, name, plugin='ALL')
            except DataIdentifierNotFound:
                existing = None
            if existing is None:
                client.add_dataset(scope=scope, name=name, meta=meta, rules=[rule], lifetime=lifetime)
                entry.update(created=True, rule='added', metadata='set' if meta else 'none')
                _log(f"output dataset {entry['dataset']}: created, rule added, "
                     f"metadata {entry['metadata']}" + (f", lifetime {lifetime}s" if lifetime else ""))
                record.append(entry)
                continue
            entry['created'] = False
            rules = [r for r in client.list_did_rules(scope, name)
                     if r.get('rse_expression') == rse]
            if rules:
                entry['rule'] = 'present'
            else:
                client.add_replication_rule([{'scope': scope, 'name': name}], copies=1,
                                            rse_expression=rse, lifetime=lifetime,
                                            grouping='DATASET')
                entry['rule'] = 'added'
            if meta:
                absent = [k for k in meta if existing.get(k) is None]
                differ = {k: (existing.get(k), v) for k, v in meta.items()
                          if existing.get(k) is not None and existing.get(k) != v}
                if differ:
                    raise RuntimeError(
                        f"output dataset {scope}:{name} exists with different metadata "
                        f"than this task declares: " +
                        ", ".join(f"{k}: dataset {a!r}, task {b!r}" for k, (a, b) in differ.items()))
                for k in absent:
                    client.set_metadata(scope, name, k, meta[k])
                entry['metadata'] = f"filled:{','.join(absent)}" if absent else 'present'
            else:
                entry['metadata'] = 'none'
        except RuntimeError:
            raise
        except Exception as e:                                # noqa: BLE001
            raise RuntimeError(f"could not create output dataset {scope}:{name}: {e}")
        _log(f"output dataset {entry['dataset']}: "
             f"{'created' if entry['created'] else 'exists'}, rule {entry['rule']}, "
             f"metadata {entry['metadata']}" + (f", lifetime {lifetime}s" if lifetime else ""))
        record.append(entry)
    return record


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
    ap.add_argument("--residual", action="store_true",
                    help="residual .tryN: the spec covers only manifest rows "
                         "with no registered RECO output "
                         "(JEDI_INTEGRATION.md § Residual rerun)")
    ap.add_argument("--canary-stamp", default="",
                    help="payload canary: submit manifest row 1 alone under the "
                         "canary account to --canary-queue, outputs to the "
                         "expiring dataset epic:/TEST/canary/<stamp>; nothing "
                         "is recorded on the PCS task (site-canary "
                         "IMPLEMENTATION.md, Payload canaries)")
    ap.add_argument("--canary-queue", default="",
                    help="the PanDA queue a payload canary is sent to")
    ap.add_argument("--canary-row", type=int, default=0,
                    help="payload canary: this manifest row (1-based) of the "
                         "task's spec instead of row 1")
    ap.add_argument("--canary-row-text", default="",
                    help="payload canary: this exact manifest row "
                         "(file,ext,nevents,ichunk) instead of one of the "
                         "spec's, the crashed job's row of a reproduction "
                         "(swf-epicprod SEGFAULT_DIAGNOSIS.md, Reproduction)")
    ap.add_argument("--canary-defer-datasets", action="store_true",
                    default=os.environ.get("CANARY_DEFER_DATASETS", "") == "1",
                    help="payload canary only: submit without creating the "
                         "expiring /TEST output dataset first, so the payload's "
                         "own handling of a catalog that does not answer can be "
                         "exercised during a JLab Rucio outage (the file is "
                         "preserved at the RSE either way; the registration is "
                         "owed). Never for production: a production task's "
                         "datasets exist at submission (RUCIO_REGISTRATION_"
                         "CONTRACT.md § 2). Also CANARY_DEFER_DATASETS=1")
    ap.add_argument("--canary-container", default="",
                    help="payload canary: run this container image instead "
                         "of the configuration's (a reproduction runs the "
                         "image the crashed task ran)")
    ap.add_argument("--canary-mem-limit-mb", type=int, default=0,
                    help="payload canary: an address-space limit (RLIMIT_AS, "
                         "MB) the dispatcher puts on the payload, so a "
                         "reference run matches the production queue's "
                         "memory")
    ap.add_argument("--es-input-dataset", default="",
                    help="payload canary as an Event Service job (swf-epicprod "
                         "NODE_EVENT_DISPATCHER.md): this PanDA input dataset "
                         "(the row's EVGEN file, in the PanDA catalog) is what "
                         "JEDI makes ranges over; the dispatcher's es mode "
                         "takes the ranges from the pilot's channel and runs "
                         "each through the payload in the task's image")
    ap.add_argument("--es-events-per-range", type=int, default=0,
                    help="Event Service canary: events per range "
                         "(nEventsPerWorker); with --es-input-dataset")
    ap.add_argument("--es-events", type=int, default=0,
                    help="Event Service canary: events of the input file the "
                         "job covers (nEventsPerJob and nEventsPerInputFile); "
                         "with --es-input-dataset")
    ap.add_argument("--canary-out-rse", default="",
                    help="payload canary: register its outputs at this RSE "
                         "instead of the configuration's (a test at JLab's "
                         "EIC-XRD while the production RSE is down, or a test "
                         "RSE); the canary dataset is created there too")
    ap.add_argument("--es-direct-input", action="store_true",
                    help="Event Service canary: the pilot hands the input as a "
                         "TURL instead of copying it (its --accessmode=direct, "
                         "read as a substring of the job parameters, so it rides "
                         "inside the exec where runGen never sees it), and "
                         "runGen takes the input as given (--givenPFN); for a "
                         "queue that cannot reach the input's storage (npps0)")
    ap.add_argument("--es-fine-grained", action="store_true",
                    help="Event Service canary as fine-grained processing "
                         "(the server's no-merge flavor): every event a "
                         "range, finished ranges counted at the job's end, "
                         "the rest back to the file; --es-events-per-range "
                         "is then the harness's unit, consecutive events "
                         "run as one chunk")
    ap.add_argument("--es-max-attempt", type=int, default=1,
                    help="Event Service canary: job attempts over the input "
                         "file (default 1, a canary's); 2 or more lets a job "
                         "that ended with units untaken (the deadline drain) "
                         "be followed by the next job over what is left")
    ap.add_argument("--es-slots", type=int, default=1,
                    help="Event Service canary: the node harness's slots, one "
                         "resident EICrecon and one range at a time each; "
                         "also the job's core count (default 1)")
    ap.add_argument("--es-deadline-s", type=int, default=0,
                    help="Event Service canary: seconds of wall after which the "
                         "harness takes no further range and drains (0 = none)")
    ap.add_argument("--es-margin-s", type=int, default=1800,
                    help="Event Service canary: the margin before the deadline "
                         "at which taking ranges stops (default 1800)")
    ap.add_argument("--trial", action="store_true",
                    help="trial run (docs/PCS.md, Trials): the composed "
                         "configuration submitted small and for real — one "
                         "job, --trial-events events, outputs under "
                         "epic:/TEST/<root> in the production layout with a "
                         "lifetime. Unlike a canary it keeps the production "
                         "processing type and owner: a trial is a production "
                         "task in everything but scale and destination.")
    ap.add_argument("--trial-events", type=int, default=100,
                    help="events per job in a trial run (default 100)")
    ap.add_argument("--trial-root", default="TEST/trial",
                    help="Rucio root for trial outputs (default TEST/trial)")
    ap.add_argument("--trial-lifetime-days", type=int, default=14,
                    help="lifetime on everything a trial registers")
    ap.add_argument("--trial-queue", default="",
                    help="the PanDA queue a trial is sent to")
    args = ap.parse_args()
    if bool(args.canary_stamp) != bool(args.canary_queue):
        _log("ERROR: --canary-stamp and --canary-queue go together")
        return 2
    if args.canary_defer_datasets and not args.canary_stamp:
        _log("ERROR: --canary-defer-datasets is for payload canaries only")
        return 2

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
    if args.residual:
        spec_query["residual"] = "1"
    if args.canary_row_text:
        # A reproduction brings its row: the spec builder then needs no
        # EVGEN input resolution and no per-job count.
        spec_query["row"] = args.canary_row_text.strip()
    if args.canary_container:
        spec_query["container"] = args.canary_container.strip()
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
    if args.canary_stamp:
        # Payload canary: the configuration's first manifest row through the
        # production payload, as a canary task (canary user, processing type
        # canary, one job, one attempt) on the named queue, its outputs in
        # the expiring dataset the dispatcher's payload-canary mode names.
        qtag = args.canary_queue.lower()
        rows = list(spec['csvRows'])
        if args.canary_row_text:
            # A reproduction runs the crashed job's own row: the sandbox
            # manifest holds that row alone, and the dispatcher's row 1
            # is it.
            spec['csvRows'] = [args.canary_row_text.strip()]
        elif args.canary_row:
            if args.canary_row < 1 or args.canary_row > len(rows):
                _log(f"ERROR: --canary-row {args.canary_row} is outside the "
                     f"spec's {len(rows)} rows")
                return 2
            spec['csvRows'] = [rows[args.canary_row - 1]]
        else:
            spec['csvRows'] = rows[:1]
        spec['outDS'] = f"group.EIC.canary.{qtag}.{args.canary_stamp}"
        # Settings ride as an environment prefix on the dispatcher command,
        # as a trial's do.
        prefix = (f"CANARY_MEM_LIMIT_MB={int(args.canary_mem_limit_mb)} "
                  if args.canary_mem_limit_mb else "")
        spec['exec'] = (f"{prefix}python3 evgen_job_dispatcher.py payload-canary "
                        f"{spec['csvBase']} {args.canary_stamp}")
        spec.update(site=args.canary_queue, processingType='canary',
                    prodSourceLabel='test', userName='canary', nJobs=1,
                    maxAttempt=1, skipScout=True)
        if args.canary_out_rse:
            spec['env'] = dict(spec.get('env') or {}, OUT_RSE=args.canary_out_rse)
            _log(f"canary outputs to {args.canary_out_rse}")
        if args.es_input_dataset:
            # An Event Service canary: JEDI makes ranges of
            # --es-events-per-range over the input dataset's file; the
            # pilot's generic executor hands them to the dispatcher's es
            # mode, which names the channel on its command line (the
            # token the pilot matches to export it) and runs each range
            # through the payload in the task's image, outside the
            # pilot's own container start (evgen_job_dispatcher.py).
            if (args.es_events_per_range < 1 and not args.es_fine_grained) or args.es_events < 1:
                _log("ERROR: --es-input-dataset needs --es-events-per-range "
                     "(or --es-fine-grained) and --es-events")
                return 2
            deadline = (f"ES_DEADLINE_S={int(args.es_deadline_s)} ES_MARGIN_S={int(args.es_margin_s)} "
                        if args.es_deadline_s else "")
            # A fine-grained task's ranges are single events; the harness
            # runs --es-events-per-range of them as one unit (the loss
            # quantum, the chunk that names the outputs).
            per_unit = (f"ES_EVENTS_PER_UNIT={int(args.es_events_per_range)} "
                        if args.es_fine_grained and args.es_events_per_range > 0 else "")
            spec['exec'] = (f"ES_PAYLOAD_IMAGE={spec.get('containerImage', '')} "
                            f"ES_SLOTS={int(args.es_slots)} {deadline}{per_unit}"
                            f"python3 evgen_job_dispatcher.py es "
                            f"{spec['csvBase']} {args.canary_stamp} "
                            f"$PILOT_EVENTRANGECHANNEL"
                            + (" --accessmode=direct" if args.es_direct_input else ""))
            spec['nCore'] = int(args.es_slots)
            if args.es_max_attempt > 1:
                spec['maxAttempt'] = int(args.es_max_attempt)
            if args.es_direct_input:
                # runGen takes the input as given (the kernel's --givenPFN)
                spec['directInput'] = True
            spec.update(inputDataset=args.es_input_dataset, nFilesPerJob=1,
                        nEventsPerInputFile=int(args.es_events),
                        nEventsPerJob=int(args.es_events),
                        nEventsPerWorker=int(args.es_events_per_range),
                        fineGrainedProc=bool(args.es_fine_grained),
                        # The es_events storage's record reaches the pilot
                        # from the queue's published ddmendpoints.json
                        # (perlmutter/<queue>/, STORAGEDATA_SERVER_URL in
                        # the launcher), not from the job: the pilot's
                        # executor resolves the storage id on the
                        # pilot-wide information service, which a job-level
                        # record does not reach (job 3494150).
                        # No log dataset: the pilot makes an event-service
                        # job's log tarball from the wrong directory and the
                        # missing file fails the job after every range was
                        # reported (3494150, pilot error 1165), which loses
                        # the ranges; the worker record on the portal is the
                        # log. The npps0 round trip ran the same way.
                        noLog=True)
            _log(f"event service canary: {args.es_events} events of "
                 f"{args.es_input_dataset} in ranges of "
                 + (f"1 (fine-grained), units of {args.es_events_per_range or 1}"
                    if args.es_fine_grained else str(args.es_events_per_range)))
        _log(f"payload canary {spec['outDS']} on {args.canary_queue}: "
             f"row {spec['csvRows'][0]}"
             + (f", RLIMIT_AS {args.canary_mem_limit_mb} MB"
                if args.canary_mem_limit_mb else ""))
    elif args.trial or spec.get('trial'):
        # The spec carries the trial's own settings when the task is a
        # trial, so submitting one needs no flag; --trial with explicit
        # options remains for a submission driven from the command line.
        t = spec.get('trial') or {}
        if t:
            args.trial_events = int(t.get('events') or args.trial_events)
            args.trial_root = str(t.get('outputRoot') or args.trial_root)
            args.trial_lifetime_days = int(
                t.get('lifetimeDays') or args.trial_lifetime_days)
            args.trial_queue = args.trial_queue or str(t.get('site') or '')
        # Trial: the composed configuration run small and for real. One
        # manifest row, a hundred events of it, outputs under
        # epic:/TEST/<root> in the production layout with a lifetime —
        # the settings ride as an environment prefix on the dispatcher
        # command, which passes them through to the payload. The
        # processing type and owner stay production's, because that is
        # what a trial proves; only PCS knows it is a trial, from the
        # suffix in its name (docs/PCS.md, Trials).
        lifetime_s = int(args.trial_lifetime_days) * 86400
        spec['csvRows'] = list(spec['csvRows'])[:1]
        spec['exec'] = (
            f"TRIAL_OUTPUT_ROOT={args.trial_root} "
            f"TRIAL_LIFETIME_S={lifetime_s} "
            f"TRIAL_EVENTS={int(args.trial_events)} "
            # %RNDM=0 is the token JEDI substitutes when it makes job
            # parameters, and is what the production exec uses; a shell
            # ${SEQNUMBER} is not resolved and job generation crashes.
            f"python3 evgen_job_dispatcher.py %RNDM=0 {spec['csvBase']}"
        )
        spec.update(nJobs=1, maxAttempt=1, skipScout=True)
        if args.trial_queue:
            spec['site'] = args.trial_queue
        _log(f"trial {spec['outDS']} on {spec.get('site') or '(brokered)'}: "
             f"{args.trial_events} events, outputs under "
             f"epic:/{args.trial_root}, lifetime {args.trial_lifetime_days}d")
    if spec.get('site') in OBJECT_STORE_LOG_QUEUES:
        # The kernel drops the log dataset on noLog (evgen_panda_submit.py).
        spec['noLog'] = True
        _log(f"site {spec['site']}: no log dataset (object-store log stage-out)")
    _log(f"EVGEN spec for {args.task_name}: outDS={spec['outDS']} "
         f"nJobs={spec.get('nJobs')} skipScout={spec.get('skipScout')}")

    # 2. The output datasets exist before the task does: created with
    # their rule and metadata under the production account, so the jobs
    # register files into them and carry no dataset-level work
    # (swf-epicprod docs/RUCIO_REGISTRATION_CONTRACT.md § 2).
    if args.canary_defer_datasets:
        # A canary during a catalog outage: its /TEST dataset is not
        # created here, so the submission needs no catalog, and the job
        # meets the outage itself after preserving its output.
        spec['output_datasets'] = [
            {'dataset': f"{_load_sibling('register-evgen-rucio.py').RUCIO_SCOPE}:{o['dataset']}",
             'level': o.get('level'), 'deferred': True,
             'reason': 'canary submitted without dataset creation (--canary-defer-datasets)'}
            for o in _outputs_to_create(spec, args)]
        _log("output datasets deferred: canary submitted without creating them")
    else:
        try:
            spec['output_datasets'] = _precreate_outputs(spec, args)
        except Exception as e:                                    # noqa: BLE001
            _log(f"ERROR: output datasets not created: {e}")
            _record_submission_failure(args, f"output datasets not created: {e}")
            return 8

    # 3. Assemble the sandbox.
    try:
        workdir = _assemble_sandbox(spec, args.proxy, SUBMIT_TMP_ROOT)
    except Exception as e:
        _log(f"ERROR: could not assemble sandbox: {e}")
        _record_submission_failure(args, f"could not assemble sandbox: {e}")
        return 3
    _log(f"sandbox: {workdir}")

    # 4. Run the kernel under the panda-client environment (cached OIDC token).
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

    # 5. Parse the JEDI task ID.
    m = JEDITASKID_RE.search(out)
    if not m:
        _log("ERROR: submission succeeded but no jediTaskID in output")
        _record_submission_failure(args, "submission succeeded but no jediTaskID in output")
        return 6
    jedi_task_id = int(m.group(1))
    _log(f"SUBMITTED {args.task_name} -> jediTaskID={jedi_task_id}")
    if args.canary_stamp:
        # A canary task is the canary's record, not the PCS task's: the
        # caller (canary payload-canary) keeps it on the probe run.
        print(f"jediTaskID={jedi_task_id}")
        return 0

    # 6. Record the outcome back to PCS (idempotent; retry a transient failure).
    last_err = None
    for attempt in range(1, RECORD_ATTEMPTS + 1):
        try:
            body = {"jedi_task_id": jedi_task_id}
            if args.panda_tasks_id:
                body["panda_tasks_id"] = args.panda_tasks_id
            if spec.get("outDS"):
                body["panda_task_name"] = spec["outDS"]
            if spec.get("residual"):
                body["residual"] = spec["residual"]
            if spec.get("payload_version"):
                body["payload_version"] = spec["payload_version"]
            if spec.get("output_datasets"):
                # The datasets created for this submission: the explicit
                # Rucio reference of its outputs, written at submission.
                body["output_datasets"] = spec["output_datasets"]
            if spec.get("csvRows"):
                # The rows this attempt runs, recorded compactly on the
                # PandaTasks row (pcs/manifests.py): what a later residual
                # of this attempt is computed over.
                body["manifest_rows"] = spec["csvRows"]
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
