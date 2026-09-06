#!/usr/bin/env python3
"""
In-job per-row dispatcher for the client-API EVGEN production path.

This runs *inside* the PanDA job (in the eic_xl container), shipped in the
submission sandbox by the submit-evgen-task doer. PanDA expands the task's
``%RNDM`` into a per-job ``${SEQNUMBER}`` (1-based); this reads the matching
row of the one sandbox'd CSV and hands it to the ePIC production payload.

It is our owned equivalent of job_submission_condor/scripts/submit_panda.py
(spec only — never run from that repo); the CSV row shape
(``file,ext,nevents,ichunk``) and the payload entry point
(``/opt/campaigns/hepmc3/scripts/run.sh``) are the fixed contract with the
payload. The payload's run.sh sources ``environment*.sh`` from the unpacked
sandbox itself, so no env is set here.

Usage (as PanDA expands it):  evgen_job_dispatcher.py <SEQNUMBER> <csv_base>

Canary probe mode:  evgen_job_dispatcher.py canary [payload_seconds]
The site-canary probe task ships this same dispatcher in its sandbox —
one runner for production and probe jobs — beside the vendored canary
kit/ (package + prmon). The canary branch runs the landing kit as the
payload and ships the landing report both ways: embedded in
jobReport.json, which the pilot lifts into the PanDA job metadata
(written on success and failure alike — site attribute reporting), and
to stdout between CANARY-REPORT markers, collectable from the job log.
"""
import csv
import json
import os
import subprocess
import sys
from itertools import islice

# The payload travels in the sandbox as payload/ (the epicprod payload,
# swf-epicprod swf_epicprod/payload, shipped by the submit doer from the
# frozen release; docs/EPICPROD_PAYLOAD.md). The container supplies the
# software stack only; no script the job runs comes from it.
PAYLOAD_SUBDIR = "payload"

# run.sh's coded exits (its explicit `exit N` failure sites).
EXIT_MSGS = {65: "output validation failed", 78: "Rucio registration failed",
             79: "output name held by a failed earlier attempt; rerun the "
                 "residual as a new try"}


def payload_version(workdir):
    """The first line of the sandbox payload's VERSION file, or ''."""
    try:
        with open(os.path.join(workdir, PAYLOAD_SUBDIR, "VERSION")) as f:
            return f.readline().strip()
    except OSError:
        return ""


def write_job_report(rc, workdir, extra=None):
    """Write jobReport.json for the pilot to ship with the job record.

    The pilot reads this file from the job workdir and sends the whole
    object with the final job update as job metadata; the epic pilot
    plugin additionally stores a nonzero exitCode and exitMsg in the job
    record (exeErrorCode/exeErrorDiag). On failure the message is the
    payload's last ERROR line when one is found in the pilot-captured
    payload output, else the coded-exit description. ``extra`` fields
    merge into the report object — the canary mode ships its landing
    report this way. Never raises and never alters the payload exit
    code.
    """
    if rc == 0:
        msg = "ok"
    else:
        msg = EXIT_MSGS.get(rc, f"payload exited {rc}")
        for name in ("payload.stdout", "payload.stderr"):
            try:
                with open(os.path.join(workdir, name), "rb") as f:
                    lines = (f.read()[-65536:]
                             .decode(errors="replace").splitlines())
                err = [ln.strip() for ln in lines
                       if ln.strip().startswith("ERROR")]
                if err:
                    msg = err[-1]
                    break
            except OSError:
                continue
    body = {"exitCode": rc, "exitMsg": msg[:500]}
    # The payload's own report (payload-report.json, written by the
    # payload's EXIT trap on every run: events requested, simulated and
    # reconstructed, per-stage wall and prmon CPU and memory, output
    # sizes, the registration outcome) rides under ``payload``, and the
    # reconstructed count as ``nEvents`` for the job record
    # (swf-epicprod docs/EPICPROD_PAYLOAD.md, payload reporting).
    payload = read_payload_report(workdir)
    if payload is not None:
        body["payload"] = payload
        reco = (payload.get("events") or {}).get("reconstructed")
        if isinstance(reco, int):
            body["nEvents"] = reco
    if extra:
        body.update(extra)
    try:
        with open(os.path.join(workdir, "jobReport.json"), "w") as f:
            json.dump(body, f)
        print(f"jobReport.json written: exitCode={rc} exitMsg={msg[:500]}"
              + (f" nEvents={body['nEvents']}" if "nEvents" in body else ""))
    except OSError as e:
        print(f"jobReport.json not written: {e}", file=sys.stderr)


