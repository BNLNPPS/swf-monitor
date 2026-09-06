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
    if extra:
        body.update(extra)
    try:
        with open(os.path.join(workdir, "jobReport.json"), "w") as f:
            json.dump(body, f)
        print(f"jobReport.json written: exitCode={rc} exitMsg={msg[:500]}")
    except OSError as e:
        print(f"jobReport.json not written: {e}", file=sys.stderr)


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


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "canary":
        # Canary probe job: same runner, landing-kit payload.
        payload_seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        return run_canary(payload_seconds, os.getcwd())
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <SEQNUMBER> <csv_base>", file=sys.stderr)
        return 2
    n = int(sys.argv[1])
    csv_base = sys.argv[2]

    # PanDA SEQNUMBER is 1-based; the CSV is 0-indexed.
    csv_index = n - 1

    # Read only the requested row — no need to load the whole manifest.
    with open(f"{csv_base}.csv") as f:
        reader = csv.reader(f)
        row = next(islice(reader, csv_index, csv_index + 1), None)
    if row is None:
        print(f"Error: row {n} not found in {csv_base}.csv", file=sys.stderr)
        return 1
    if len(row) < 4:
        print(f"Error: malformed CSV row {n}: {row!r}", file=sys.stderr)
        return 1

    file_path, ext, nevents, ichunk = row[0], row[1], row[2], row[3]
    # The pilot runs this dispatcher in the job workdir; capture it before
    # the payload runs so the report lands where the pilot looks.
    workdir = os.getcwd()
    payload_run = os.path.join(workdir, PAYLOAD_SUBDIR, "run.sh")
    version = payload_version(workdir)
    print(f"epicprod payload {version or '(no VERSION)'}: {payload_run}")
    # The payload prepends the JLab xrootd path to EVGEN/<file>; pass the
    # EVGEN-relative path, extension, event count and chunk index through.
    result = subprocess.run(
        [payload_run, f"EVGEN/{file_path}", ext, nevents, ichunk],
        text=True,
    )
    # The report carries the payload version on every job; the pilot lifts
    # it into the job record on success, and the exit message on failure.
    write_job_report(result.returncode, workdir,
                     extra={"payload_version": version})
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
