#!/usr/bin/env python3
"""Parity smoke probe for the swf-monitor FastMCP migration.

Compares two MCP endpoints (live django-mcp-server vs FastMCP candidate)
and asserts the checks listed in docs/MCP_FASTMCP_MIGRATION_PLAN.md
"Parity Checks (must all pass before Phase 2 cutover)".

Example:
    scripts/mcp_migration_smoke.py \\
        --live-url http://127.0.0.1:8001/swf-monitor/mcp/ \\
        --candidate-url http://127.0.0.1:8013/swf-monitor/mcp/ \\
        --candidate-token "$MCP_BEARER_TOKEN" \\
        --check-against-settings

Exits non-zero on any failure. Live and candidate take per-endpoint auth
flags (--live-token / --candidate-token); pass an empty string for
unauthenticated endpoints. The plan deliberately does NOT support
disabling auth on the candidate during the parity window -- a temporary
auth bypass is exactly the kind of code we don't want shipped.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


def _request(url, method="POST", payload=None, token=None, timeout=10.0):
    headers = {
        "accept": "application/json, text/event-stream",
        "user-agent": "mcp-migration-smoke/1.0",
    }
    data = None
    if payload is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            try:
                return response.status, json.loads(body) if body else None
            except json.JSONDecodeError:
                return response.status, {"_raw": body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": body}


def mcp_initialize(url, token, timeout):
    status, body = _request(url, payload={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-migration-smoke", "version": "1.0"},
        },
    }, token=token, timeout=timeout)
    if status != 200 or not body or "result" not in body:
        raise RuntimeError(f"initialize {url}: HTTP {status} body={body!r}")
    return body["result"]


def mcp_tools_list(url, token, timeout):
    status, body = _request(url, payload={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list",
    }, token=token, timeout=timeout)
    if status != 200 or not body or "result" not in body:
        raise RuntimeError(f"tools/list {url}: HTTP {status} body={body!r}")
    return body["result"]["tools"]


def mcp_call(url, token, name, args, timeout):
    """tools/call returning the concatenated text of all content blocks.

    Single-block tools (return a str/dict) get block[0]'s text. Multi-block
    tools (return a list) — FastMCP and django-mcp-server both split each
    element into its own block — get their texts concatenated.
    """
    status, body = _request(url, payload={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }, token=token, timeout=timeout)
    if status != 200 or not body or "result" not in body:
        raise RuntimeError(f"tools/call {name} {url}: HTTP {status} body={body!r}")
    content = body["result"].get("content") or []
    return [block.get("text", "") for block in content]


def _load_settings_instructions():
    """Import Django settings and return MCP_SERVER_INSTRUCTIONS."""
    import os
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, os.path.join(repo_root, "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "swf_monitor_project.settings")
    import django
    django.setup()
    from django.conf import settings
    return settings.MCP_SERVER_NAME, settings.MCP_SERVER_INSTRUCTIONS


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live-url", required=True)
    parser.add_argument("--candidate-url", required=True)
    parser.add_argument("--live-token", default="")
    parser.add_argument("--candidate-token", default="")
    parser.add_argument("--expected-name", default="swf-testbed")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--check-against-settings", action="store_true",
                        help="Load Django settings and require candidate "
                             "serverInfo to byte-match MCP_SERVER_NAME / "
                             "MCP_SERVER_INSTRUCTIONS.")
    args = parser.parse_args()

    failures: list[str] = []

    def fail(msg: str):
        failures.append(msg)
        print(f"  [FAIL] {msg}")

    def ok(msg: str):
        print(f"  [OK]   {msg}")

    print(f"Live      : {args.live_url}")
    print(f"Candidate : {args.candidate_url}")
    print()

    print("== initialize handshake ==")
    live_init = mcp_initialize(args.live_url, args.live_token, args.timeout)
    cand_init = mcp_initialize(args.candidate_url, args.candidate_token, args.timeout)
    live_si = live_init.get("serverInfo", {})
    cand_si = cand_init.get("serverInfo", {})
    live_instr = live_init.get("instructions") or ""
    cand_instr = cand_init.get("instructions") or ""
    print(f"  live  serverInfo={live_si} instructions={len(live_instr)} chars")
    print(f"  cand  serverInfo={cand_si} instructions={len(cand_instr)} chars")
    print()

    # Check 3: candidate serverInfo.name
    print("== #3 candidate serverInfo.name ==")
    if cand_si.get("name") != args.expected_name:
        fail(f"#3 candidate serverInfo.name={cand_si.get('name')!r} expected {args.expected_name!r}")
    else:
        ok(f"#3 candidate serverInfo.name == {args.expected_name!r}")
    print()

    # Check 4: candidate instructions vs settings
    print("== #4 candidate serverInfo.instructions ==")
    if args.check_against_settings:
        try:
            expected_name, expected_instr = _load_settings_instructions()
        except Exception as exc:
            fail(f"#4 could not load Django settings: {exc}")
        else:
            if expected_name != args.expected_name:
                fail(f"#4 settings.MCP_SERVER_NAME={expected_name!r} != --expected-name {args.expected_name!r}")
            if cand_instr != expected_instr:
                fail(f"#4 candidate instructions != settings.MCP_SERVER_INSTRUCTIONS "
                     f"(lens {len(cand_instr)} vs {len(expected_instr)})")
            else:
                ok(f"#4 candidate instructions byte-equal to settings.MCP_SERVER_INSTRUCTIONS "
                   f"({len(expected_instr)} chars)")
    else:
        if cand_instr != live_instr:
            fail(f"#4 candidate instructions != live instructions "
                 f"(lens {len(cand_instr)} vs {len(live_instr)}); "
                 f"pass --check-against-settings for canonical comparison")
        else:
            ok(f"#4 candidate instructions byte-equal to live instructions "
               f"({len(cand_instr)} chars)")
    print()

    # Check 5: candidate get_server_instructions
    print("== #5 candidate tools/call get_server_instructions ==")
    cand_gsi_blocks = mcp_call(args.candidate_url, args.candidate_token,
                              "get_server_instructions", {}, args.timeout)
    cand_gsi = cand_gsi_blocks[0] if cand_gsi_blocks else ""
    if cand_gsi != cand_instr:
        fail(f"#5 candidate get_server_instructions != serverInfo.instructions "
             f"(lens {len(cand_gsi)} vs {len(cand_instr)})")
    else:
        ok(f"#5 candidate get_server_instructions byte-equal to serverInfo.instructions")
    print()

    # Check 1: tool name set equality
    print("== #1 tool name set equality ==")
    live_tools = mcp_tools_list(args.live_url, args.live_token, args.timeout)
    cand_tools = mcp_tools_list(args.candidate_url, args.candidate_token, args.timeout)
    live_names = {t["name"] for t in live_tools}
    cand_names = {t["name"] for t in cand_tools}
    only_live = sorted(live_names - cand_names)
    only_cand = sorted(cand_names - live_names)
    if only_live or only_cand:
        fail(f"#1 tool name sets diverge — "
             f"live-only: {only_live}, candidate-only: {only_cand}")
    else:
        ok(f"#1 tool name sets equal ({len(live_names)} tools)")
    print()

    # Check 2: schema equivalence (parameter sets + required flags)
    print("== #2 inputSchema parameter-set and required equality ==")
    live_by_name = {t["name"]: t for t in live_tools}
    cand_by_name = {t["name"]: t for t in cand_tools}
    shared = sorted(live_names & cand_names)
    schema_diffs = []
    for name in shared:
        ls = live_by_name[name].get("inputSchema") or {}
        cs = cand_by_name[name].get("inputSchema") or {}
        l_props = set((ls.get("properties") or {}).keys())
        c_props = set((cs.get("properties") or {}).keys())
        l_req = set(ls.get("required") or [])
        c_req = set(cs.get("required") or [])
        if l_props != c_props or l_req != c_req:
            schema_diffs.append({
                "tool": name,
                "live_props_only": sorted(l_props - c_props),
                "cand_props_only": sorted(c_props - l_props),
                "live_required_only": sorted(l_req - c_req),
                "cand_required_only": sorted(c_req - l_req),
            })
    if schema_diffs:
        fail(f"#2 inputSchema diverges on {len(schema_diffs)} tools:")
        for d in schema_diffs[:10]:
            print(f"      {d}")
        if len(schema_diffs) > 10:
            print(f"      ... and {len(schema_diffs) - 10} more")
    else:
        ok(f"#2 inputSchema parameter-set + required equal on {len(shared)} shared tools")
    print()

    # Check 7: swf_list_available_tools parity with tools/list (candidate).
    # Returns a list of dicts; FastMCP and django-mcp-server both split a
    # list return into one content block per element, so parse each block.
    print("== #7 swf_list_available_tools parity (candidate) ==")
    sla_blocks = mcp_call(args.candidate_url, args.candidate_token,
                          "swf_list_available_tools", {}, args.timeout)
    sla_names = set()
    parse_errors = 0
    for block in sla_blocks:
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if isinstance(obj, dict) and "name" in obj:
            sla_names.add(obj["name"])
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and "name" in item:
                    sla_names.add(item["name"])
    if parse_errors:
        fail(f"#7 swf_list_available_tools: {parse_errors} content blocks "
             f"failed to parse as JSON")
    if not sla_names:
        fail(f"#7 swf_list_available_tools returned no recognizable names "
             f"({len(sla_blocks)} blocks)")
    else:
        only_sla = sorted(sla_names - cand_names)
        only_cn = sorted(cand_names - sla_names)
        if only_sla or only_cn:
            fail(f"#7 discoverer != tools/list — "
                 f"discoverer-only: {only_sla}, tools-only: {only_cn}")
        else:
            ok(f"#7 swf_list_available_tools set == tools/list set "
               f"({len(sla_names)} tools)")
    print()

    # Check 6: candidate auth matrix (only if a token is configured)
    print("== #6 candidate auth matrix ==")
    if not args.candidate_token:
        print(f"  [SKIP] #6 — no --candidate-token configured")
    else:
        # 6a: GET → 405
        get_status, _ = _request(args.candidate_url, method="GET",
                                 token=args.candidate_token,
                                 timeout=args.timeout)
        if get_status != 405:
            fail(f"#6a candidate GET status {get_status} expected 405")
        else:
            ok(f"#6a candidate GET returns 405")
        # 6b: POST no auth → 401
        s, _ = _request(args.candidate_url, payload={
            "jsonrpc": "2.0", "id": 99, "method": "tools/list",
        }, token=None, timeout=args.timeout)
        if s != 401:
            fail(f"#6b candidate POST no-auth status {s} expected 401")
        else:
            ok(f"#6b candidate POST no-auth returns 401")
        # 6c: POST bad token → 403
        s, _ = _request(args.candidate_url, payload={
            "jsonrpc": "2.0", "id": 99, "method": "tools/list",
        }, token="wrong-token-for-smoke", timeout=args.timeout)
        if s != 403:
            fail(f"#6c candidate POST wrong-token status {s} expected 403")
        else:
            ok(f"#6c candidate POST wrong-token returns 403")
        # 6d: /health no auth → 200
        parsed = urlparse(args.candidate_url)
        health_url = f"{parsed.scheme}://{parsed.netloc}/health"
        s, body = _request(health_url, method="GET", timeout=args.timeout)
        if s != 200 or not isinstance(body, dict) or body.get("status") != "ok":
            fail(f"#6d candidate {health_url}: status {s} body {body!r}")
        else:
            ok(f"#6d candidate /health returns 200 {{'status':'ok'}}")
    print()

    if failures:
        print(f"\n{len(failures)} parity check(s) FAILED.")
        return 1
    print("All parity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
