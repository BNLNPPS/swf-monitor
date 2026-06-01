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

v1 capability:
  fetch_payload_log  — retrieve + cache one PanDA job's payload log
                       (delegates to scripts/cache-payload-log.py).

Run as a systemd service (like the swf-*-bot units), User=wenauseic. Because it
subclasses BaseAgent it registers and heartbeats to the monitor, so it appears
in the agent list and reconnects to ActiveMQ on its own.

See docs/EPICPROD_OPS.md.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

from swf_common_lib.base_agent import BaseAgent

# Anycast control queue: one consumer handles each request exactly once.
OPS_QUEUE = os.environ.get("EPICPROD_OPS_QUEUE", "/queue/epicprod.ops")

# The standalone doer, shipped alongside this agent.
FETCH_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "cache-payload-log.py"


class EpicProdOpsAgent(BaseAgent):
    """Production operations agent — dispatches ops messages to handlers."""

    KNOWN_TYPES = {"fetch_payload_log"}

    def __init__(self):
        super().__init__(agent_type="PRODOPS", subscription_queue=OPS_QUEUE)

    def on_message(self, frame):
        message_data, msg_type = self.log_received_message(frame, known_types=self.KNOWN_TYPES)
        if message_data is None:          # namespace-filtered — ignore
            return
        handler = getattr(self, f"_handle_{msg_type}", None)
        if handler is None:
            logging.warning(f"PRODOPS: no handler for msg_type '{msg_type}'")
            return
        with self.processing():
            try:
                handler(message_data)
            except Exception as e:
                logging.error(f"PRODOPS: handler '{msg_type}' raised: {e}")

    # -- handlers ------------------------------------------------------------

    def _handle_fetch_payload_log(self, m):
        """Fetch + cache one job's payload log via the standalone helper."""
        missing = [k for k in ("scope", "lfn", "jeditaskid", "pandaid") if not m.get(k)]
        if missing:
            logging.error(f"PRODOPS fetch_payload_log: missing fields {missing}")
            return
        cmd = [
            sys.executable, str(FETCH_SCRIPT),
            "--scope", str(m["scope"]),
            "--lfn", str(m["lfn"]),
            "--jeditaskid", str(m["jeditaskid"]),
            "--pandaid", str(m["pandaid"]),
        ]
        logging.info(f"PRODOPS fetch_payload_log: pandaid={m['pandaid']} task={m['jeditaskid']}")
        p = subprocess.run(cmd, capture_output=True, text=True)
        for line in (p.stderr or "").splitlines():
            logging.info(f"  cache-payload-log: {line}")
        if p.returncode != 0:
            logging.error(f"PRODOPS fetch_payload_log FAILED rc={p.returncode} pandaid={m['pandaid']}")
        else:
            logging.info(f"PRODOPS fetch_payload_log done: pandaid={m['pandaid']}")


def main():
    EpicProdOpsAgent().run()


if __name__ == "__main__":
    main()
