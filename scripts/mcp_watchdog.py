#!/usr/bin/env python3
"""Probe swf-monitor MCP and optionally restart the ASGI service on failure."""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


DEFAULT_MCP_URL = "http://127.0.0.1:8001/swf-monitor/mcp/"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8001/swf-monitor/api/mcp-health/"


def post_json(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "swf-monitor-mcp-watchdog/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)


def get_json(url, timeout):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "swf-monitor-mcp-watchdog/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)


def probe(health_url, mcp_url, timeout):
    started = time.monotonic()
    health_status, health = get_json(health_url, timeout)
    if health_status != 200 or not health.get("ok"):
        raise RuntimeError(f"health failed: status={health_status} body={health}")

    init_status, init = post_json(
        mcp_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "swf-monitor-mcp-watchdog",
                    "version": "1.0",
                },
            },
        },
        timeout,
    )
    if init_status != 200 or "result" not in init:
        raise RuntimeError(f"initialize failed: status={init_status} body={init}")

    tools_status, tools = post_json(
        mcp_url,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
        },
        timeout,
    )
    tool_list = tools.get("result", {}).get("tools")
    if tools_status != 200 or not isinstance(tool_list, list):
        raise RuntimeError(f"tools/list failed: status={tools_status} body={tools}")

    elapsed = time.monotonic() - started
    return elapsed, len(tool_list)


def restart_service(service):
    result = subprocess.run(
        ["systemctl", "restart", service],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"restart failed rc={result.returncode}: "
            f"{result.stdout.strip()} {result.stderr.strip()}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--service", default="swf-monitor-mcp-asgi.service")
    args = parser.parse_args()

    try:
        elapsed, tool_count = probe(args.health_url, args.mcp_url, args.timeout)
    except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as e:
        print(f"MCP watchdog probe failed: {e}", file=sys.stderr)
        if args.restart:
            try:
                restart_service(args.service)
                print(f"Restarted {args.service}", file=sys.stderr)
            except RuntimeError as restart_error:
                print(str(restart_error), file=sys.stderr)
                return 2
        return 1

    print(f"MCP watchdog OK: {tool_count} tools in {elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
