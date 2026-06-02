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
testbed-native event front end. Long-running doers (fetch, submit) run on
BaseAgent's worker pool via ``run_in_background`` so the single receiver thread
is never blocked; control messages (health_ping, shutdown) act inline.

This is a system-level singleton (not a per-user testbed agent). It runs under a
fixed 'prodops' namespace (from prodops.toml) so it is identifiable in the
monitor and every caller addresses it explicitly, and is managed by systemd like
the swf-*-bot units. The cleaner-killer cron reaps stale/duplicate instances and
keeps one alive.

Capabilities:
  fetch_payload_log  — retrieve + cache one PanDA job's payload log
                       (delegates to scripts/cache-payload-log.py).
  submit_task        — submit one PCS ProdTask to PanDA via prun, reusing the
                       cached production token (delegates to
                       scripts/submit-prod-task.py).
  health_ping        — liveness probe; replies 'pong' to reply_to.
  shutdown           — deliberate stop; exits EXIT_DELIBERATE so systemd leaves
                       the singleton down instead of restarting it.

See docs/EPICPROD_OPS.md.
"""
import json
import logging
import os
import signal
import subprocess
import sys
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

# The standalone doers, shipped alongside this agent.
FETCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache-payload-log.py"
SUBMIT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "submit-prod-task.py"

# Backstop on the prun submission subprocess (sandbox upload + JEDI insert).
SUBMIT_TIMEOUT = int(os.environ.get("EPICPROD_SUBMIT_TIMEOUT", "300"))

# Dedicated namespace config shipped beside the agent. A fixed 'prodops' namespace
# makes the singleton identifiable in the monitor and lets callers address it
# explicitly — every message to this agent carries namespace=prodops.
PRODOPS_CONFIG = Path(__file__).resolve().parent / "prodops.toml"

# Deliberate-shutdown sentinel: main() exits with this so systemd's
# RestartPreventExitStatus knows the stop was on purpose and must not restart.
# Distinct from 0 — a persistent agent exiting 0 unbidden is still a failure.
EXIT_DELIBERATE = 100


class EpicProdOpsAgent(BaseAgent):
    """Production operations agent — dispatches ops messages to handlers."""

    KNOWN_TYPES = {"fetch_payload_log", "submit_task", "health_ping", "shutdown"}

    def __init__(self):
        # System-level singleton (not a per-user testbed agent): its namespace is
        # the fixed 'prodops' from PRODOPS_CONFIG, so it is identifiable in the
        # monitor and callers address it explicitly. See docs/EPICPROD_OPS.md.
        super().__init__(agent_type="PRODOPS", subscription_queue=OPS_QUEUE,
                         config_path=str(PRODOPS_CONFIG))
        self._deliberate = False

    def on_message(self, frame):
        message_data, msg_type = self.log_received_message(frame, known_types=self.KNOWN_TYPES)
        if message_data is None:          # namespace-filtered — ignore
            return
        handler = getattr(self, f"_handle_{msg_type}", None)
        if handler is None:
            self.logger.warning(f"PRODOPS: no handler for msg_type '{msg_type}'")
            return
        # Handlers return immediately: control messages (health_ping/shutdown) act
        # inline; work messages (fetch_payload_log/submit_task) validate here and
        # enqueue their doer via run_in_background, so the receiver thread is never
        # blocked. The worker pool drives PROCESSING state — no processing() wrap.
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

    def _handle_shutdown(self, m):
        """Deliberate-shutdown back door. An operator (or controller) can ask the
        singleton to step down over the bus; we unwind through BaseAgent's normal
        SIGTERM path and main() then exits EXIT_DELIBERATE so systemd
        (RestartPreventExitStatus) leaves it stopped instead of restarting it.
        Distinct from `systemctl stop`, the host-level back door."""
        self.logger.warning(
            f"PRODOPS: deliberate shutdown requested by {m.get('sender', '?')}")
        self._deliberate = True
        os.kill(self.pid, signal.SIGTERM)   # reuse BaseAgent's graceful unwind

    def _handle_fetch_payload_log(self, m):
        """Validate, then run the fetch on the worker pool — it blocks on Rucio
        + xrootd. Deduped per job so two requests for the same job don't extract
        into one cache dir at once."""
        missing = [k for k in ("scope", "lfn", "jeditaskid", "pandaid") if not m.get(k)]
        if missing:
            self.logger.error(f"PRODOPS fetch_payload_log: missing fields {missing}")
            return
        self.run_in_background(
            self._do_fetch_payload_log, m,
            dedup_key=f"fetch:{m['jeditaskid']}:{m['pandaid']}",
            label=f"fetch_payload_log pandaid={m['pandaid']}")

    def _do_fetch_payload_log(self, m):
        """Fetch + cache one job's payload log via the standalone helper."""
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
            # Push completion to the browser via the SSE relay (rides the topic
            # the monitor consumes; the page matches on pandaid). See docs/SSE_PUSH.md.
            self.send_message('/topic/epictopic', {
                'msg_type': 'payload_log_ready',
                'pandaid': str(m['pandaid']),
                'jeditaskid': str(m['jeditaskid']),
            })

    def _handle_submit_task(self, m):
        """Validate, then run the submission on the worker pool. Deduped per
        task so two near-simultaneous triggers can't fire two submissions —
        the status / panda_task_id gates close that window later; this closes
        the in-flight window at the agent."""
        task_name = m.get("task_name")
        if not task_name:
            self.logger.error("PRODOPS submit_task: missing task_name")
            return
        self.run_in_background(
            self._do_submit_task, m,
            dedup_key=f"submit:{task_name}", label=f"submit_task {task_name}")

    def _do_submit_task(self, m):
        """Submit one PCS ProdTask to PanDA via the standalone doer.

        The web tier ('Submit to PanDA') publishes {task_name}; the doer
        fetches the prun command from PCS, runs it non-interactively with the
        cached long-lived production token, and records the jediTaskID back.
        We hold the token; the web tier structurally cannot. The outcome lands
        on the ProdTask (panda_task_id + status) via record-submission, which
        the UI reads — so, like fetch_payload_log, there is no bus reply."""
        task_name = m["task_name"]
        cmd = [sys.executable, str(SUBMIT_SCRIPT), "--task-name", str(task_name)]
        if m.get("owner"):          # X-Remote-User for the owner-gated record write
            cmd += ["--owner", str(m["owner"])]
        self.logger.info(f"PRODOPS submit_task: {task_name}")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBMIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(f"PRODOPS submit_task TIMEOUT after {SUBMIT_TIMEOUT}s: {task_name}")
            return
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  submit-prod-task: {line}")
        if p.returncode != 0:
            self.logger.error(f"PRODOPS submit_task FAILED rc={p.returncode}: {task_name}")
        else:
            jedi_task_id = (p.stdout or '').strip()
            self.logger.info(
                f"PRODOPS submit_task done: {task_name} -> jediTaskID={jedi_task_id}")
            # Push completion to the browser via the SSE relay. See docs/SSE_PUSH.md.
            if jedi_task_id:
                self.send_message('/topic/epictopic', {
                    'msg_type': 'prodtask_submitted',
                    'task_name': task_name,
                    'jedi_task_id': jedi_task_id,
                })

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
    agent = EpicProdOpsAgent()
    agent.run()
    # A deliberate bus 'shutdown' exits with the sentinel so systemd does not
    # restart it; any other exit is a failure and is restarted (burst-capped).
    sys.exit(EXIT_DELIBERATE if agent._deliberate else 0)


if __name__ == "__main__":
    main()
