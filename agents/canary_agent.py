#!/usr/bin/env python3
"""
Canary site-health agent.

An always-on agent built on the shared testbed agent infrastructure
(`swf_common_lib.base_agent.BaseAgent`) that runs the site-canary
assessment cycle on the platform host: the passive assessor over PanDA
accounting and the policy evaluator over stored evidence (site-canary
docs/SWF_INTEGRATION.md, bootstrap step 5). It grows the credentialed
canary capabilities to come — probe submission, queue-status actuation —
as handlers, in the prod-ops agent pattern
(swf-epicprod docs/EPICPROD_OPS_AGENT.md).

It is event-driven, not polled. Requests arrive as JSON messages on an
anycast control queue; each action is a `msg_type` dispatched to a
`_handle_<msg_type>` method. The doer is the site-canary CLI itself —
already a standalone committed tool — run as a bounded subprocess on
BaseAgent's worker pool via ``run_in_background`` so the single receiver
thread is never blocked. The hourly cadence is a cron-enqueued
`assess_refresh` (scripts/enqueue-ops-message.py); the same command by
hand is the on-demand trigger.

This is a system-level singleton (not a per-user testbed agent). It runs
under a fixed 'canary' namespace (from canary.toml) so it is identifiable
in the monitor and every caller addresses it explicitly, and is managed by
systemd like the other platform singletons.

Capabilities:
  assess_refresh — run `canary assess --panda --write` (per-queue passive
                   samples from PanDA accounting into the canary store),
                   then `canary evaluate --write` (policy verdicts and
                   health-state transitions over the stored evidence;
                   recorded, not actuated).
  health_ping    — liveness probe; replies 'pong' to reply_to.
  shutdown       — deliberate stop; exits EXIT_DELIBERATE so systemd
                   leaves the singleton down instead of restarting it.
"""
import json
import os
import signal
import subprocess
import sys
import time

from pathlib import Path

from swf_common_lib.base_agent import BaseAgent

# Anycast control queue: one consumer handles each request exactly once.
CANARY_QUEUE = os.environ.get("CANARY_OPS_QUEUE", "/queue/canary.ops")

# Backstops on the doer subprocesses. The assessor is one bounded PanDA
# accounting query plus store writes; the evaluator reads the store only.
ASSESS_TIMEOUT = int(os.environ.get("CANARY_ASSESS_TIMEOUT", "300"))
EVALUATE_TIMEOUT = int(os.environ.get("CANARY_EVALUATE_TIMEOUT", "120"))

# Dedicated namespace config shipped beside the agent. A fixed 'canary'
# namespace makes the singleton identifiable in the monitor and lets callers
# address it explicitly — every message to this agent carries namespace=canary.
CANARY_CONFIG = Path(__file__).resolve().parent / "canary.toml"

# Deliberate-shutdown sentinel: main() exits with this so systemd's
# RestartPreventExitStatus knows the stop was on purpose and must not restart.
# Distinct from 0 — a persistent agent exiting 0 unbidden is still a failure.
EXIT_DELIBERATE = 100


