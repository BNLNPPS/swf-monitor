#!/usr/bin/env python3
"""
Run one credentialed PanDA task operation for an existing JEDI task.

The web tier only queues these requests. This doer sources the PanDA client
environment, uses the cached production token, and calls the PanDA client API.
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
STATE_CHANGE_OPERATIONS = ("pause", "resume", "kill")
BATCH_OPERATIONS = ("pause", "resume", "retry_failures", "kill")
# A retried task has left these states once JEDI acts on the command.
RETRY_TERMINAL_STATUSES = (
    "finished", "failed", "done", "exhausted", "aborted", "broken")


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


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
    if p.stdout:
        print(p.stdout, end="")
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
        result = client.retry_task(jedi_task_id, new_parameters=new_parameters)
    elif operation == "kill":
        result = Client.killTask(jedi_task_id, False)
    elif operation in ("pause", "resume"):
        action = Client.pauseTask if operation == "pause" else Client.resumeTask
        result = action(jedi_task_id, False)
    else:
        raise ValueError(f"unknown operation {operation!r}")

    ok, diagnostic = _panda_result_ok(result)
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
    if operation == "kill":
        return observed_status in ("aborting", "aborted")
    if operation == "retry_failures":
        return observed_status not in RETRY_TERMINAL_STATUSES
    return False


def _run_batch_panda_operations(Client, operation, items, *, verify_timeout,
                                poll_interval, send_interval):
    """Submit scalar commands with pacing, then verify on one shared clock."""
    actions = {
        "pause": Client.pauseTask,
        "resume": Client.resumeTask,
        "retry_failures": Client.retryTask,
        "kill": Client.killTask,
    }
    action = actions[operation]
    results = []
    for index, item in enumerate(items):
        operation_id = str(item.get("operation_id") or "")
        jedi_task_id = int(item["jedi_task_id"])
        try:
            panda_result = action(jedi_task_id, False)
            accepted, diagnostic = _panda_result_ok(panda_result)
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
                 "kill"],
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

    return _run_inside_pclient(args)


if __name__ == "__main__":
    sys.exit(main())
