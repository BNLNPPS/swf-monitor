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


DEFAULT_PCLIENT_SETUP = os.path.expanduser("~/pclient/run/setup.sh")
DEFAULT_AUTH_VO = "EIC.production"


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def _run_inside_pclient(args):
    payload = {
        "operation": args.operation,
        "jedi_task_id": args.jedi_task_id,
        "increase": args.increase,
        "new_parameters": args.new_parameters,
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
    from pandaclient import panda_api

    with open(payload_path) as f:
        payload = json.load(f)

    client = panda_api.get_api()
    operation = payload["operation"]
    jedi_task_id = int(payload["jedi_task_id"])

    if operation == "increase_attempts":
        result = client.increase_attempt_nr(jedi_task_id, int(payload.get("increase") or 1))
    elif operation == "retry_failures":
        new_parameters = payload.get("new_parameters") or None
        result = client.retry_task(jedi_task_id, new_parameters=new_parameters)
    else:
        raise ValueError(f"unknown operation {operation!r}")

    ok, diagnostic = _panda_result_ok(result)
    print(json.dumps({
        "operation": operation,
        "jedi_task_id": jedi_task_id,
        "ok": ok,
        "diagnostic": diagnostic,
        "result": result,
    }, default=str))
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


def main():
    ap = argparse.ArgumentParser(description="Run an existing PanDA task operation.")
    ap.add_argument("--operation", choices=["increase_attempts", "retry_failures"])
    ap.add_argument("--jedi-task-id", type=int)
    ap.add_argument("--increase", type=int, default=1)
    ap.add_argument("--new-parameters", default="", help="JSON object for retry_task new_parameters")
    ap.add_argument("--auth-vo", default=DEFAULT_AUTH_VO)
    ap.add_argument("--pclient-setup", default=DEFAULT_PCLIENT_SETUP)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--inside-pclient", action="store_true")
    ap.add_argument("--payload")
    args = ap.parse_args()

    if args.inside_pclient:
        if not args.payload:
            _log("ERROR: --payload required with --inside-pclient")
            return 2
        return _inside_pclient(args.payload)

    if not args.operation or not args.jedi_task_id:
        _log("ERROR: --operation and --jedi-task-id are required")
        return 2
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
