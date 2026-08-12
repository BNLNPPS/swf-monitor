#!/usr/bin/env python3
"""
Run one credentialed PanDA task operation for an existing JEDI task.

The web tier only queues these requests. This doer sources the PanDA client
environment, uses the cached production token, and calls the PanDA client API.

Before any retry-class operation it reopens the task's closed BNL Rucio
datasets (the PanDA-registered log datasets): PanDA closes them at task
finalization and the gen-VO JEDI plugins, unlike ATLAS's, never reopen them,
so a post-final retry otherwise fails every log registration (DDM error 200,
"is closed"). The reopen runs in this outer process under the invoking venv
(rucio client + X509_USER_PROXY); its report is merged into the result JSON.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


DEFAULT_PCLIENT_SETUP = os.path.expanduser("~/pclient/run/setup.sh")
DEFAULT_AUTH_VO = "EIC.production"

# Single-op verbs whose PanDA acceptance is followed by task-state
# verification. Single-op retry_failures keeps its fire-and-report
# behavior for the compose page; batch retry_failures verifies.
STATE_CHANGE_OPERATIONS = ("pause", "resume", "finish")
BATCH_OPERATIONS = ("pause", "resume", "retry_failures", "finish")
# A retried task has left these states once JEDI acts on the command.
RETRY_TERMINAL_STATUSES = (
    "finished", "failed", "done", "exhausted", "aborted", "broken")

# Operations that make JEDI generate new jobs against the task's existing
# datasets; these get the reopen-before-retry pass.
RETRY_OPERATIONS = ("retry_failures", "increase_attempts")
# BNL Rucio connection for the reopen pass, same env convention as
# cache-payload-log.py; the agent's production.env provides the values.
RUCIO_URL = os.environ.get("RUCIO_URL", "https://nprucio01.sdcc.bnl.gov:443")
RUCIO_ACCOUNT = os.environ.get("RUCIO_ACCOUNT", "panda")
RUCIO_VO = os.environ.get("RUCIO_VO", "eic")
RUCIO_SCOPE = os.environ.get("RUCIO_SCOPE", "group.EIC")
X509_PROXY = os.environ.get("X509_USER_PROXY", "/data/wenauseic/longproxy-for-rucio")
# A reopened dataset gets a fresh lifetime so a retried task's logs do not
# expire on the original registration-time clock (PanDA registers with 30d).
REOPENED_LIFETIME_DAYS = 30


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _reopen_task_datasets(jedi_task_ids):
    """Reopen the closed BNL Rucio datasets of tasks about to be retried.

    Datasets are found by their task_id metadata (exact, set by PanDA at
    registration — never by name pattern, which collides across minQ2
    siblings). Each closed dataset is reopened and given a fresh lifetime.
    Failures are reported, never raised: the retry proceeds and the report
    lands in the operation record for the operator to see.
    """
    report = {"tasks": {}, "reopened": 0, "ok": True}
    try:
        from rucio.client import Client
    except ImportError as e:
        report["ok"] = False
        report["error"] = f"rucio client unavailable in {sys.executable}: {e}"
        _log(f"WARNING: dataset reopen skipped: {report['error']}")
        return report
    try:
        client = Client(
            rucio_host=RUCIO_URL, auth_host=RUCIO_URL, account=RUCIO_ACCOUNT,
            auth_type="x509_proxy", creds={"client_proxy": X509_PROXY},
            ca_cert=None, vo=RUCIO_VO)
        client.whoami()
    except Exception as e:
        report["ok"] = False
        report["error"] = f"BNL Rucio auth failed: {e}"
        _log(f"WARNING: dataset reopen skipped: {report['error']}")
        return report
    for jedi_task_id in jedi_task_ids:
        entry = {"datasets": [], "reopened": [], "errors": []}
        report["tasks"][str(jedi_task_id)] = entry
        try:
            names = list(client.list_dids(
                scope=RUCIO_SCOPE, filters={"task_id": int(jedi_task_id)},
                did_type="dataset"))
        except Exception as e:
            entry["errors"].append(f"list_dids failed: {e}")
            report["ok"] = False
            _log(f"WARNING: dataset lookup failed for task {jedi_task_id}: {e}")
            continue
        entry["datasets"] = names
        for name in names:
            try:
                meta = client.get_metadata(scope=RUCIO_SCOPE, name=name)
                if meta.get("is_open") is False:
                    client.set_status(scope=RUCIO_SCOPE, name=name, open=True)
                    client.set_metadata(
                        scope=RUCIO_SCOPE, name=name, key="lifetime",
                        value=REOPENED_LIFETIME_DAYS * 86400)
                    entry["reopened"].append(name)
                    report["reopened"] += 1
                    _log(f"reopened closed dataset for task {jedi_task_id}: "
                         f"{RUCIO_SCOPE}:{name}")
            except Exception as e:
                entry["errors"].append(f"{name}: {e}")
                report["ok"] = False
                _log(f"WARNING: reopen failed for {RUCIO_SCOPE}:{name}: {e}")
    return report


def _merge_reopen_report(stdout_text, report):
    """Inject the reopen report into the doer's final JSON output line.

    The inner pclient process prints one JSON line; the agent parses the
    last stdout line. If the merge cannot parse that line, the original
    stdout is returned untouched and the report goes to stderr instead —
    the report is never silently dropped.
    """
    lines = stdout_text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            continue
        try:
            payload = json.loads(lines[index])
        except ValueError:
            break
        payload["dataset_reopen"] = report
        lines[index] = json.dumps(payload, default=str)
        return "\n".join(lines) + ("\n" if stdout_text.endswith("\n") else "")
    _log(f"WARNING: could not merge dataset reopen report into result JSON; "
         f"report: {json.dumps(report, default=str)}")
    return stdout_text


def _run_inside_pclient(args):
    payload = {
        "operation": args.operation,
        "jedi_task_id": args.jedi_task_id,
        "increase": args.increase,
        "new_parameters": args.new_parameters,
        "verify_timeout": args.verify_timeout,
        "poll_interval": args.poll_interval,
        "send_interval": args.send_interval,
        "items": args.items,
    }
    with tempfile.TemporaryDirectory(prefix="panda-task-operation.") as tmpdir:
        payload_path = os.path.join(tmpdir, "payload.json")
        runner = os.path.join(tmpdir, "run.sh")
        with open(payload_path, "w") as f:
            json.dump(payload, f)
        with open(runner, "w") as f:
            f.write("#!/bin/bash\nset -e\n")
            f.write(f"source {args.pclient_setup}\n")
            f.write(f"export PANDA_AUTH_VO={args.auth_vo}\n")
            f.write(f"python3 {os.path.abspath(__file__)} --inside-pclient --payload {payload_path}\n")
        try:
            p = subprocess.run(["bash", runner], capture_output=True, text=True,
                               timeout=args.timeout)
        except subprocess.TimeoutExpired:
            _log(f"ERROR: PanDA operation timed out after {args.timeout}s")
            return 4
    stdout_text = p.stdout or ""
    if getattr(args, "reopen_report", None) is not None and stdout_text:
        stdout_text = _merge_reopen_report(stdout_text, args.reopen_report)
    if stdout_text:
        print(stdout_text, end="")
    if p.stderr:
        print(p.stderr, end="", file=sys.stderr)
    return p.returncode


def _inside_pclient(payload_path):
    from pandaclient import Client, panda_api

    with open(payload_path) as f:
        payload = json.load(f)

    client = panda_api.get_api()
    operation = payload["operation"]
    jedi_task_id = int(payload["jedi_task_id"])

    if payload.get("items"):
        output = _run_batch_panda_operations(
            Client,
            operation,
            payload["items"],
            verify_timeout=float(payload.get("verify_timeout") or 90),
            poll_interval=float(payload.get("poll_interval") or 5),
            send_interval=float(payload.get("send_interval") or 1),
        )
        print(json.dumps(output, default=str))
        return 0

    if operation == "increase_attempts":
        result = client.increase_attempt_nr(jedi_task_id, int(payload.get("increase") or 1))
    elif operation == "retry_failures":
        new_parameters = payload.get("new_parameters") or None
        if new_parameters is None:
            observed, _diag = _panda_task_status(
                Client.getTaskStatus(jedi_task_id, False))
            if observed == "aborted":
                new_parameters = _reactivation_params(Client, jedi_task_id)
        result = client.retry_task(jedi_task_id, new_parameters=new_parameters)
    elif operation == "finish":
        result = Client.finishTask(jedi_task_id, False)
    elif operation in ("pause", "resume"):
        action = Client.pauseTask if operation == "pause" else Client.resumeTask
        result = action(jedi_task_id, False)
    else:
        raise ValueError(f"unknown operation {operation!r}")

    ok, diagnostic = _panda_result_ok(result)
    if operation == "retry_failures":
        ok = _retry_accepted(ok, diagnostic)
    output = {
        "operation": operation,
        "jedi_task_id": jedi_task_id,
        "ok": ok,
        "diagnostic": diagnostic,
        "result": result,
    }
    if operation in STATE_CHANGE_OPERATIONS:
        output["accepted"] = ok
        output["verified"] = False
        output["observed_status"] = ""
        if ok:
            deadline = time.monotonic() + float(payload.get("verify_timeout") or 90)
            poll_interval = max(1.0, float(payload.get("poll_interval") or 5))
            while True:
                status_result = Client.getTaskStatus(jedi_task_id, False)
                observed_status, status_diagnostic = _panda_task_status(status_result)
                if observed_status:
                    output["observed_status"] = observed_status
                    if _task_state_verified(operation, observed_status):
                        output["verified"] = True
                        break
                else:
                    output["status_diagnostic"] = status_diagnostic
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_interval)
    print(json.dumps(output, default=str))
    if not ok:
        _log(f"ERROR: PanDA returned failure for {operation} on {jedi_task_id}: {diagnostic}")
        return 1
    return 0


def _reactivation_params(Client, jedi_task_id):
    """No-op parameter restatement that routes an aborted task through
    PanDA's reactivation path (retry with new_parameters -> incexec, the
    only retry the command gate accepts for aborted; verified live on
    task 38541). Restates the stored taskPriority verbatim so nothing
    about the task changes. Returns None when the stored parameters
    cannot be read — the caller then sends a plain retry, whose refusal
    diagnostic is recorded, rather than risking a priority change."""
    try:
        status, output = Client.getTaskParamsMap(jedi_task_id)
    except Exception as exc:
        _log(f"WARNING: getTaskParamsMap failed for {jedi_task_id}: {exc}")
        return None
    if status == 0 and isinstance(output, (list, tuple)) and len(output) > 1:
        params = output[1]
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                params = None
        if isinstance(params, dict) and params.get("taskPriority") is not None:
            return {"taskPriority": params["taskPriority"]}
    _log(f"WARNING: no stored taskPriority for {jedi_task_id}; "
         "cannot build reactivation params")
    return None


def _retry_accepted(ok, diagnostic):
    """Plain retry acceptance is return code 0; the reactivation path
    answers return code 3 with an explicit acceptance message."""
    if ok:
        return True
    return "reactivation accepted" in str(diagnostic or "")


def _panda_result_ok(result):
    """Interpret PanDA client (transport_status, operation_result) returns."""
    if isinstance(result, (list, tuple)) and result:
        status = result[0]
        if status != 0:
            return False, f"transport status {status}"
        if len(result) == 1:
            return True, "transport succeeded"
        payload = result[1]
        if isinstance(payload, (list, tuple)) and payload:
            code = payload[0]
            message = payload[1] if len(payload) > 1 else ""
            return code == 0, f"return code {code}: {message}"
        if isinstance(payload, dict):
            if "success" in payload:
                return bool(payload["success"]), payload.get("message") or str(payload)
            if "code" in payload:
                return payload.get("code") == 0, payload.get("message") or str(payload)
        if payload is None:
            return False, "operation returned None"
        return True, str(payload)
    return result is not None, str(result)


def _panda_task_status(result):
    """Interpret Client.getTaskStatus's (transport_status, status) result."""
    if not isinstance(result, (list, tuple)) or not result:
        return "", str(result)
    if result[0] != 0:
        return "", f"transport status {result[0]}"
    if len(result) < 2 or result[1] is None:
        return "", "task status unavailable"
    status = str(result[1]).strip().lower()
    if not status:
        return "", "empty task status"
    return status, ""


