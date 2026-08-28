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
"""
import csv
import json
import os
import subprocess
import sys
from itertools import islice

# Payload entry point inside the eic_xl container (campaigns checkout at
# /opt/campaigns/hepmc3). Fixed by the container layout.
PAYLOAD_RUN = "/opt/campaigns/hepmc3/scripts/run.sh"

# run.sh's coded exits (its explicit `exit N` failure sites).
EXIT_MSGS = {65: "output validation failed", 78: "Rucio registration failed"}


def write_job_report(rc, workdir):
    """Write jobReport.json for the pilot to ship with the job record.

    The pilot reads this file from the job workdir and sends it with the
    final job update; the epic pilot plugin stores a nonzero exitCode and
    exitMsg in the job record (exeErrorCode/exeErrorDiag). The message is
    the payload's last ERROR line when one is found in the pilot-captured
    payload output, else the coded-exit description. Never raises and
    never alters the payload exit code.
    """
    msg = EXIT_MSGS.get(rc, f"payload exited {rc}")
    for name in ("payload.stdout", "payload.stderr"):
        try:
            with open(os.path.join(workdir, name), "rb") as f:
                lines = f.read()[-65536:].decode(errors="replace").splitlines()
            err = [ln.strip() for ln in lines if ln.strip().startswith("ERROR")]
            if err:
                msg = err[-1]
                break
        except OSError:
            continue
    try:
        with open(os.path.join(workdir, "jobReport.json"), "w") as f:
            json.dump({"exitCode": rc, "exitMsg": msg[:500]}, f)
        print(f"jobReport.json written: exitCode={rc} exitMsg={msg[:500]}")
    except OSError as e:
        print(f"jobReport.json not written: {e}", file=sys.stderr)


def main():
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
    # The payload prepends the JLab xrootd path to EVGEN/<file>; pass the
    # EVGEN-relative path, extension, event count and chunk index through.
    result = subprocess.run(
        [PAYLOAD_RUN, f"EVGEN/{file_path}", ext, nevents, ichunk],
        text=True,
    )
    if result.returncode != 0:
        write_job_report(result.returncode, workdir)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
