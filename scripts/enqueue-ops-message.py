#!/usr/bin/env python3
"""Send one message to the epicprod ops queue — the cron-friendly trigger.

The cron side of scheduled automation is deliberately trivial: this script
does nothing but enqueue a message for the prod-ops agent, which performs,
logs, and times the actual work (see docs/EPICPROD_OPS_AGENT.md, action-stream
logging). Uses the same ACTIVEMQ_* environment the agents use (source ~/.env).

Usage:
    enqueue-ops-message.py catalog_sync --created-by nightly_cron
    enqueue-ops-message.py association_sweep --created-by backfill --extra days=30
    enqueue-ops-message.py assess_refresh --queue /queue/canary.ops \
        --namespace canary --created-by hourly_cron
"""

import argparse
import json
import os
import ssl
import sys

import stomp

OPS_QUEUE = os.environ.get("EPICPROD_OPS_QUEUE", "/queue/epicprod.ops")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('msg_type', help="ops message type, e.g. catalog_sync")
    parser.add_argument('--created-by', default='cron',
                        help="requester recorded in the action stream")
    parser.add_argument('--extra', action='append', default=[],
                        help="extra key=value message fields (int if numeric)")
    parser.add_argument('--queue', default=OPS_QUEUE,
                        help="destination queue (default: the epicprod ops queue)")
    parser.add_argument('--namespace', default='prodops',
                        help="agent namespace stamped on the message")
    args = parser.parse_args()

    msg = {'msg_type': args.msg_type, 'namespace': args.namespace,
           'created_by': args.created_by}
    for item in args.extra:
        key, _, value = item.partition('=')
        if not key or not value:
            sys.exit(f"bad --extra {item!r}, need key=value")
        msg[key] = int(value) if value.isdigit() else value

    host = os.getenv('ACTIVEMQ_HOST', 'localhost')
    port = int(os.getenv('ACTIVEMQ_PORT', '61612'))
    conn = stomp.Connection(host_and_ports=[(host, port)], vhost=host,
                            try_loopback_connect=False)
    if os.getenv('ACTIVEMQ_USE_SSL', 'False').lower() == 'true':
        conn.transport.set_ssl(
            for_hosts=[(host, port)],
            ca_certs=os.getenv('ACTIVEMQ_SSL_CA_CERTS') or None,
            ssl_version=ssl.PROTOCOL_TLS_CLIENT,
        )
    conn.connect(os.getenv('ACTIVEMQ_USER', 'admin'),
                 os.getenv('ACTIVEMQ_PASSWORD', 'admin'), wait=True)
    try:
        conn.send(destination=args.queue, body=json.dumps(msg))
    finally:
        conn.disconnect()
    print(f"enqueued {args.msg_type} to {args.queue}")


if __name__ == '__main__':
    main()
