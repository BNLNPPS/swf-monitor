#!/usr/bin/env python3
"""
ePIC production operations agent.

An always-on agent built on the shared testbed agent infrastructure
(`swf_common_lib.base_agent.BaseAgent`) that performs the credentialed
production-operations actions the web tier structurally cannot: it runs as
`wenauseic`, so it holds the Rucio proxy and can drive xrootd.

It is event-driven, not polled. Requests arrive as JSON messages on an anycast
control queue (handled once by the single consumer). Each action is a
`msg_type` dispatched to a `_handle_<msg_type>` method, so growing the agent =
adding a handler. The actual work is delegated to standalone scripts (the
"doers"), keeping each capability usable on its own and the agent a thin,
testbed-native event front end.

This is a system-level singleton (not a per-user testbed agent), so it runs
namespace-less and is managed by systemd like the swf-*-bot units. The
cleaner-killer cron reaps stale/duplicate instances and keeps one alive.

Capabilities:
  fetch_payload_log  — retrieve + cache one PanDA job's payload log
                       (delegates to scripts/cache-payload-log.py).
  health_ping        — liveness probe; replies 'pong' to reply_to.

See docs/EPICPROD_OPS.md.
"""
import json
import logging
import os
import subprocess
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from swf_common_lib.base_agent import BaseAgent

# Anycast control queue: one consumer handles each request exactly once.
OPS_QUEUE = os.environ.get("EPICPROD_OPS_QUEUE", "/queue/epicprod.ops")

# Managed scratch/cache root (shared with the doer and the web view).
SWF_TMP_DIR = os.environ.get("SWF_TMP_DIR", "/data/swf-tmp")

# Hard backstop on the doer subprocess. Longer than the doer's own xrdcp timeout
# so the doer fails first and cleanly; this only catches a wholly-wedged run.
FETCH_TIMEOUT = int(os.environ.get("EPICPROD_FETCH_TIMEOUT", "180"))

# Failure marker written into the cache dir; read by the web view to surface the
# error and bound retries. A later success replaces the whole dir, clearing it.
ERROR_MARKER = ".error"

# The standalone doer, shipped alongside this agent.
FETCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache-payload-log.py"


class EpicProdOpsAgent(BaseAgent):
    """Production operations agent — dispatches ops messages to handlers."""

    KNOWN_TYPES = {"fetch_payload_log", "health_ping"}

    def __init__(self):
        super().__init__(agent_type="PRODOPS", subscription_queue=OPS_QUEUE)
        # System-level singleton, not a per-user testbed agent: run namespace-less
        # so it never inherits (or "lands in") a developer namespace, and so it
        # processes ops requests from any caller. See docs/EPICPROD_OPS.md.
        self.namespace = None

    def on_message(self, frame):
        message_data, msg_type = self.log_received_message(frame, known_types=self.KNOWN_TYPES)
        if message_data is None:          # namespace-filtered — ignore
            return
        handler = getattr(self, f"_handle_{msg_type}", None)
        if handler is None:
            self.logger.warning(f"PRODOPS: no handler for msg_type '{msg_type}'")
            return
        # health_ping is a liveness probe, not work — don't flip operational state.
        ctx = nullcontext() if msg_type == "health_ping" else self.processing()
        with ctx:
            try:
                handler(message_data)
            except Exception as e:
                self.logger.error(f"PRODOPS: handler '{msg_type}' raised: {e}")

    # -- handlers ------------------------------------------------------------

    def _handle_health_ping(self, m):
        """Liveness probe: reply 'pong' to the caller's reply_to queue.

        The cleaner-killer cron pings over the bus — for a messaging service the
        message path *is* the health — and restarts the unit if no pong arrives.
        Replies via conn.send directly, mirroring the agent-manager's ping reply.
        """
        reply_to = m.get("reply_to")
        if not reply_to:
            self.logger.warning("PRODOPS health_ping: no reply_to, dropping")
            return
        pong = {"msg_type": "pong", "agent": self.agent_name, "pid": self.pid}
        self.conn.send(destination=reply_to, body=json.dumps(pong))
        self.logger.info(f"PRODOPS health_ping -> pong to {reply_to}")

    def _handle_fetch_payload_log(self, m):
        """Fetch + cache one job's payload log via the standalone helper."""
        missing = [k for k in ("scope", "lfn", "jeditaskid", "pandaid") if not m.get(k)]
        if missing:
            self.logger.error(f"PRODOPS fetch_payload_log: missing fields {missing}")
            return
        jobdir = os.path.join(SWF_TMP_DIR, "panda-logs", str(m["jeditaskid"]), str(m["pandaid"]))
        cmd = [
            sys.executable, str(FETCH_SCRIPT),
            "--scope", str(m["scope"]),
            "--lfn", str(m["lfn"]),
            "--jeditaskid", str(m["jeditaskid"]),
            "--pandaid", str(m["pandaid"]),
        ]
        if m.get("force"):           # operator override: re-fetch even if cached
            cmd.append("--force")
        self.logger.info(f"PRODOPS fetch_payload_log: pandaid={m['pandaid']} task={m['jeditaskid']}")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS fetch_payload_log TIMEOUT after {FETCH_TIMEOUT}s pandaid={m['pandaid']}")
            self._mark_error(jobdir, f"fetch timed out after {FETCH_TIMEOUT}s")
            return
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  cache-payload-log: {line}")
        if p.returncode != 0:
            stderr = (p.stderr or "").strip()
            reason = stderr.splitlines()[-1] if stderr else f"rc={p.returncode}"
            self.logger.error(
                f"PRODOPS fetch_payload_log FAILED rc={p.returncode} pandaid={m['pandaid']}")
            self._mark_error(jobdir, reason)
        else:
            self.logger.info(f"PRODOPS fetch_payload_log done: pandaid={m['pandaid']}")

    # -- helpers -------------------------------------------------------------

    def _mark_error(self, jobdir, reason):
        """Record a failed fetch in the cache dir (attempt count + reason) so the
        web view can surface it and stop auto-retrying past the cap."""
        try:
            os.makedirs(jobdir, exist_ok=True)
            path = os.path.join(jobdir, ERROR_MARKER)
            attempts = 0
            if os.path.isfile(path):
                try:
                    with open(path) as f:
                        attempts = int(json.load(f).get("attempts", 0))
                except (ValueError, OSError):
                    attempts = 0
            with open(path, "w") as f:
                json.dump({"attempts": attempts + 1, "last_error": reason,
                           "ts": datetime.now(timezone.utc).isoformat()}, f)
        except OSError as e:
            self.logger.error(f"PRODOPS could not write {ERROR_MARKER} in {jobdir}: {e}")


def main():
    EpicProdOpsAgent().run()


if __name__ == "__main__":
    main()
