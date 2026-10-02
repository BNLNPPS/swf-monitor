"""Alarm: storage_headroom.

Production output is written to storage behind xrootd doors whose space is
finite. This alarm asks each listed door for the space of its path
(``xrdfs <door> query space <path>``: total and free) and fires when the
free fraction falls below the threshold, before writes start failing.

On 2026-10-02 the EIC area behind BNL-XRD filled; thousands of finished jobs
lost their output to "HTTP 507 Insufficient Storage" before the door probe
reported it down.
"""
from __future__ import annotations

import os
import subprocess

from ..common import Detection

PARAMS = {
    # "name=door|path" items, comma separated.
    "doors": "BNL-XRD=root://epicxrd1.sdcc.bnl.gov:1094|/eic/EPIC",
    # Fire below this free fraction of the total.
    "min_free_fraction": 0.10,
    "proxy": "/data/wenauseic/longproxy-for-rucio",
    "timeout_seconds": 60,
}


def _doors(raw):
    for item in str(raw).split(","):
        name, _, rest = item.strip().partition("=")
        door, _, path = rest.partition("|")
        if name and door and path:
            yield name.strip(), door.strip(), path.strip()


def _space(door, path, proxy, timeout):
    env = dict(os.environ, X509_USER_PROXY=proxy)
    out = subprocess.run(["xrdfs", door, "query", "space", path], capture_output=True,
                         text=True, timeout=timeout, env=env)
    if out.returncode != 0:
        return None
    fields = dict(kv.split("=", 1) for kv in out.stdout.strip().split("&") if "=" in kv)
    try:
        return int(fields["oss.space"]), int(fields["oss.free"])
    except (KeyError, ValueError):
        return None


def detect(client, params):
    minimum = float(params.get("min_free_fraction", 0.10))
    proxy = params.get("proxy", "")
    timeout = float(params.get("timeout_seconds", 60))
    for name, door, path in _doors(params.get("doors", "")):
        try:
            space = _space(door, path, proxy, timeout)
        except (OSError, subprocess.SubprocessError):
            space = None
        if not space or space[0] <= 0:
            continue  # unanswered: the door probe owns reachability
        total, free = space
        fraction = free / total
        if fraction < minimum:
            yield Detection(
                dedupe_key=f"storage:{name}",
                subject=(f"{name} storage {100 * (1 - fraction):.1f}% full: "
                         f"{free / 1e12:.1f} of {total / 1e12:.1f} TB free"),
                body_context=(
                    f"The space behind {door} {path} has {free / 1e12:.1f} TB free of "
                    f"{total / 1e12:.1f} TB ({100 * fraction:.1f}%, threshold "
                    f"{100 * minimum:.0f}%). Jobs writing there fail once it is full; "
                    "free space or redirect output before then."),
                extra_data={"rse": name, "door": door, "path": path, "total_bytes": total,
                            "free_bytes": free, "free_fraction": round(fraction, 4)},
            )