def read_payload_report(workdir):
    """The payload report the run left in the work directory, or None;
    an unreadable report is said on stderr, never raised."""
    path = os.path.join(workdir, "payload-report.json")
    if not os.path.exists(path):
        print("no payload report found", file=sys.stderr)
        return None
    try:
        with open(path) as f:
            report = json.load(f)
    except (OSError, ValueError) as e:
        print(f"payload report not readable: {e}", file=sys.stderr)
        return None
    if not isinstance(report, dict):
        print("payload report is not a JSON object", file=sys.stderr)
        return None
    return report


def run_canary(payload_seconds, workdir):
    """Canary probe branch: the landing kit is the payload (site-canary
    PLAN.md increment 8). The sandbox carries kit/ — the vendored canary
    package and the prmon binary — and the landing report is emitted to
    stdout between CANARY-REPORT markers for collection from the job log.
    """
    kit = os.path.join(workdir, "kit")
    env = dict(os.environ)
    env["CANARY_PRMON"] = os.path.join(kit, "prmon")
    env["PYTHONPATH"] = kit + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run(
        [sys.executable, "-m", "canary", "landing",
         "--payload-seconds", str(payload_seconds),
         "-o", "landing-report.json"],
        text=True, env=env)
    landing = None
    report_text = None
    try:
        with open(os.path.join(workdir, "landing-report.json")) as f:
            report_text = f.read()
        landing = json.loads(report_text)
    except (OSError, ValueError) as e:
        print(f"landing report not readable: {e}", file=sys.stderr)
    print("CANARY-REPORT-BEGIN", flush=True)
    if report_text:
        print(report_text, flush=True)
    print("CANARY-REPORT-END", flush=True)
    # A probe that reached this point has done its job: delivering the
    # report. It exits success whatever the landing kit returned, so the
    # pilot ships jobReport.json as job metadata (the server keeps
    # metadata for finished jobs only), and the verdict is read from the
    # report, which carries the kit's own exit code. The stdout markers
    # above are the fallback copy.
    canary = dict(landing) if landing else {}
    canary["kit_exit_code"] = result.returncode
    if landing is None:
        canary["error"] = "landing report not produced"
    write_job_report(0, workdir, extra={"canary": canary})
    return 0


def run_row(n, csv_base, workdir, extra_env=None):
    """Run manifest row ``n`` (1-based) through the sandbox payload.
    Returns (payload exit code, the row) or (1, None) when the row cannot
    be read."""
    # PanDA SEQNUMBER is 1-based; the CSV is 0-indexed. Read only the
    # requested row, never the whole manifest.
    csv_index = n - 1
    with open(f"{csv_base}.csv") as f:
        reader = csv.reader(f)
        row = next(islice(reader, csv_index, csv_index + 1), None)
    if row is None:
        print(f"Error: row {n} not found in {csv_base}.csv", file=sys.stderr)
        return 1, None
    if len(row) < 4:
        print(f"Error: malformed CSV row {n}: {row!r}", file=sys.stderr)
        return 1, None
    file_path, ext, nevents, ichunk = row[0], row[1], row[2], row[3]
    payload_run = os.path.join(workdir, PAYLOAD_SUBDIR, "run.sh")
    print(f"epicprod payload {payload_version(workdir) or '(no VERSION)'}: "
          f"{payload_run}")
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    # The payload prepends the JLab xrootd path to EVGEN/<file>; pass the
    # EVGEN-relative path, extension, event count and chunk index through.
    result = subprocess.run(
        [payload_run, f"EVGEN/{file_path}", ext, nevents, ichunk],
        text=True, env=env,
    )
    return result.returncode, row


# Payload canary (site-canary IMPLEMENTATION.md, Payload canaries): the
# production payload on one manifest row under the canary account, its
# outputs in one flat dataset under epic:/TEST/ that removes itself.
CANARY_DATASET_ROOT = "TEST/canary"
CANARY_LIFETIME_S = 7 * 86400


