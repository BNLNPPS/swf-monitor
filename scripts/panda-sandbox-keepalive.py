#!/usr/bin/env python3
"""panda-sandbox-keepalive.py — keep retryable tasks' sandbox tarballs alive.

The PanDA server purges its sandbox cache of files whose modification time
is older than seven days (copyArchive.py), and nothing refreshes the files
of tasks that outlive that window, so a retry of an older task fails when
each job's pre-process cannot download the tarball (executor error 5303).
The server API provides ``touch_cache_file`` for exactly this: it resets
the file's modification time so the cleanup passes it by.

This doer touches the sandbox tarball of every task worth keeping
retryable: epic-VO tasks in a non-final state, plus tasks finished, failed,
or exhausted within the retention window (SysConfig
``panda_sandbox_keepalive_final_days``). Each task's tarball name and
source server come from its stored parameters (``jedi_taskparams``). A
tarball already purged is reported per task — that task is not natively
retryable. Aborted and broken tasks are not kept alive.

The prod-ops agent's doer for the nightly ``catalog_sync`` chain step;
Django-bootstrap standalone script — also usable by hand. Auth is the
production x509 proxy (``X509_USER_PROXY``) as TLS client certificate,
the same credential the payload-log fetch uses. See
``swf-epicprod/docs/JEDI_INTEGRATION.md``.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../scripts/panda-sandbox-keepalive.py [--dry-run]
"""
import argparse
import ast
import json
import os
import re
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

import requests  # noqa: E402
from django.db import connections  # noqa: E402
from monitor_app.models import SysConfig  # noqa: E402

X509_PROXY = os.environ.get(
    "X509_USER_PROXY", "/data/wenauseic/longproxy-for-rucio")
# The PanDA server's own cache checks run with verify disabled
# (copyArchive.py); honor a configured CA bundle when one is present.
CA_VERIFY = os.environ.get("REQUESTS_CA_BUNDLE") or False

FINAL_STATUSES = ("done", "finished", "failed", "broken", "aborted",
                  "exhausted")
RETRYABLE_FINAL_STATUSES = ("finished", "failed", "exhausted")

# Sandbox names come in two prefixes (jobO for API submissions, sources for
# prun) and two UUID spellings (condensed hex and hyphenated).
TARBALL_RE = re.compile(r"(?:jobO|sources)\.[0-9a-f-]+\.tar\.gz")
SOURCE_URL_RE = re.compile(r'"sourceURL":\s*"([^"]+)"')


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _candidate_tasks(final_days):
    cur = connections["panda"].cursor()
    cur.execute(
        "SELECT jeditaskid, status FROM jedi_tasks"
        " WHERE vo = 'epic'"
        " AND (NOT (status = ANY(%s))"
        "      OR (status = ANY(%s)"
        "          AND modificationtime > now() - make_interval(days => %s)))"
        " ORDER BY jeditaskid",
        [list(FINAL_STATUSES), list(RETRYABLE_FINAL_STATUSES),
         int(final_days)],
    )
    return cur.fetchall()


def _task_sandbox(jedi_task_id):
    """Return (source_url, tarball_name) from stored task parameters, or
    (None, None) when the task has no cache-resident sandbox."""
    cur = connections["panda"].cursor()
    cur.execute(
        "SELECT taskparams FROM jedi_taskparams WHERE jeditaskid = %s",
        [jedi_task_id],
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None, None
    text = row[0] if isinstance(row[0], str) else row[0].read()
    tarballs = set(TARBALL_RE.findall(text))
    sources = set(SOURCE_URL_RE.findall(text))
    if not tarballs or not sources:
        return None, None
    if len(tarballs) > 1 or len(sources) > 1:
        _log(f"WARNING: task {jedi_task_id} has ambiguous sandbox refs "
             f"tarballs={sorted(tarballs)} sources={sorted(sources)}")
    return sorted(sources)[0], sorted(tarballs)[0]


def _touch(source_url, tarball):
    """POST touch_cache_file; returns (status, message) where status is
    'touched', 'missing', or 'error'."""
    url = f"{source_url}/api/v1/file_server/touch_cache_file"
    try:
        r = requests.post(url, data={"file_name": tarball},
                          cert=(X509_PROXY, X509_PROXY),
                          verify=CA_VERIFY, timeout=30)
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return "error", f"HTTP {r.status_code}: {r.text[:200]}"
    # The endpoint answers with a Python-repr dict, not JSON; accept both.
    try:
        body = r.json()
    except ValueError:
        try:
            body = ast.literal_eval(r.text.strip())
        except (ValueError, SyntaxError):
            return "error", f"unparseable response: {r.text[:200]}"
    if not isinstance(body, dict):
        return "error", f"unexpected response shape: {r.text[:200]}"
    if body.get("success"):
        return "touched", ""
    message = str(body.get("message") or "")
    if "FileNotFoundError" in message or "No such file" in message:
        return "missing", message
    return "error", message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="enumerate and report; do not touch")
    args = parser.parse_args()

    final_days = int(SysConfig.get_setting(
        "panda_sandbox_keepalive_final_days", 30))

    tasks = _candidate_tasks(final_days)
    # One sandbox can serve several tasks (sandbox reuse); touch each once.
    by_tarball = {}
    no_sandbox = []
    for jedi_task_id, status in tasks:
        source_url, tarball = _task_sandbox(jedi_task_id)
        if not tarball:
            no_sandbox.append(jedi_task_id)
            continue
        entry = by_tarball.setdefault(
            (source_url, tarball), {"tasks": [], "statuses": []})
        entry["tasks"].append(jedi_task_id)
        entry["statuses"].append(status)

    touched, missing, errors = [], [], []
    for (source_url, tarball), entry in sorted(by_tarball.items()):
        if args.dry_run:
            _log(f"dry-run: would touch {tarball} on {source_url} "
                 f"for tasks {entry['tasks']}")
            continue
        status, message = _touch(source_url, tarball)
        record = {"tarball": tarball, "tasks": entry["tasks"],
                  "statuses": entry["statuses"]}
        if status == "touched":
            touched.append(record)
        elif status == "missing":
            missing.append(record)
            _log(f"WARNING: sandbox already purged for tasks "
                 f"{entry['tasks']} ({tarball}) — not natively retryable")
        else:
            record["error"] = message
            errors.append(record)
            _log(f"ERROR: touch failed for {tarball} on {source_url}: "
                 f"{message}")

    summary = {
        "final_days": final_days,
        "candidates": len(tasks),
        "no_sandbox": no_sandbox,
        "tarballs": len(by_tarball),
        "touched": len(touched),
        "missing": missing,
        "errors": errors,
        "dry_run": bool(args.dry_run),
        "ok": not errors,
    }
    print(json.dumps(summary, default=str))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
