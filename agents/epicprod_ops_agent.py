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
  rucio_snapshot_update — refresh the JLab Rucio output snapshot for the current
                       (+last) campaign and rematch produced datasets onto each
                       task's overrides['outputs'] (delegates to
                       scripts/rucio-snapshot-update.py).
  evgen_rucio_update — assimilate the JLab Rucio EVGEN inventory (epic:/EVGEN/*)
                       and resolve each PCS evgen Dataset onto metadata['rucio']
                       (delegates to scripts/import_evgen_rucio.py --apply).
  campaign_progress_refresh — rebuild current campaign progress data and its
                       rendered progress table cache.
  panda_task_operation — run a PanDA-native operation on an existing JEDI task:
                       increase allowed attempts or retry failed work.
  sync_epicprod_inventory — refresh the monitor's ePIC production job/file
                       inventory and parsed failure diagnosis for a PanDA job.
  refresh_system_status — refresh cached System status rows for services,
                       agents, and external monitor endpoints.
  association_sweep  — batch-associate recent PanDA tasks with PCS campaign
                       tasks (manage.py sweep_panda_associations).
  catalog_sync       — the nightly composite: association sweep, Rucio
                       snapshot, EVGEN assimilation, questionnaire match,
                       progress refresh, in order; logs the chain summary
                       that serves as the catalog-freshness timestamp.
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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from swf_common_lib.base_agent import BaseAgent

# Anycast control queue: one consumer handles each request exactly once.
OPS_QUEUE = os.environ.get("EPICPROD_OPS_QUEUE", "/queue/epicprod.ops")

# Monitor REST base for the epicprod action stream (same endpoint the shared
# REST log handler posts to).
MONITOR_HTTP_URL = os.environ.get("SWF_MONITOR_HTTP_URL", "http://localhost:8002")
ACTION_LOG_TIMEOUT = int(os.environ.get("EPICPROD_ACTION_LOG_TIMEOUT", "5"))

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

# Client-API EVGEN submission doer (sidelines prun): assembles the sandbox
# (manifest + env + dispatcher + JLab proxy) and submits noInput+noOutput.
SUBMIT_EVGEN_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "submit-evgen-task.py"
SUBMIT_EVGEN_TIMEOUT = int(os.environ.get("EPICPROD_SUBMIT_EVGEN_TIMEOUT", "300"))
PANDA_TASK_OPERATION_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "panda-task-operation.py"
PANDA_TASK_OPERATION_TIMEOUT = int(os.environ.get("EPICPROD_PANDA_TASK_OPERATION_TIMEOUT", "120"))

# Update-from-Rucio doer: a live JLab Rucio fetch (current + last campaign) plus
# the per-task rematch — slow and network-bound, so generously bounded.
RUCIO_SNAPSHOT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rucio-snapshot-update.py"
RUCIO_SNAPSHOT_TIMEOUT = int(os.environ.get("EPICPROD_RUCIO_SNAPSHOT_TIMEOUT", "900"))
# Arrivals sweep: one created_after DID query per root — light and bounded.
RUCIO_ARRIVALS_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rucio-arrivals-sweep.py"
RUCIO_ARRIVALS_TIMEOUT = int(os.environ.get("EPICPROD_RUCIO_ARRIVALS_TIMEOUT", "300"))
CATALOG_IMPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pcs-catalog-import.py"
CATALOG_IMPORT_TIMEOUT = int(os.environ.get("EPICPROD_CATALOG_IMPORT_TIMEOUT", "1800"))
QUESTIONNAIRE_MATCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update-questionnaire-matches.py"
QUESTIONNAIRE_MATCH_TIMEOUT = int(os.environ.get("EPICPROD_QUESTIONNAIRE_MATCH_TIMEOUT", "300"))
QUESTIONNAIRE_IMPORT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import-questionnaires.py"
QUESTIONNAIRE_AUTOMATCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "match-questionnaires.py"
QUESTIONNAIRE_AUTOMATCH_TIMEOUT = int(os.environ.get("EPICPROD_AUTOMATCH_TIMEOUT", "1800"))
QUESTIONNAIRE_IMPORT_TIMEOUT = int(os.environ.get("EPICPROD_QUESTIONNAIRE_IMPORT_TIMEOUT", "120"))
CAMPAIGN_PROGRESS_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "refresh-campaign-progress.py"
CAMPAIGN_PROGRESS_TIMEOUT = int(os.environ.get("EPICPROD_CAMPAIGN_PROGRESS_TIMEOUT", "300"))
CAMPAIGN_PROGRESS_MIN_INTERVAL = int(os.environ.get("EPICPROD_CAMPAIGN_PROGRESS_MIN_INTERVAL", "300"))

# EVGEN-input assimilation doer: a live JLab Rucio fetch of epic:/EVGEN/* plus
# the per-dataset match — slow and network-bound, so generously bounded.
EVGEN_RUCIO_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_evgen_rucio.py"
EVGEN_RUCIO_TIMEOUT = int(os.environ.get("EPICPROD_EVGEN_RUCIO_TIMEOUT", "900"))

# Inventory refresh doer: a Django management command that reads PanDA/log
# evidence and writes monitor-side EpicProdJob/EpicProdFile rows.
MANAGE_PY = Path(__file__).resolve().parent.parent / "src" / "manage.py"
INVENTORY_TIMEOUT = int(os.environ.get("EPICPROD_INVENTORY_TIMEOUT", "180"))

# Association sweep: batch counterpart of the lazy per-view reconciliation —
# pulls directly submitted PanDA tasks into the catalog (management command).
ASSOCIATION_SWEEP_TIMEOUT = int(os.environ.get("EPICPROD_ASSOCIATION_SWEEP_TIMEOUT", "600"))
ASSOCIATION_SWEEP_DAYS = int(os.environ.get("EPICPROD_ASSOCIATION_SWEEP_DAYS", "14"))

# System status refresh doer: cached monitor/system rows, intentionally a
# standalone script rather than a Django management command.
SYSTEM_STATUS_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "refresh-system-status.py"
SYSTEM_STATUS_TIMEOUT = int(os.environ.get("EPICPROD_SYSTEM_STATUS_TIMEOUT", "60"))
SYSTEM_STATUS_INTERVAL = int(os.environ.get("EPICPROD_SYSTEM_STATUS_INTERVAL", "300"))
SYSTEM_STATUS_INITIAL_DELAY = int(os.environ.get("EPICPROD_SYSTEM_STATUS_INITIAL_DELAY", "30"))

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

    KNOWN_TYPES = {"fetch_payload_log", "submit_task", "submit_evgen_task",
                   "panda_task_operation",
                   "rucio_snapshot_update", "evgen_rucio_update", "catalog_import",
                   "questionnaire_match_update", "campaign_progress_refresh",
                   "association_sweep", "catalog_sync", "questionnaire_import",
                   "questionnaire_automatch",
                   "sync_epicprod_inventory", "refresh_system_status",
                   "health_ping", "shutdown"}

    def __init__(self):
        # System-level singleton (not a per-user testbed agent): its namespace is
        # the fixed 'prodops' from PRODOPS_CONFIG, so it is identifiable in the
        # monitor and callers address it explicitly. See docs/EPICPROD_OPS.md.
        super().__init__(agent_type="PRODOPS", subscription_queue=OPS_QUEUE,
                         config_path=str(PRODOPS_CONFIG))
        self._deliberate = False
        self._campaign_progress_last_start = 0
        self._action_log_session = requests.Session()
        self._system_status_thread = threading.Thread(
            target=self._system_status_periodic_loop,
            name="system-status-refresh",
            daemon=True,
        )
        self._system_status_thread.start()

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
        self._log_action('agent_shutdown', username=str(m.get('sender') or ''),
                         sublevel='high', live_default=True, level=logging.WARNING)
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
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS fetch_payload_log TIMEOUT after {FETCH_TIMEOUT}s pandaid={m['pandaid']}")
            self._mark_error(jobdir, f"fetch timed out after {FETCH_TIMEOUT}s")
            self._log_action('payload_log_fetch', t0, outcome='timeout',
                             reason=f'timed out after {FETCH_TIMEOUT}s',
                             subject_type='panda_job', subject_key=str(m['pandaid']),
                             username=str(m.get('requested_by') or ''),
                             sublevel='normal',
                             level=logging.ERROR, jeditaskid=str(m['jeditaskid']))
            return
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  cache-payload-log: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(
                f"PRODOPS fetch_payload_log FAILED rc={p.returncode} pandaid={m['pandaid']}")
            self._mark_error(jobdir, reason)
            self._log_action('payload_log_fetch', t0, outcome='error',
                             reason=reason,
                             subject_type='panda_job', subject_key=str(m['pandaid']),
                             username=str(m.get('requested_by') or ''),
                             sublevel='normal',
                             level=logging.ERROR, jeditaskid=str(m['jeditaskid']))
        else:
            self._log_action('payload_log_fetch', t0,
                             subject_type='panda_job', subject_key=str(m['pandaid']),
                             username=str(m.get('requested_by') or ''),
                             sublevel='normal',
                             jeditaskid=str(m['jeditaskid']))
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
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBMIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(f"PRODOPS submit_task TIMEOUT after {SUBMIT_TIMEOUT}s: {task_name}")
            # NO silent failure: tell the waiting page the submission timed out.
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submit_failed', 'task_name': task_name,
                'reason': f'submission timed out after {SUBMIT_TIMEOUT}s'})
            self._log_action('task_submit', t0, outcome='timeout',
                             reason=f'timed out after {SUBMIT_TIMEOUT}s',
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)
            return
        stderr = p.stderr or ""
        for line in stderr.splitlines():
            self.logger.info(f"  submit-prod-task: {line}")
        jedi_task_id = (p.stdout or '').strip()
        reason = self._derive_reason(p)
        # Every outcome emits an event over the SSE relay (docs/SSE_PUSH.md) so the
        # compose page is never left polling a submission that already resolved.
        if p.returncode == 0 and jedi_task_id:
            self.logger.info(
                f"PRODOPS submit_task done: {task_name} -> jediTaskID={jedi_task_id}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submitted',
                'task_name': task_name, 'jedi_task_id': jedi_task_id})
            self._log_action('task_submit', t0,
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, jedi_task_id=jedi_task_id)
        elif p.returncode == 7 and jedi_task_id:
            # Submitted to PanDA, but the record-back POST failed after retries —
            # an orphan. Surface the id; the task stays ready/unrecorded and
            # record-submission is idempotent, so it can be re-recorded.
            self.logger.error(
                f"PRODOPS submit_task UNRECORDED: {task_name} submitted as "
                f"jediTaskID={jedi_task_id} but record-back failed")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submit_unrecorded', 'task_name': task_name,
                'jedi_task_id': jedi_task_id, 'reason': reason})
            self._log_action('task_submit', t0, outcome='unrecorded',
                             reason=reason,
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR,
                             jedi_task_id=jedi_task_id)
        else:
            self.logger.error(f"PRODOPS submit_task FAILED rc={p.returncode}: {task_name}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submit_failed',
                'task_name': task_name, 'reason': reason})
            self._log_action('task_submit', t0, outcome='error',
                             reason=reason,
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)

    def _handle_submit_evgen_task(self, m):
        """Validate, then run the client-API EVGEN submission on the worker
        pool. This is the live Submit-button path (prun is sidelined). Deduped
        per task so two near-simultaneous triggers can't fire two submissions."""
        task_name = m.get("task_name")
        if not task_name:
            self.logger.error("PRODOPS submit_evgen_task: missing task_name")
            return
        self.run_in_background(
            self._do_submit_evgen_task, m,
            dedup_key=f"submit:{task_name}", label=f"submit_evgen_task {task_name}")

    def _do_submit_evgen_task(self, m):
        """Submit one PCS ProdTask to PanDA via the client-API EVGEN doer.

        The doer fetches the EVGEN spec from PCS, assembles the sandbox
        (manifest + env + dispatcher + JLab proxy), and submits noInput+noOutput
        with the cached production token; the payload stages its EVGEN input and
        self-registers RECO to JLab Rucio. Outcome lands on the ProdTask
        (panda_task_id + status) via record-submission, and every outcome emits
        the same SSE events as the prun path, so the compose page is unchanged
        and never left polling. The JLab proxy + monitor URL/token travel in the
        agent's environment (SWF_MONITOR_URL, SWFMON_TOKEN, EVGEN_X509_PROXY)."""
        task_name = m["task_name"]
        cmd = [sys.executable, str(SUBMIT_EVGEN_SCRIPT), "--task-name", str(task_name)]
        if m.get("panda_tasks_id"):
            cmd += ["--panda-tasks-id", str(m["panda_tasks_id"])]
        if m.get("owner"):          # X-Remote-User for the owner-gated record write
            cmd += ["--owner", str(m["owner"])]
        self.logger.info(f"PRODOPS submit_evgen_task: {task_name}")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBMIT_EVGEN_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(f"PRODOPS submit_evgen_task TIMEOUT after {SUBMIT_EVGEN_TIMEOUT}s: {task_name}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submit_failed', 'task_name': task_name,
                'reason': f'submission timed out after {SUBMIT_EVGEN_TIMEOUT}s'})
            self._log_action('evgen_task_submit', t0, outcome='timeout',
                             reason=f'timed out after {SUBMIT_EVGEN_TIMEOUT}s',
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)
            return
        stderr = p.stderr or ""
        for line in stderr.splitlines():
            self.logger.info(f"  submit-evgen-task: {line}")
        jedi_task_id = (p.stdout or '').strip()
        reason = self._derive_reason(p)
        if p.returncode == 0 and jedi_task_id:
            self.logger.info(
                f"PRODOPS submit_evgen_task done: {task_name} -> jediTaskID={jedi_task_id}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submitted',
                'task_name': task_name, 'jedi_task_id': jedi_task_id})
            self._log_action('evgen_task_submit', t0,
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, jedi_task_id=jedi_task_id)
        elif p.returncode == 7 and jedi_task_id:
            # Submitted, but the record-back POST failed after retries — an
            # orphan; surface the id (record-submission is idempotent).
            self.logger.error(
                f"PRODOPS submit_evgen_task UNRECORDED: {task_name} submitted as "
                f"jediTaskID={jedi_task_id} but record-back failed")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submit_unrecorded', 'task_name': task_name,
                'jedi_task_id': jedi_task_id, 'reason': reason})
            self._log_action('evgen_task_submit', t0, outcome='unrecorded',
                             reason=reason,
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR,
                             jedi_task_id=jedi_task_id)
        else:
            self.logger.error(f"PRODOPS submit_evgen_task FAILED rc={p.returncode}: {task_name}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'prodtask_submit_failed',
                'task_name': task_name, 'reason': reason})
            self._log_action('evgen_task_submit', t0, outcome='error',
                             reason=reason,
                             subject_type='campaign_task', subject_key=task_name,
                             username=str(m.get('owner') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)

    def _handle_panda_task_operation(self, m):
        """Run a PanDA-native operation on an existing JEDI task."""
        operation = m.get("operation")
        task_name = m.get("task_name")
        jedi_task_id = m.get("jedi_task_id")
        if operation not in ("increase_attempts", "retry_failures"):
            self.logger.error(f"PRODOPS panda_task_operation: bad operation {operation!r}")
            return
        if not task_name or not jedi_task_id:
            self.logger.error("PRODOPS panda_task_operation: missing task_name/jedi_task_id")
            return
        self.run_in_background(
            self._do_panda_task_operation, m,
            dedup_key=f"panda-op:{operation}:{jedi_task_id}",
            label=f"panda_task_operation {operation} {jedi_task_id}")

    def _do_panda_task_operation(self, m):
        operation = m["operation"]
        task_name = str(m["task_name"])
        jedi_task_id = str(m["jedi_task_id"])
        cmd = [
            sys.executable, str(PANDA_TASK_OPERATION_SCRIPT),
            "--operation", operation,
            "--jedi-task-id", jedi_task_id,
            "--timeout", str(PANDA_TASK_OPERATION_TIMEOUT),
        ]
        if operation == "increase_attempts":
            cmd += ["--increase", str(m.get("increase") or 1)]
        if operation == "retry_failures" and m.get("new_parameters"):
            cmd += ["--new-parameters", json.dumps(m["new_parameters"])]

        self.logger.info(
            f"PRODOPS panda_task_operation: {operation} task={task_name} "
            f"jediTaskID={jedi_task_id}")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=PANDA_TASK_OPERATION_TIMEOUT + 30)
        except subprocess.TimeoutExpired:
            reason = f'operation timed out after {PANDA_TASK_OPERATION_TIMEOUT}s'
            self.logger.error(
                f"PRODOPS panda_task_operation TIMEOUT: {operation} {jedi_task_id}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'panda_task_operation_done',
                'task_name': task_name, 'jedi_task_id': jedi_task_id,
                'operation': operation, 'ok': False, 'error': reason})
            self._log_action('panda_task_operation', t0, outcome='timeout',
                             reason=reason,
                             subject_type='panda_task', subject_key=jedi_task_id,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR,
                             operation=operation, task_name=task_name)
            return

        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  panda-task-operation: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  panda-task-operation: {line}")
        if p.returncode == 0:
            summary = (p.stdout or "").strip()
            self.logger.info(
                f"PRODOPS panda_task_operation done: {operation} {jedi_task_id}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'panda_task_operation_done',
                'task_name': task_name, 'jedi_task_id': jedi_task_id,
                'operation': operation, 'ok': True, 'summary': summary})
            self._log_action('panda_task_operation', t0,
                             subject_type='panda_task', subject_key=jedi_task_id,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True,
                             operation=operation, task_name=task_name)
        else:
            reason = self._derive_reason(p)
            self.logger.error(
                f"PRODOPS panda_task_operation FAILED rc={p.returncode}: "
                f"{operation} {jedi_task_id}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'panda_task_operation_done',
                'task_name': task_name, 'jedi_task_id': jedi_task_id,
                'operation': operation, 'ok': False, 'error': reason})
            self._log_action('panda_task_operation', t0, outcome='error',
                             reason=reason,
                             subject_type='panda_task', subject_key=jedi_task_id,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR,
                             operation=operation, task_name=task_name)

    def _handle_sync_epicprod_inventory(self, m):
        """Refresh one job/task inventory record on the worker pool."""
        if not (m.get("pandaid") or m.get("jeditaskid") or m.get("task_name")):
            self.logger.error(
                "PRODOPS sync_epicprod_inventory: need pandaid, jeditaskid, or task_name")
            return
        if m.get("pandaid"):
            key = f"pandaid:{m['pandaid']}"
            label = f"sync_epicprod_inventory pandaid={m['pandaid']}"
        elif m.get("jeditaskid"):
            key = f"jeditaskid:{m['jeditaskid']}"
            label = f"sync_epicprod_inventory jeditaskid={m['jeditaskid']}"
        else:
            key = f"task:{m['task_name']}"
            label = f"sync_epicprod_inventory task={m['task_name']}"
        self.run_in_background(
            self._do_sync_epicprod_inventory, m,
            dedup_key=f"sync_inventory:{key}",
            label=label)

    def _do_sync_epicprod_inventory(self, m):
        cmd = [sys.executable, str(MANAGE_PY), "sync_epicprod_inventory"]
        if m.get("pandaid"):
            cmd += ["--pandaid", str(m["pandaid"])]
        elif m.get("jeditaskid"):
            cmd += ["--jeditaskid", str(m["jeditaskid"])]
        else:
            cmd += ["--prod-task", str(m["task_name"])]
        self.logger.info(f"PRODOPS sync_epicprod_inventory: {' '.join(cmd)}")
        if m.get('pandaid'):
            subj_type, subj_key = 'panda_job', str(m['pandaid'])
        elif m.get('jeditaskid'):
            subj_type, subj_key = 'panda_task', str(m['jeditaskid'])
        else:
            subj_type, subj_key = 'campaign_task', str(m['task_name'])
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=INVENTORY_TIMEOUT)
        except subprocess.TimeoutExpired:
            reason = f"timed out after {INVENTORY_TIMEOUT}s"
            self.logger.error(f"PRODOPS sync_epicprod_inventory TIMEOUT: {reason}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'epicprod_inventory_ready', 'ok': False,
                'pandaid': m.get('pandaid'), 'jeditaskid': m.get('jeditaskid'),
                'task_name': m.get('task_name'), 'error': reason})
            self._log_action('inventory_sync', t0, outcome='timeout',
                             reason=reason,
                             subject_type=subj_type, subject_key=subj_key,
                             level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  sync-epicprod-inventory: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  sync-epicprod-inventory: {line}")
        ok = p.returncode == 0
        reason = ''
        if not ok:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS sync_epicprod_inventory FAILED rc={p.returncode}")
        else:
            self.logger.info("PRODOPS sync_epicprod_inventory done")
        self.send_message('/topic/epictopic', {
            'msg_type': 'epicprod_inventory_ready', 'ok': ok,
            'pandaid': m.get('pandaid'), 'jeditaskid': m.get('jeditaskid'),
            'task_name': m.get('task_name'), 'error': reason})
        self._log_action('inventory_sync', t0,
                         outcome='ok' if ok else 'error',
                         reason=reason,
                         subject_type=subj_type, subject_key=subj_key,
                         level=logging.INFO if ok else logging.ERROR)

    def _handle_refresh_system_status(self, m):
        """Refresh cached system status rows through the shared doer."""
        self.run_in_background(
            self._do_refresh_system_status, m,
            dedup_key="refresh_system_status",
            label="refresh_system_status")

    def _do_refresh_system_status(self, m):
        cmd = [
            sys.executable, str(SYSTEM_STATUS_SCRIPT),
            "--source", str(m.get("source") or "ops_agent"),
        ]
        selected = m.get("selected") or m.get("only") or []
        if isinstance(selected, str):
            selected = [selected]
        for name in selected:
            cmd += ["--only", str(name)]
        self.logger.info(f"PRODOPS refresh_system_status: {' '.join(cmd)}")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SYSTEM_STATUS_TIMEOUT)
        except subprocess.TimeoutExpired:
            reason = f"timed out after {SYSTEM_STATUS_TIMEOUT}s"
            self.logger.error(f"PRODOPS refresh_system_status TIMEOUT: {reason}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'system_status_ready', 'ok': False, 'error': reason})
            self._log_action('system_status_refresh', t0, outcome='timeout',
                             reason=reason,
                             level=logging.ERROR,
                             source=str(m.get('source') or 'ops_agent'))
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  refresh-system-status: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  refresh-system-status: {line}")
        ok = p.returncode == 0
        reason = ''
        if not ok:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS refresh_system_status FAILED rc={p.returncode}")
        else:
            self.logger.info("PRODOPS refresh_system_status done")
        self.send_message('/topic/epictopic', {
            'msg_type': 'system_status_ready', 'ok': ok, 'error': reason})
        self._log_action('system_status_refresh', t0,
                         outcome='ok' if ok else 'error',
                         reason=reason,
                         level=logging.INFO if ok else logging.ERROR,
                         source=str(m.get('source') or 'ops_agent'))

    def _system_status_periodic_loop(self):
        """Keep cached system knowledge fresh without page-load probes."""
        if SYSTEM_STATUS_INTERVAL <= 0:
            self.logger.info("PRODOPS periodic system status refresh disabled")
            return
        time.sleep(max(SYSTEM_STATUS_INITIAL_DELAY, 0))
        while True:
            self.run_in_background(
                self._do_refresh_system_status,
                {'source': 'ops_agent_periodic'},
                dedup_key="refresh_system_status",
                label="refresh_system_status periodic")
            time.sleep(SYSTEM_STATUS_INTERVAL)

    def _handle_association_sweep(self, m):
        """Batch-associate recent PanDA tasks with PCS campaign tasks."""
        self.run_in_background(
            self._do_association_sweep, m,
            dedup_key="association_sweep", label="association_sweep")

    def _do_association_sweep(self, m):
        """Run the sweep_panda_associations management command. No SSE push —
        nothing in the browser waits on this; the action stream carries the
        outcome, counts, and duration."""
        days = int(m.get('days') or ASSOCIATION_SWEEP_DAYS)
        cmd = [sys.executable, str(MANAGE_PY), "sweep_panda_associations",
               "--days", str(days)]
        self.logger.info(f"PRODOPS association_sweep: days={days}")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=ASSOCIATION_SWEEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS association_sweep TIMEOUT after {ASSOCIATION_SWEEP_TIMEOUT}s")
            self._log_action('association_sweep', t0, outcome='timeout',
                             reason=f'timed out after {ASSOCIATION_SWEEP_TIMEOUT}s',
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True,
                             level=logging.ERROR, days=days)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  sweep-panda-associations: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  sweep-panda-associations: {line}")
        summary = (p.stdout or "").strip().splitlines()
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS association_sweep FAILED rc={p.returncode}")
            self._log_action('association_sweep', t0, outcome='error',
                             reason=reason,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True,
                             level=logging.ERROR, days=days)
        else:
            self.logger.info("PRODOPS association_sweep done")
            self._log_action('association_sweep', t0,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, days=days,
                             summary=(summary[-1] if summary else ''))

    def _handle_questionnaire_import(self, m):
        """Import new production-request form responses (nightly sweep mode:
        CSV URL from SysConfig 'questionnaire_csv_url')."""
        self.run_in_background(
            self._do_questionnaire_import, m,
            dedup_key="questionnaire_import", label="questionnaire_import")

    def _do_questionnaire_import(self, m):
        created_by = str(m.get('created_by') or 'questionnaire_import')
        cmd = [sys.executable, str(QUESTIONNAIRE_IMPORT_SCRIPT),
               "--from-sysconfig", "--created-by", created_by]
        self.logger.info("PRODOPS questionnaire_import: importing form responses")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=QUESTIONNAIRE_IMPORT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS questionnaire_import TIMEOUT after {QUESTIONNAIRE_IMPORT_TIMEOUT}s")
            self._log_action('questionnaire_import', t0, outcome='timeout',
                             reason=f'timed out after {QUESTIONNAIRE_IMPORT_TIMEOUT}s',
                             username=created_by, sublevel='normal',
                             live_default=True, level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  import-questionnaires: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  import-questionnaires: {line}")
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS questionnaire_import FAILED rc={p.returncode}")
            self._log_action('questionnaire_import', t0, outcome='error',
                             reason=reason,
                             username=created_by, sublevel='normal',
                             live_default=True, level=logging.ERROR)
        elif out.startswith('SKIPPED'):
            self.logger.info("PRODOPS questionnaire_import skipped (no URL configured)")
            self._log_action('questionnaire_import', t0, outcome='skipped',
                             username=created_by, message=out)
        else:
            self.logger.info("PRODOPS questionnaire_import done")
            self._log_action('questionnaire_import', t0,
                             username=created_by, sublevel='normal',
                             live_default=True,
                             summary=(out.splitlines()[-1] if out else ''))

    def _handle_questionnaire_automatch(self, m):
        """LLM-assisted matching of questionnaires to catalog tasks."""
        self.run_in_background(
            self._do_questionnaire_automatch, m,
            dedup_key="questionnaire_automatch", label="questionnaire_automatch")

    def _do_questionnaire_automatch(self, m):
        created_by = str(m.get('created_by') or 'automatch')
        cmd = [sys.executable, str(QUESTIONNAIRE_AUTOMATCH_SCRIPT),
               "--created-by", created_by]
        if m.get('all'):
            cmd.append("--all")
        self.logger.info("PRODOPS questionnaire_automatch: matching requests to tasks")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=QUESTIONNAIRE_AUTOMATCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS questionnaire_automatch TIMEOUT after {QUESTIONNAIRE_AUTOMATCH_TIMEOUT}s")
            self._log_action('questionnaire_automatch', t0, outcome='timeout',
                             reason=f'timed out after {QUESTIONNAIRE_AUTOMATCH_TIMEOUT}s',
                             username=created_by, sublevel='normal',
                             live_default=True, level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  match-questionnaires: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  match-questionnaires: {line}")
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS questionnaire_automatch FAILED rc={p.returncode}")
            self._log_action('questionnaire_automatch', t0, outcome='error',
                             reason=reason,
                             username=created_by, sublevel='normal',
                             live_default=True, level=logging.ERROR)
        else:
            self.logger.info("PRODOPS questionnaire_automatch done")
            self._log_action('questionnaire_automatch', t0,
                             username=created_by, sublevel='normal',
                             live_default=True,
                             summary=(out.splitlines()[-1] if out else ''))

    def _handle_catalog_sync(self, m):
        """Nightly composite catalog sync: association sweep, Rucio snapshot,
        EVGEN assimilation, questionnaire match, progress refresh — in
        dependency order."""
        self.run_in_background(
            self._do_catalog_sync, m,
            dedup_key="catalog_sync", label="catalog_sync")

    def _do_catalog_sync(self, m):
        """Run the sync steps sequentially. Each step logs its own action
        record with outcome and duration; this logs the chain summary — the
        catalog-freshness timestamp the alarm system watches."""
        created_by = str(m.get('created_by') or 'catalog_sync')
        t0 = time.monotonic()
        steps = [
            ('catalog_import_csv',
             lambda msg: self._do_catalog_import(dict(msg, source='csv'))),
            ('questionnaire_import', self._do_questionnaire_import),
            ('association_sweep', self._do_association_sweep),
            ('rucio_snapshot_update', self._do_rucio_snapshot_update),
            ('rucio_arrivals_sweep', self._do_rucio_arrivals_sweep),
            ('evgen_rucio_update', self._do_evgen_rucio_update),
            ('questionnaire_automatch', self._do_questionnaire_automatch),
            ('questionnaire_match_update', self._do_questionnaire_match_update),
            ('campaign_progress_refresh', self._do_campaign_progress_refresh),
        ]
        failed = []
        for name, step in steps:
            try:
                step(dict(m, created_by=created_by))
            except Exception as e:
                failed.append(name)
                self.logger.error(f"PRODOPS catalog_sync step {name} raised: {e}")
        self._log_action(
            'catalog_sync', t0,
            outcome='ok' if not failed else 'error',
            reason=('step(s) raised: ' + ', '.join(failed)) if failed else '',
            username=created_by,
            sublevel='high', live_default=True,
            level=logging.INFO if not failed else logging.ERROR,
            steps=len(steps), raised=failed)

    def _handle_rucio_snapshot_update(self, m):
        """Refresh the JLab Rucio snapshot + rematch outputs, off the receiver
        thread — the doer makes a live JLab fetch and matches every task, far
        too slow for the web request (which is why it moved here). Deduped to one
        refresh at a time; it scans the whole current/last campaign."""
        self.run_in_background(
            self._do_rucio_snapshot_update, m,
            dedup_key="rucio_snapshot_update", label="rucio_snapshot_update")

    def _do_rucio_snapshot_update(self, m):
        """Run the snapshot-refresh doer; push completion on success AND failure
        so the catalog never hangs on 'Updating…'. See docs/SSE_PUSH.md."""
        cmd = [sys.executable, str(RUCIO_SNAPSHOT_SCRIPT)]
        self.logger.info("PRODOPS rucio_snapshot_update: refreshing current/last snapshot")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=RUCIO_SNAPSHOT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS rucio_snapshot_update TIMEOUT after {RUCIO_SNAPSHOT_TIMEOUT}s")
            self.send_message('/topic/epictopic', {
                'msg_type': 'rucio_snapshot_ready', 'ok': False,
                'error': f'timed out after {RUCIO_SNAPSHOT_TIMEOUT}s'})
            self._log_action('rucio_sweep', t0, outcome='timeout',
                             reason=f'timed out after {RUCIO_SNAPSHOT_TIMEOUT}s',
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  rucio-snapshot-update: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  rucio-snapshot-update: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS rucio_snapshot_update FAILED rc={p.returncode}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'rucio_snapshot_ready', 'ok': False, 'error': reason})
            self._log_action('rucio_sweep', t0, outcome='error',
                             reason=reason,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)
        else:
            self.logger.info("PRODOPS rucio_snapshot_update done")
            self.send_message('/topic/epictopic', {
                'msg_type': 'rucio_snapshot_ready', 'ok': True})
            self._log_action('rucio_sweep', t0,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True)

    def _handle_rucio_arrivals_sweep(self, m):
        """Run the clockwork arrivals sweep off the receiver thread —
        normally a catalog_sync chain step, also directly invokable."""
        self.run_in_background(
            self._do_rucio_arrivals_sweep, m,
            dedup_key="rucio_arrivals_sweep", label="rucio_arrivals_sweep")

    def _do_rucio_arrivals_sweep(self, m):
        """Detect new files landed in JLab Rucio since the last sweep and
        record per-campaign arrivals (the derived 'producing' signal). The
        service emits the live rucio_arrivals event when anything arrived;
        this logs the step itself."""
        cmd = [sys.executable, str(RUCIO_ARRIVALS_SCRIPT),
               "--created-by", str(m.get('created_by') or 'prodops_agent')]
        self.logger.info("PRODOPS rucio_arrivals_sweep: querying new files")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=RUCIO_ARRIVALS_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS rucio_arrivals_sweep TIMEOUT after {RUCIO_ARRIVALS_TIMEOUT}s")
            self._log_action('rucio_arrivals_sweep', t0, outcome='timeout',
                             reason=f'timed out after {RUCIO_ARRIVALS_TIMEOUT}s',
                             username=str(m.get('created_by') or ''),
                             sublevel='low', live_default=False,
                             level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  rucio-arrivals-sweep: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  rucio-arrivals-sweep: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS rucio_arrivals_sweep FAILED rc={p.returncode}")
            self._log_action('rucio_arrivals_sweep', t0, outcome='error',
                             reason=reason,
                             username=str(m.get('created_by') or ''),
                             sublevel='low', live_default=False,
                             level=logging.ERROR)
        else:
            self.logger.info("PRODOPS rucio_arrivals_sweep done")
            self._log_action('rucio_arrivals_sweep', t0,
                             username=str(m.get('created_by') or ''),
                             sublevel='low', live_default=False,
                             summary=((p.stdout or '').splitlines() or [''])[0])

    def _handle_evgen_rucio_update(self, m):
        """Assimilate the JLab Rucio EVGEN inventory off the receiver thread — a
        live JLab fetch + per-dataset match, too slow for the web request.
        Deduped to one assimilation at a time."""
        self.run_in_background(
            self._do_evgen_rucio_update, m,
            dedup_key="evgen_rucio_update", label="evgen_rucio_update")

    def _do_evgen_rucio_update(self, m):
        """Run the EVGEN assimilation doer (--apply); push completion on success
        AND failure so the catalog never hangs on 'Updating…'. See docs/SSE_PUSH.md."""
        cmd = [sys.executable, str(EVGEN_RUCIO_SCRIPT), "--apply"]
        self.logger.info("PRODOPS evgen_rucio_update: assimilating JLab Rucio EVGEN")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=EVGEN_RUCIO_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS evgen_rucio_update TIMEOUT after {EVGEN_RUCIO_TIMEOUT}s")
            self.send_message('/topic/epictopic', {
                'msg_type': 'evgen_rucio_ready', 'ok': False,
                'error': f'timed out after {EVGEN_RUCIO_TIMEOUT}s'})
            self._log_action('evgen_sweep', t0, outcome='timeout',
                             reason=f'timed out after {EVGEN_RUCIO_TIMEOUT}s',
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  import-evgen-rucio: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  import-evgen-rucio: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS evgen_rucio_update FAILED rc={p.returncode}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'evgen_rucio_ready', 'ok': False, 'error': reason})
            self._log_action('evgen_sweep', t0, outcome='error',
                             reason=reason,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR)
        else:
            self.logger.info("PRODOPS evgen_rucio_update done")
            self.send_message('/topic/epictopic', {
                'msg_type': 'evgen_rucio_ready', 'ok': True})
            self._log_action('evgen_sweep', t0,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True)

    def _handle_catalog_import(self, m):
        """Run a PCS catalog import (csv / epic-prod) off the receiver thread —
        the epic-prod walk of ~4900 datasets is far too slow for the web request
        (which is why it moved here). Deduped per source."""
        source = m.get('source', '')
        self.run_in_background(
            self._do_catalog_import, m,
            dedup_key=f"catalog_import:{source}", label=f"catalog_import:{source}")

    def _do_catalog_import(self, m):
        """Run the catalog-import doer; push completion on success AND failure so
        the catalog never hangs on 'Updating…'. See docs/SSE_PUSH.md."""
        source = m.get('source', '')
        cmd = [sys.executable, str(CATALOG_IMPORT_SCRIPT), source]
        self.logger.info(f"PRODOPS catalog_import: importing {source}")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=CATALOG_IMPORT_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"PRODOPS catalog_import {source} TIMEOUT after {CATALOG_IMPORT_TIMEOUT}s")
            self.send_message('/topic/epictopic', {
                'msg_type': 'catalog_import_ready', 'source': source, 'ok': False,
                'error': f'timed out after {CATALOG_IMPORT_TIMEOUT}s'})
            self._log_action('catalog_import', t0, outcome='timeout',
                             reason=f'timed out after {CATALOG_IMPORT_TIMEOUT}s',
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR, source=source)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  pcs-catalog-import: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  pcs-catalog-import: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(f"PRODOPS catalog_import {source} FAILED rc={p.returncode}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'catalog_import_ready', 'source': source, 'ok': False,
                'error': reason})
            self._log_action('catalog_import', t0, outcome='error',
                             reason=reason,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, level=logging.ERROR, source=source)
        else:
            summary = (p.stdout or "").strip().splitlines()
            self.logger.info(f"PRODOPS catalog_import {source} done")
            self.send_message('/topic/epictopic', {
                'msg_type': 'catalog_import_ready', 'source': source, 'ok': True,
                'summary': summary[-1] if summary else ''})
            self._log_action('catalog_import', t0,
                             username=str(m.get('created_by') or ''),
                             sublevel='high', live_default=True, source=source,
                             summary=(summary[-1] if summary else ''))

    def _handle_questionnaire_match_update(self, m):
        """Rebuild task-local questionnaire-match caches off the receiver
        thread, then push completion so the catalog button can reload."""
        self.run_in_background(
            self._do_questionnaire_match_update, m,
            dedup_key="questionnaire_match_update",
            label="questionnaire_match_update")

    def _do_questionnaire_match_update(self, m):
        created_by = m.get('created_by') or 'questionnaire_match'
        cmd = [
            sys.executable, str(QUESTIONNAIRE_MATCH_SCRIPT),
            "--updated-by", str(created_by),
        ]
        self.logger.info("PRODOPS questionnaire_match_update: rebuilding cache")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=QUESTIONNAIRE_MATCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                "PRODOPS questionnaire_match_update TIMEOUT after "
                f"{QUESTIONNAIRE_MATCH_TIMEOUT}s")
            self.send_message('/topic/epictopic', {
                'msg_type': 'questionnaire_match_ready', 'ok': False,
                'error': f'timed out after {QUESTIONNAIRE_MATCH_TIMEOUT}s'})
            self._log_action('questionnaire_match', t0, outcome='timeout',
                             reason=f'timed out after {QUESTIONNAIRE_MATCH_TIMEOUT}s',
                             username=str(created_by), level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  update-questionnaire-matches: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  update-questionnaire-matches: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(
                f"PRODOPS questionnaire_match_update FAILED rc={p.returncode}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'questionnaire_match_ready', 'ok': False,
                'error': reason})
            self._log_action('questionnaire_match', t0, outcome='error',
                             reason=reason,
                             username=str(created_by), level=logging.ERROR)
        else:
            summary = (p.stdout or "").strip()
            self.logger.info("PRODOPS questionnaire_match_update done")
            self.send_message('/topic/epictopic', {
                'msg_type': 'questionnaire_match_ready', 'ok': True,
                'summary': summary})
            self._log_action('questionnaire_match', t0, username=str(created_by))

    def _handle_campaign_progress_refresh(self, m):
        """Rebuild current campaign progress data + progress table cache."""
        now = time.time()
        if now - self._campaign_progress_last_start < CAMPAIGN_PROGRESS_MIN_INTERVAL:
            self.logger.warning("PRODOPS campaign_progress_refresh: cooldown active, dropping duplicate")
            return
        self._campaign_progress_last_start = now
        self.run_in_background(
            self._do_campaign_progress_refresh, m,
            dedup_key="campaign_progress_refresh",
            label="campaign_progress_refresh")

    def _do_campaign_progress_refresh(self, m):
        created_by = m.get('created_by') or 'progress_refresh'
        cmd = [
            sys.executable, str(CAMPAIGN_PROGRESS_SCRIPT),
            "--generated-by", str(created_by),
        ]
        self.logger.info("PRODOPS campaign_progress_refresh: rebuilding progress cache")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=CAMPAIGN_PROGRESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.logger.error(
                "PRODOPS campaign_progress_refresh TIMEOUT after "
                f"{CAMPAIGN_PROGRESS_TIMEOUT}s")
            self.send_message('/topic/epictopic', {
                'msg_type': 'campaign_progress_ready', 'ok': False,
                'error': f'timed out after {CAMPAIGN_PROGRESS_TIMEOUT}s'})
            self._log_action('progress_refresh', t0, outcome='timeout',
                             reason=f'timed out after {CAMPAIGN_PROGRESS_TIMEOUT}s',
                             username=str(created_by), level=logging.ERROR)
            return
        for line in (p.stdout or "").splitlines():
            self.logger.info(f"  refresh-campaign-progress: {line}")
        for line in (p.stderr or "").splitlines():
            self.logger.info(f"  refresh-campaign-progress: {line}")
        if p.returncode != 0:
            reason = self._derive_reason(p)
            self.logger.error(
                f"PRODOPS campaign_progress_refresh FAILED rc={p.returncode}")
            self.send_message('/topic/epictopic', {
                'msg_type': 'campaign_progress_ready', 'ok': False,
                'error': reason})
            self._log_action('progress_refresh', t0, outcome='error',
                             reason=reason,
                             username=str(created_by), level=logging.ERROR)
        else:
            summary = (p.stdout or "").strip()
            self.logger.info("PRODOPS campaign_progress_refresh done")
            self.send_message('/topic/epictopic', {
                'msg_type': 'campaign_progress_ready', 'ok': True,
                'summary': summary})
            self._log_action('progress_refresh', t0, username=str(created_by))

    # -- action stream --------------------------------------------------------

    def _log_action(self, action, t0=None, *, outcome='ok', subject_type='',
                    subject_key='', username='', sublevel='low',
                    live_default=False, message='', reason='',
                    level=logging.INFO, **counts):
        """Record one action in the epicprod action stream (AppLog via REST).

        The ops agent is out-of-process, so this posts the same record shape
        monitor_app.epicprod_logging.log_epicprod_action writes via the ORM:
        app_name='epicprod', instance 'ops-agent', structured extra_data with
        the event's declared sublevel (importance: which humans it
        reaches; authoritative, changed by changing the event) and its
        live_default RECOMMENDATION for the live stream (effective decision =
        SysConfig live override over the default). Pass the doer start time
        as t0 and the measured duration_ms is recorded — every sweep and
        timed operation reports its execution time to the log. Every non-ok
        outcome passes reason — the short failure cause (last stderr line,
        rc, or timeout note); it is stored in extra_data and appended to the
        message, so the why is exposed everywhere the record is read.
        Never raises; a failed post is logged and the action proceeds.
        """
        extra = {
            'action': str(action),
            'outcome': str(outcome),
            'sublevel': str(sublevel),
            'live_default': bool(live_default),
        }
        if subject_type:
            extra['subject_type'] = str(subject_type)
        if subject_key:
            extra['subject_key'] = str(subject_key)
        if username:
            extra['username'] = str(username)
        if t0 is not None:
            extra['duration_ms'] = int((time.monotonic() - t0) * 1000)
        if reason:
            extra['reason'] = str(reason)[:300]
        for key, value in counts.items():
            if key not in ('action', 'subject_type', 'subject_key', 'username',
                           'outcome', 'duration_ms', 'sublevel', 'live_default',
                           'reason'):
                extra[key] = value
        if not message:
            subject = f"{subject_type}:{subject_key}" if subject_key else ''
            message = ' '.join(x for x in (str(action), subject, str(outcome)) if x)
        if reason:
            message = f"{message} — {str(reason)[:300]}"
        try:
            resp = self._action_log_session.post(
                f"{MONITOR_HTTP_URL.rstrip('/')}/api/logs/",
                json={
                    'app_name': 'epicprod',
                    'instance_name': 'ops-agent',
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'level': int(level),
                    'levelname': logging.getLevelName(int(level)),
                    'message': message,
                    'module': 'epicprod_ops_agent',
                    'funcname': str(action),
                    'lineno': 0,
                    'process': os.getpid(),
                    'thread': threading.get_ident(),
                    'extra_data': extra,
                },
                timeout=ACTION_LOG_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            self.logger.warning(f"PRODOPS action log post failed ({action}): {e}")

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _derive_reason(p):
        """Short failure cause from a finished doer subprocess.

        A negative returncode means the doer was killed by a signal (an agent
        restart SIGTERMs in-flight doers) and its stderr cannot explain that.
        Otherwise prefer the last stderr line that states an error over
        trailing bootstrap/INFO noise, then the last line, then the bare rc.
        """
        if p.returncode < 0:
            try:
                signame = signal.Signals(-p.returncode).name
            except ValueError:
                signame = str(-p.returncode)
            return f"killed by signal {signame}"
        stderr = (p.stderr or "").strip()
        if not stderr:
            return f"rc={p.returncode}"
        lines = stderr.splitlines()
        for line in reversed(lines):
            if any(tok in line for tok in ('ERROR', 'CRITICAL', 'FATAL',
                                           'Error:', 'Exception')):
                return line.strip()
        return lines[-1].strip()

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
