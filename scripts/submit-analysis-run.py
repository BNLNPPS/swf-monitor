#!/usr/bin/env python3
"""
Submit one stage of a PCS analysis run to PanDA: the credentialed doer
behind the prod-ops agent's ``submit_analysis_run`` capability, and usable
standalone (swf-epicprod docs/ANALYSIS.md).

PCS builds the specification; this fetches it, never rebuilds it:
``GET /analysis/api/v1/runs/<run id>/spec/?stage=map|merge``.

Flow:
  1. GET the stage's specification. A merge is refused (409) until every
     map job has written its partial.
  2. Recheck the destination queue (swf_epicprod.regulation).
  3. Assemble the sandbox: the one-row-per-job manifest, the
     ``environment-<csvBase>.sh`` that payload/analysis_run.sh sources, the
     specification's extra files (a merge's run card and job records), the
     eicprod JLab proxy the job writes its partial or plots with (only
     allow-listed accounts may write unregistered to EIC-XRD-LOG), the
     report channel's settings, and the epicprod payload.
  4. Run the submission kernel (scripts/evgen_panda_submit.py) under the
     panda-client environment with the cached production token.
  5. Parse ``jediTaskID=<N>`` and POST it to
     ``/analysis/api/v1/runs/<run id>/record-submission/``.

Exit codes match submit-evgen-task.py, so the agent treats the doers alike:
0 submitted and recorded (prints the jediTaskID), 7 submitted but not
recorded (prints the jediTaskID; recording is idempotent), 2 refused
before anything was built, 3 sandbox, 4 timeout, 5 kernel failed, 6 no
jediTaskID in the kernel's output.

Standalone:
    python scripts/submit-analysis-run.py --run-card-id <id> --stage map --proxy <x509>
    python scripts/submit-analysis-run.py --run-card-id <id> --stage map --label test --dry-run
"""
import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name):
    """A sibling script as a module (they have no package)."""
    spec = importlib.util.spec_from_file_location(
        name.replace('-', '_').replace('.py', ''), os.path.join(HERE, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The EVGEN doer's HTTP, payload and report-channel helpers, shared as is.
evgen = _load_sibling("submit-evgen-task.py")

STAGES = ("map", "merge")
RECORD_ATTEMPTS = 3
RECORD_BACKOFF = 2          # seconds, multiplied by attempt number
SUBMIT_TMP_ROOT = os.path.join(os.environ.get("SWF_TMP_DIR", "/data/swf-tmp"), "submit-analysis")
PROXY_SANDBOX_NAME = "eicprod-proxy"
PAYLOAD_ENTRY = "analysis_run.sh"
# EVGEN generator sources in the payload that an analysis job never builds;
# the patched rucio-clients tarball stays, rucio_auth.sh installs it.
PAYLOAD_SKIP = ("__pycache__", "*.pyc", "afterburner-cpp.tgz", "djangoh-*.tgz", "mesonsf-*.tgz")


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _run_path(run_card_id, leaf):
    return f"/analysis/api/v1/runs/{run_card_id}/{leaf}/"


def fetch_spec(args):
    """The stage's specification from PCS; a 409 (merge not ready) is a
    refusal, with PCS's reason."""
    try:
        raw = evgen._api_get(args.swf_monitor_url, _run_path(args.run_card_id, "spec"),
                             {"stage": args.stage}, args.token)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"spec refused (HTTP {e.code}): {detail}") from e
    spec = json.loads(raw)
    if spec.get("kind") != "analysis" or spec.get("stage") != args.stage:
        raise RuntimeError(f"not an analysis {args.stage} specification: "
                           f"kind={spec.get('kind')} stage={spec.get('stage')}")
    if spec.get("runCardId") != args.run_card_id:
        raise RuntimeError(f"specification is for run {spec.get('runCardId')}, "
                           f"not {args.run_card_id}")
    return spec


def env_lines(env):
    """``export`` lines, each value quoted for the shell: ANALYSIS_PARAMS
    holds spaces. The proxy line is written separately, unquoted, because
    ${PWD} must expand in the job's directory."""
    return [f"export {k}={shlex.quote(str(v))}\n" for k, v in env.items()]


def assemble_sandbox(spec, proxy_path, root, payload_dir=None, reporting=None):
    """The submission dir: spec.json beside a sandbox/ holding what the
    job gets. Returns the submission dir."""
    os.makedirs(root, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix=f"{spec['csvBase']}.", dir=root)
    sandbox = os.path.join(workdir, "sandbox")
    os.makedirs(sandbox)
    base = spec["csvBase"]
    with open(os.path.join(sandbox, f"{base}.csv"), "w") as f:
        f.write("\n".join(spec["csvRows"]) + "\n")
    env = dict(spec.get("env") or {})
    env.update(reporting or {})
    with open(os.path.join(sandbox, f"environment-{base}.sh"), "w") as f:
        f.writelines(env_lines(env))
        if proxy_path:
            f.write(f'export X509_USER_PROXY="${{PWD}}/{PROXY_SANDBOX_NAME}"\n')
    for name, text in (spec.get("sandboxFiles") or {}).items():
        if os.path.basename(name) != name or name.startswith("."):
            raise ValueError(f"sandbox file name {name!r} is not a plain file name")
        with open(os.path.join(sandbox, name), "w") as f:
            f.write(text)
    if proxy_path:
        dest = os.path.join(sandbox, PROXY_SANDBOX_NAME)
        shutil.copy(proxy_path, dest)
        os.chmod(dest, 0o600)
    payload_dir = payload_dir or evgen._payload_dir()
    if not os.path.isfile(os.path.join(payload_dir, PAYLOAD_ENTRY)):
        raise RuntimeError(f"{PAYLOAD_ENTRY} not in the payload at {payload_dir}: "
                           "the deployed swf-epicprod has no analysis payload")
    shutil.copytree(payload_dir, os.path.join(sandbox, "payload"),
                    ignore=shutil.ignore_patterns(*PAYLOAD_SKIP))
    spec["payload_version"] = evgen._payload_version(payload_dir)
    with open(os.path.join(workdir, "spec.json"), "w") as f:
        json.dump(spec, f, indent=2)
    return workdir


def record(args, spec, jedi_task_id):
    """POST the task back to the run; True when recorded."""
    body = {"stage": args.stage, "jedi_task_id": jedi_task_id,
            "panda_task_name": spec.get("outDS", ""),
            "payload_version": spec.get("payload_version", "")}
    last = None
    for attempt in range(1, RECORD_ATTEMPTS + 1):
        try:
            evgen._api_post_json(args.swf_monitor_url,
                                 _run_path(args.run_card_id, "record-submission"),
                                 {}, body, args.token, owner=args.owner)
            return True
        except Exception as e:                                    # noqa: BLE001
            last = e
            _log(f"record-submission attempt {attempt}/{RECORD_ATTEMPTS} failed: {e}")
            if attempt < RECORD_ATTEMPTS:
                time.sleep(RECORD_BACKOFF * attempt)
    _log(f"WARNING: submitted (jediTaskID={jedi_task_id}) but not recorded: {last}")
    return False


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Submit one stage of a PCS analysis run to PanDA.")
    ap.add_argument("--run-card-id", required=True, help="the run's run_card_id")
    ap.add_argument("--stage", required=True, choices=STAGES)
    ap.add_argument("--swf-monitor-url",
                    default=os.environ.get("SWF_MONITOR_URL", "").rstrip("/"),
                    help="swf-monitor base URL incl. /swf-monitor app path")
    ap.add_argument("--token",
                    default=os.environ.get("SWFMON_TOKEN") or os.environ.get("SWF_MONITOR_TOKEN", ""),
                    help="DRF token for the spec GET and the record-submission POST")
    ap.add_argument("--owner", default=os.environ.get("SWF_TASK_OWNER", ""),
                    help="sent as X-Remote-User on the record write")
    ap.add_argument("--proxy", default=os.environ.get("EVGEN_X509_PROXY", ""),
                    help="JLab eicprod x509 proxy the job writes to EIC-XRD-LOG with "
                         "(EVGEN_X509_PROXY, as the EVGEN doer ships)")
    ap.add_argument("--label", default="", choices=["", "user", "test"],
                    help="prodSourceLabel instead of the specification's (user); "
                         "test for a canary")
    ap.add_argument("--auth-vo", default=evgen.DEFAULT_AUTH_VO)
    ap.add_argument("--pclient-setup", default=evgen.DEFAULT_PCLIENT_SETUP)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true",
                    help="build the sandbox and print its path; submit nothing")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.swf_monitor_url:
        _log("ERROR: no --swf-monitor-url / SWF_MONITOR_URL")
        return 2
    try:
        spec = fetch_spec(args)
    except Exception as e:                                        # noqa: BLE001
        _log(f"ERROR: {e}")
        return 2
    if args.label:
        spec["prodSourceLabel"] = args.label
    _log(f"analysis {args.stage} {spec['outDS']}: {spec.get('nJobs')} job(s) on "
         f"{spec.get('site')}, label {spec.get('prodSourceLabel')}, gshare {spec.get('gshare')}")
    try:
        from swf_epicprod.regulation import require_task_destination
        require_task_destination(spec.get("site"))
    except ImportError:
        if not args.dry_run:
            _log("ERROR: swf_epicprod is not installed; cannot check the destination")
            return 2
    except Exception as e:                                        # noqa: BLE001
        _log(f"ERROR: {e}")
        return 2
    if not args.proxy and not args.dry_run:
        _log("ERROR: no --proxy / EVGEN_X509_PROXY: the job could not write its output")
        return 2
    try:
        workdir = assemble_sandbox(spec, args.proxy, SUBMIT_TMP_ROOT,
                                   reporting=evgen._reporting_env())
    except Exception as e:                                        # noqa: BLE001
        _log(f"ERROR: could not assemble sandbox: {e}")
        return 3
    _log(f"sandbox: {workdir} (payload {spec['payload_version'] or 'no VERSION'})")
    if args.dry_run:
        print(workdir)
        return 0

    runner = os.path.join(workdir, "run-submit.sh")
    with open(runner, "w") as f:
        f.write("#!/bin/bash\nset -e\n")
        f.write(f"source {shlex.quote(args.pclient_setup)}\n")
        f.write(f"export PANDA_AUTH_VO={shlex.quote(args.auth_vo)}\n")
        f.write(f"python3 {shlex.quote(evgen.KERNEL_SCRIPT)} --spec spec.json --workdir sandbox\n")
    try:
        p = subprocess.run(["bash", runner], cwd=workdir, capture_output=True,
                           text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        _log(f"ERROR: submission timed out after {args.timeout}s")
        return 4
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        _log(f"  analysis-submit: {line}")
    if p.returncode != 0:
        _log(f"ERROR: kernel exited rc={p.returncode}")
        return 5
    m = evgen.JEDITASKID_RE.search(out)
    if not m:
        _log("ERROR: submission succeeded but no jediTaskID in output")
        return 6
    jedi_task_id = int(m.group(1))
    _log(f"SUBMITTED analysis {args.stage} {args.run_card_id[:12]} -> jediTaskID={jedi_task_id}")
    print(jedi_task_id)
    return 0 if record(args, spec, jedi_task_id) else 7


if __name__ == "__main__":
    sys.exit(main())
