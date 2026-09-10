#!/usr/bin/env python3
"""Event Service probe payload: speak the pilot's range channel.

Runs inside an Event Service job under the pilot's generic executor.
The pilot opens a yampl server socket and exports its name as
PILOT_EVENTRANGECHANNEL; this client connects, asks for ranges, does a
moment of work per range, writes a receipt file per range and reports
each one finished in the pilot's message form:

    payload -> pilot   "Ready for events"
    pilot -> payload   JSON list of ranges, or "No more events"
    payload -> pilot   "<output path>,ID:<eventRangeID>,CPU:<s>,WALL:<s>"

The pilot tars the reported outputs, stages them out on the queue's
es_events/pw activity and reports the ranges finished to the server.
Exit 0 when the pilot says there are no more events.

Usage in the job's exec: python3 es_range_client.py $PILOT_EVENTRANGECHANNEL
(the token on the command line is what makes the pilot export it).

Needs the python-yampl module on PYTHONPATH (built on the host by
swf-epicprod tools/npps0/build-yampl.sh; the pass script exports it and
the pilot's environment reaches the payload).
"""

import json
import os
import sys
import time


def log(msg):
    print(f"[es_range_client] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def main():
    # The pilot hands over the channel by string match: only a payload
    # command that names PILOT_EVENTRANGECHANNEL gets the export prefix
    # (pilot3 esprocess.py); any other command is given the AthenaMP
    # --preExec form. So the job's exec passes $PILOT_EVENTRANGECHANNEL
    # as the argument, and the environment is the fallback.
    name = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else os.environ.get('PILOT_EVENTRANGECHANNEL')
    if not name:
        log("ERROR: no channel name (argument or PILOT_EVENTRANGECHANNEL); not an Event Service job")
        return 2
    try:
        import yampl
    except Exception as exc:  # the host must provide the binding
        log(f"ERROR: cannot import yampl: {exc}; PYTHONPATH={os.environ.get('PYTHONPATH')}")
        return 3

    work_seconds = float(os.environ.get('ES_PROBE_WORK_SECONDS', '2'))
    # Receipts go to the job directory, one level above runGen's workDir
    # (the current directory): runGen removes workDir when the payload
    # exits, before the pilot tars and stages the reported outputs.
    outdir = os.path.abspath(os.environ.get('ES_PROBE_OUTDIR',
                                            os.path.join(os.path.dirname(os.getcwd()), 'es_probe_out')))
    os.makedirs(outdir, exist_ok=True)

    sock = yampl.ClientSocket(name, 'local')
    log(f"connected to channel {name}; work {work_seconds}s per range; receipts in {outdir}")

    done = 0
    while True:
        sock.send_raw(b"Ready for events")
        while True:
            size, buf = sock.try_recv_raw()
            if size != -1:
                break
            time.sleep(0.05)
        message = buf.decode('utf8') if isinstance(buf, bytes) else str(buf)
        if "No more events" in message:
            log(f"no more events after {done} ranges")
            return 0
        try:
            ranges = json.loads(message)
        except Exception as exc:
            log(f"ERROR: unparseable range message: {exc}: {message[:300]}")
            return 4
        if isinstance(ranges, dict):
            ranges = [ranges]
        for rng in ranges:
            rid = rng.get('eventRangeID')
            t0 = time.time()
            c0 = time.process_time()
            time.sleep(work_seconds)
            wall = time.time() - t0
            cpu = time.process_time() - c0
            path = os.path.join(outdir, f"{rid}.json")
            with open(path, 'w') as fh:
                json.dump({'range': rng, 'wall': wall, 'cpu': cpu,
                           'host': os.uname().nodename, 'pid': os.getpid()}, fh)
            report = f"{path},ID:{rid},CPU:{cpu:.3f},WALL:{wall:.3f}"
            sock.send_raw(report.encode('utf8'))
            done += 1
            log(f"range {rid} events {rng.get('startEvent')}-{rng.get('lastEvent')} of {rng.get('LFN')} finished: {report}")


if __name__ == '__main__':
    sys.exit(main())