def _task_state_verified(operation, observed_status):
    if operation == "pause":
        return observed_status == "paused"
    if operation == "resume":
        return observed_status not in ("paused", "throttled", "staging")
    if operation == "finish":
        return observed_status in ("finishing", "passed", "finished", "done")
    if operation == "retry_failures":
        return observed_status not in RETRY_TERMINAL_STATUSES
    return False


def _run_batch_panda_operations(Client, operation, items, *, verify_timeout,
                                poll_interval, send_interval):
    """Submit scalar commands with pacing, then verify on one shared clock."""
    actions = {
        "pause": Client.pauseTask,
        "resume": Client.resumeTask,
    }
    results = []
    for index, item in enumerate(items):
        operation_id = str(item.get("operation_id") or "")
        jedi_task_id = int(item["jedi_task_id"])
        try:
            if operation == "retry_failures":
                # An aborted task takes the reactivation path (retry with
                # new_parameters -> incexec); plain retry refuses aborted.
                observed, _diag = _panda_task_status(
                    Client.getTaskStatus(jedi_task_id, False))
                if observed == "aborted":
                    panda_result = Client.retryTask(
                        jedi_task_id, False,
                        newParams=_reactivation_params(Client, jedi_task_id))
                else:
                    panda_result = Client.retryTask(jedi_task_id, False)
            elif operation == "finish":
                panda_result = Client.finishTask(jedi_task_id, False)
            else:
                panda_result = actions[operation](jedi_task_id, False)
            accepted, diagnostic = _panda_result_ok(panda_result)
            if operation == "retry_failures":
                accepted = _retry_accepted(accepted, diagnostic)
        except Exception as exc:
            panda_result = None
            accepted = False
            diagnostic = str(exc)
        results.append({
            "operation_id": operation_id,
            "jedi_task_id": jedi_task_id,
            "accepted": accepted,
            "verified": False,
            "observed_status": "",
            "diagnostic": diagnostic,
            "result": panda_result,
        })
        if index + 1 < len(items):
            time.sleep(max(0.0, send_interval))

    pending = {index for index, result in enumerate(results)
               if result["accepted"]}
    deadline = time.monotonic() + verify_timeout
    while pending:
        for index in list(pending):
            result = results[index]
            try:
                status_result = Client.getTaskStatus(
                    result["jedi_task_id"], False)
            except Exception as exc:
                result["status_diagnostic"] = str(exc)
                continue
            observed_status, status_diagnostic = _panda_task_status(status_result)
            if observed_status:
                result["observed_status"] = observed_status
                if _task_state_verified(operation, observed_status):
                    result["verified"] = True
                    pending.remove(index)
            elif status_diagnostic:
                result["status_diagnostic"] = status_diagnostic
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(max(1.0, poll_interval))

    return {
        "operation": operation,
        "batch": True,
        "results": results,
    }