def _read_stages(path):
    """The payload's stage log as [{stage, status, at, detail}], in order."""
    stages = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.strip().split(" ", 3)
                if len(parts) >= 3:
                    stages.append({"at": parts[0], "stage": parts[1],
                                   "status": parts[2],
                                   "detail": parts[3] if len(parts) > 3 else ""})
    except OSError:
        pass
    return stages


def _payload_output(workdir):
    """The tail of the payload's captured stdout, as text."""
    try:
        with open(os.path.join(workdir, "payload.stdout"), "rb") as f:
            return f.read()[-1048576:].decode(errors="replace")
    except OSError:
        return ""


def _loaded_metadata(text):
    """The last 'Loaded metadata: {...}' block the registration printed,
    parsed, or None."""
    idx = text.rfind("Loaded metadata: {")
    if idx < 0:
        return None
    start = text.index("{", idx)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    return None
    return None


def run_payload_canary(csv_base, stamp, workdir):
    """Payload canary: manifest row 1 through the production payload with
    its outputs directed to epic:/TEST/canary/<stamp>, a seven-day
    lifetime on what it registers, and no log upload. The verdict
    material rides in jobReport.json under ``canary``: the payload exit
    code, the stage log, the events processed, the registration
    metadata, the DIDs registered. Exits 0 whatever the payload did, so
    the pilot ships the report as job metadata (kept for finished jobs
    only); the verdict is read from the report, never from the exit."""
    dataset = f"{CANARY_DATASET_ROOT}/{stamp}"
    stages_log = os.path.join(workdir, "payload-stages.log")
    rc, row = run_row(1, csv_base, workdir, extra_env={
        "CANARY_OUTPUT_DATASET": dataset,
        "CANARY_LIFETIME_S": str(CANARY_LIFETIME_S),
        "PAYLOAD_STAGES_LOG": stages_log,
    })
    stages = _read_stages(stages_log)
    text = _payload_output(workdir)
    requested = None
    if row and str(row[2]).strip().isdigit():
        requested = int(row[2])
    # The events processed are the reconstructed count of the payload's
    # report (write_job_report carries the whole report as well).
    payload = read_payload_report(workdir) or {}
    produced = (payload.get("events") or {}).get("reconstructed")
    canary = {
        "kind": "payload",
        "stamp": stamp,
        "dataset": f"epic:/{dataset}",
        "payload_version": payload_version(workdir),
        "payload_exit_code": rc,
        "manifest_row": row,
        "requested_events": requested,
        "events_processed": produced if isinstance(produced, int) else None,
        "stages": stages,
        "dids": [s["detail"] for s in stages
                 if s["stage"] == "registration" and s["status"] == "ok"
                 and s["detail"]],
        "metadata": _loaded_metadata(text),
    }
    write_job_report(0, workdir, extra={"payload_version": canary["payload_version"],
                                        "canary": canary})
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "canary":
        # Canary probe job: same runner, landing-kit payload.
        payload_seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        return run_canary(payload_seconds, os.getcwd())
    if len(sys.argv) >= 2 and sys.argv[1] == "payload-canary":
        # Payload canary job: same runner, production payload on row 1,
        # outputs to the expiring canary dataset named by the stamp.
        if len(sys.argv) < 4:
            print(f"Usage: {sys.argv[0]} payload-canary <csv_base> <stamp>",
                  file=sys.stderr)
            return 2
        return run_payload_canary(sys.argv[2], sys.argv[3], os.getcwd())
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <SEQNUMBER> <csv_base>", file=sys.stderr)
        return 2
    # The pilot runs this dispatcher in the job workdir; capture it before
    # the payload runs so the report lands where the pilot looks.
    workdir = os.getcwd()
    rc, _row = run_row(int(sys.argv[1]), sys.argv[2], workdir)
    # The report carries the payload version on every job; the pilot lifts
    # it into the job record on success, and the exit message on failure.
    write_job_report(rc, workdir, extra={"payload_version": payload_version(workdir)})
    return rc


if __name__ == "__main__":
    sys.exit(main())
