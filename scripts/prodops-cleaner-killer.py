#!/usr/bin/env python3
"""
Cleaner-killer for the ePIC production-operations agent (a system singleton).

The prod-ops agent subscribes to the anycast queue /queue/epicprod.ops, so a
second instance anywhere would *steal* requests. systemd keeps exactly one
instance under its own management, but it cannot see a process that is alive yet
wedged, nor a stray agent started outside the unit. This cron job closes both
gaps. Run order matters:

  1. Reap duplicates — keep only the systemd-managed instance: host-gated
     SIGKILL of any other live PRODOPS agent, found by its registry-saved
     pid+hostname (the same source the MCP swf_kill_agent uses — that is the
     interactive, per-dev shape; this is the autonomous, system-singleton shape).
     Never a process-name match.
  2. Liveness — with duplicates gone, ping the one remaining agent over the bus
     (health_ping -> pong). For a messaging service the message path *is* the
     health, so a process that is alive but not consuming still fails this. No
     pong -> restart the unit (a failed restart is an alarm).

Optional --prune-days N removes cached job logs older than N days (reclaimable;
a miss just re-fetches).

Standalone, no Django. MQ config from the same env vars BaseAgent uses
(ACTIVEMQ_*). Intended for cron. See docs/EPICPROD_OPS.md.
"""
import argparse
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

UNIT = os.environ.get("EPICPROD_OPS_UNIT", "epicprod-ops-agent")
OPS_QUEUE = os.environ.get("EPICPROD_OPS_QUEUE", "/queue/epicprod.ops")
SWF_TMP_DIR = os.environ.get("SWF_TMP_DIR", "/data/swf-tmp")
PING_TIMEOUT = int(os.environ.get("EPICPROD_PING_TIMEOUT", "10"))
# Monitor registry — the authoritative source of each agent's saved pid+hostname.
# SWF_MONITOR_URL comes from production.env (the truth), already carrying the
# /swf-monitor app path — used as-is, no appending.
MONITOR_URL = os.environ.get("SWF_MONITOR_URL", "").rstrip("/")
API_TOKEN = os.environ.get("SWF_API_TOKEN", "")
CA_BUNDLE = os.environ.get("REQUESTS_CA_BUNDLE") or True
HEARTBEAT_FRESH_SEC = int(os.environ.get("EPICPROD_HEARTBEAT_FRESH_SEC", "120"))
# Sentinel the agent exits with on a deliberate bus 'shutdown' (must match
# EXIT_DELIBERATE in epicprod_ops_agent.py and RestartPreventExitStatus in the
# unit). A unit whose last main exit was this is down on purpose — don't restart.
EXIT_DELIBERATE = int(os.environ.get("EPICPROD_EXIT_DELIBERATE", "100"))

log = logging.getLogger("prodops-cleaner-killer")


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


# -- 1. reap duplicates ------------------------------------------------------

def systemd_main_pid():
    """PID of the systemd-managed agent, or None if not running / unknown."""
    p = _run(["systemctl", "show", "-p", "MainPID", "--value", UNIT])
    if p.returncode != 0:
        log.error(f"systemctl show '{UNIT}' failed rc={p.returncode}: {p.stderr.strip()}")
        return None
    try:
        pid = int(p.stdout.strip())
    except ValueError:
        return None
    return pid or None


def _heartbeat_fresh(ts):
    """True if the registry heartbeat is recent enough that the process is really
    alive — so we never SIGKILL a pid that a dead agent left behind (and the OS
    may have reused)."""
    if not ts:
        return False
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() <= HEARTBEAT_FRESH_SEC