def main():
    ap = argparse.ArgumentParser(description="Run an existing PanDA task operation.")
    ap.add_argument(
        "--operation",
        choices=["increase_attempts", "retry_failures", "pause", "resume",
                 "finish"],
    )
    ap.add_argument("--jedi-task-id", type=int)
    ap.add_argument("--increase", type=int, default=1)
    ap.add_argument("--new-parameters", default="", help="JSON object for retry_task new_parameters")
    ap.add_argument("--auth-vo", default=DEFAULT_AUTH_VO)
    ap.add_argument("--pclient-setup", default=DEFAULT_PCLIENT_SETUP)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--verify-timeout", type=int, default=90)
    ap.add_argument("--poll-interval", type=float, default=5)
    ap.add_argument("--send-interval", type=float, default=1)
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--inside-pclient", action="store_true")
    ap.add_argument("--payload")
    args = ap.parse_args()
    args.items = []

    if args.inside_pclient:
        if not args.payload:
            _log("ERROR: --payload required with --inside-pclient")
            return 2
        return _inside_pclient(args.payload)

    if not args.operation or not args.jedi_task_id:
        if not args.batch or not args.operation:
            _log("ERROR: --operation and --jedi-task-id are required")
            return 2
    if args.batch:
        try:
            args.items = json.load(sys.stdin)
        except (ValueError, TypeError) as exc:
            _log(f"ERROR: batch stdin is not valid JSON: {exc}")
            return 2
        if not isinstance(args.items, list) or not args.items:
            _log("ERROR: batch stdin must be a non-empty JSON list")
            return 2
        if args.operation not in BATCH_OPERATIONS:
            _log("ERROR: batch mode supports "
                 + ", ".join(BATCH_OPERATIONS))
            return 2
        args.jedi_task_id = int(args.items[0]["jedi_task_id"])
    if args.increase < 1:
        _log("ERROR: --increase must be >= 1")
        return 2
    if args.new_parameters:
        try:
            new_parameters = json.loads(args.new_parameters)
        except ValueError as e:
            _log(f"ERROR: --new-parameters is not valid JSON: {e}")
            return 2
        if not isinstance(new_parameters, dict):
            _log("ERROR: --new-parameters must be a JSON object")
            return 2
        args.new_parameters = new_parameters
    else:
        args.new_parameters = None

    args.reopen_report = None
    if args.operation in RETRY_OPERATIONS:
        ids = ([item.get("jedi_task_id") for item in args.items]
               if args.items else [args.jedi_task_id])
        args.reopen_report = _reopen_task_datasets([i for i in ids if i])

    return _run_inside_pclient(args)


if __name__ == "__main__":
    sys.exit(main())