class CanaryAgent(BaseAgent):
    """Canary site-health agent — dispatches canary messages to handlers."""

    KNOWN_TYPES = {"assess_refresh", "health_ping", "shutdown"}

    def __init__(self):
        # System-level singleton (not a per-user testbed agent): its namespace
        # is the fixed 'canary' from CANARY_CONFIG, so it is identifiable in
        # the monitor and callers address it explicitly.
        super().__init__(agent_type="CANARY", subscription_queue=CANARY_QUEUE,
                         config_path=str(CANARY_CONFIG))
        self._deliberate = False

    def on_message(self, frame):
        message_data, msg_type = self.log_received_message(frame, known_types=self.KNOWN_TYPES)
        if message_data is None:          # namespace-filtered — ignore
            return
        handler = getattr(self, f"_handle_{msg_type}", None)
        if handler is None:
            self.logger.warning(f"CANARY: no handler for msg_type '{msg_type}'")
            return
        # Handlers return immediately: control messages act inline; work
        # messages validate here and enqueue their doer via run_in_background,
        # so the receiver thread is never blocked. The worker pool drives
        # PROCESSING state — no processing() wrap.
        try:
            handler(message_data)
        except Exception as e:
            self.logger.error(f"CANARY: handler '{msg_type}' raised: {e}")

    # -- handlers ------------------------------------------------------------

    def _handle_health_ping(self, m):
        """Liveness probe: reply 'pong' to the caller's reply_to queue."""
        reply_to = m.get("reply_to")
        if not reply_to:
            self.logger.warning("CANARY health_ping: no reply_to, dropping")
            return
        pong = {"msg_type": "pong", "agent": self.agent_name, "pid": self.pid}
        self.conn.send(destination=reply_to, body=json.dumps(pong))
        self.logger.info(f"CANARY health_ping -> pong to {reply_to}")

    def _handle_shutdown(self, m):
        """Deliberate-shutdown back door: unwind through BaseAgent's normal
        SIGTERM path; main() then exits EXIT_DELIBERATE so systemd
        (RestartPreventExitStatus) leaves it stopped instead of restarting."""
        self.logger.warning(
            f"CANARY: deliberate shutdown requested by {m.get('sender', '?')}")
        self._deliberate = True
        os.kill(self.pid, signal.SIGTERM)   # reuse BaseAgent's graceful unwind

    def _handle_assess_refresh(self, m):
        """Enqueue one assessment cycle on the worker pool — it blocks on the
        PanDA accounting query. Deduped so overlapping triggers (cron plus an
        on-demand run) never run two cycles at once."""
        self.run_in_background(
            self._do_assess_refresh, m,
            dedup_key="assess_refresh", label="assess_refresh")

    def _do_assess_refresh(self, m):
        """Run the site-canary CLI doers: assess (write passive samples), then
        evaluate (record verdicts and health-state transitions)."""
        created_by = str(m.get("created_by") or "?")
        self.logger.info(f"CANARY assess_refresh: starting (by {created_by})")
        t0 = time.monotonic()
        if not self._run_doer(["assess", "--panda", "--write"], ASSESS_TIMEOUT):
            self._emit_complete(ok=False, stage="assess")
            return
        if not self._run_doer(["evaluate", "--write"], EVALUATE_TIMEOUT):
            self._emit_complete(ok=False, stage="evaluate")
            return
        elapsed = time.monotonic() - t0
        self.logger.info(f"CANARY assess_refresh done in {elapsed:.1f}s")
        self._emit_complete(ok=True)

    # -- doers ---------------------------------------------------------------

    def _run_doer(self, canary_args, timeout):
        """Run one site-canary CLI subprocess, bounded; relay its output and
        surface every failure. Returns True on success."""
        cmd = [sys.executable, "-m", "canary"] + canary_args
        name = canary_args[0]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.logger.error(f"CANARY {name} TIMEOUT after {timeout}s")
            return False
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  canary {name}: {line}")
        if p.returncode != 0:
            tail = (p.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
            self.logger.error(
                f"CANARY {name} FAILED rc={p.returncode}: {tail[0]}")
            return False
        return True

    def _emit_complete(self, ok, stage=None):
        """Publish the cycle outcome to the SSE topic so pages can refresh
        live; a failed stage is named, never silent."""
        event = {"msg_type": "canary_assess_complete", "ok": ok}
        if stage:
            event["failed_stage"] = stage
        self.send_message('/topic/epictopic', event)


def main():
    agent = CanaryAgent()
    agent.run()
    # A deliberate bus 'shutdown' exits with the sentinel so systemd does not
    # restart it; any other exit is a failure and is restarted (burst-capped).
    sys.exit(EXIT_DELIBERATE if agent._deliberate else 0)


if __name__ == "__main__":
    main()