def registry_prodops():
    """Live PRODOPS records — each carries the pid+hostname the agent saved to the
    monitor on registration. Returns None if the registry can't be read."""
    if not MONITOR_URL or not API_TOKEN:
        log.error("reap: SWF_MONITOR_URL / SWF_API_TOKEN not set — cannot read registry")
        return None
    r = requests.get(f"{MONITOR_URL}/api/systemagents/",
                     headers={"Authorization": f"Token {API_TOKEN}"},
                     verify=CA_BUNDLE, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("results", data) if isinstance(data, dict) else data
    return [a for a in items if a.get("agent_type") == "PRODOPS"]


def reap_duplicates():
    """Keep only the systemd-managed instance alive. Find PRODOPS duplicates by
    their registry-saved pid (what swf_kill_agent uses), not a process-name match;
    host-gated SIGKILL of any live one whose pid is not the systemd MainPID."""
    main = systemd_main_pid()
    if main is None:
        log.warning(f"unit '{UNIT}' MainPID unknown — not reaping (letting systemd settle)")
        return
    try:
        agents = registry_prodops()
    except Exception as e:
        log.error(f"reap: registry read failed: {e}")
        return
    if agents is None:
        return
    this_host = socket.gethostname()
    killed = 0
    for a in agents:
        pid, host = a.get("pid"), a.get("hostname")
        if not pid or a.get("operational_state") == "EXITED":
            continue
        if not _heartbeat_fresh(a.get("last_heartbeat")):
            continue                                   # stale: process gone, leave the pid alone
        if pid == main:
            continue                                   # the systemd-managed instance
        if host != this_host:
            log.warning(f"reap: live PRODOPS '{a.get('instance_name')}' on {host} pid={pid} — "
                        f"cannot kill remotely; it may be stealing from the ops queue (ALARM)")
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
            log.warning(f"REAPED duplicate PRODOPS '{a.get('instance_name')}' pid={pid} "
                        f"(registry-saved; systemd main={main})")
        except ProcessLookupError:
            log.info(f"reap: '{a.get('instance_name')}' pid={pid} already gone")
        except PermissionError:
            log.error(f"reap: cannot kill pid={pid}: permission denied")
    if not killed:
        log.info(f"reap: systemd main pid {main} is the sole live PRODOPS on {this_host}")


# -- 2. liveness -------------------------------------------------------------

def liveness():
    try:
        import stomp
    except ImportError:
        log.error("stomp.py not available — cannot run liveness check")
        return

    host = os.environ.get("ACTIVEMQ_HOST", "localhost")
    port = int(os.environ.get("ACTIVEMQ_PORT", "61612"))
    user = os.environ.get("ACTIVEMQ_USER", "admin")
    password = os.environ.get("ACTIVEMQ_PASSWORD", "admin")
    use_ssl = os.environ.get("ACTIVEMQ_USE_SSL", "False").lower() == "true"
    ca_certs = os.environ.get("ACTIVEMQ_SSL_CA_CERTS", "")
    reply_q = f"/queue/epicprod.ops.pong.{socket.gethostname()}.{os.getpid()}"

    got = {"pong": False}

    class _Listener(stomp.ConnectionListener):
        def on_message(self, frame):
            got["pong"] = True

    # Mirror BaseAgent's connection footprint.
    conn = stomp.Connection(
        host_and_ports=[(host, port)], vhost=host,
        try_loopback_connect=False, heartbeats=(30000, 30000),
        auto_content_length=False,
    )
    if use_ssl and ca_certs:
        import ssl
        conn.transport.set_ssl(for_hosts=[(host, port)], ca_certs=ca_certs,
                               ssl_version=ssl.PROTOCOL_TLS_CLIENT)
    conn.set_listener("", _Listener())
    try:
        conn.connect(user, password, wait=True)
        conn.subscribe(destination=reply_q, id="pong", ack="auto")
        conn.send(destination=OPS_QUEUE,
                  body=json.dumps({"msg_type": "health_ping",
                                   "namespace": "prodops", "reply_to": reply_q}))
    except Exception as e:
        log.error(f"liveness: MQ connect/send failed: {e}")
        return

    deadline = time.time() + PING_TIMEOUT
    while time.time() < deadline and not got["pong"]:
        time.sleep(0.2)
    try:
        conn.disconnect()
    except Exception:
        pass

    if got["pong"]:
        log.info("liveness: pong received — agent healthy")
        return
    # No pong — but a deliberate shutdown (sentinel EXIT_DELIBERATE) means the
    # singleton is *meant* to be down; don't fight it.
    st = _run(["systemctl", "show", "-p", "ExecMainStatus", "--value", UNIT])
    if st.stdout.strip() == str(EXIT_DELIBERATE):
        log.info(f"liveness: no pong, but '{UNIT}' exited deliberately "
                 f"(code {EXIT_DELIBERATE}) — leaving it stopped")
        return
    log.warning(f"liveness: no pong in {PING_TIMEOUT}s — restarting unit '{UNIT}'")
    # reset-failed first: a unit that tripped the start-limit burst sits in
    # 'failed' and a bare restart is refused until the rate-limit state is cleared.
    _run(["sudo", "systemctl", "reset-failed", UNIT])
    r = _run(["sudo", "systemctl", "restart", UNIT])
    if r.returncode != 0:
        log.error(f"ALARM: restart of '{UNIT}' FAILED rc={r.returncode}: {r.stderr.strip()}")
    else:
        log.warning(f"restarted '{UNIT}'")


# -- 3. prune ----------------------------------------------------------------

def prune(days):
    root = os.path.join(SWF_TMP_DIR, "panda-logs")
    if not os.path.isdir(root):
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for task in os.listdir(root):
        tdir = os.path.join(root, task)
        if not os.path.isdir(tdir):
            continue
        for job in os.listdir(tdir):
            jdir = os.path.join(tdir, job)
            try:
                if os.path.isdir(jdir) and os.path.getmtime(jdir) < cutoff:
                    shutil.rmtree(jdir, ignore_errors=True)
                    removed += 1
            except OSError as e:
                log.error(f"prune: {jdir}: {e}")
    log.info(f"prune: removed {removed} cached job-log dir(s) older than {days}d")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s prodops-cleaner-killer %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    ap = argparse.ArgumentParser(
        description="Reap duplicate prod-ops agents and keep the singleton alive.")
    ap.add_argument("--prune-days", type=int, default=None,
                    help="also remove cached job logs older than N days")
    ap.add_argument("--no-liveness", action="store_true",
                    help="skip the MQ ping / restart (reap [+ prune] only)")
    a = ap.parse_args()

    reap_duplicates()
    if not a.no_liveness:
        liveness()
    if a.prune_days is not None:
        prune(a.prune_days)


if __name__ == "__main__":
    main()
