#!/usr/bin/env python3
"""A stand-in speed-test engine for the test suite.

Executed as a real subprocess so the runner's process handling is exercised
for real. It emits recorded Ookla-shaped output and never touches the
network.

The ``hang`` behaviour additionally spawns a child that sleeps, so a test can
prove that stopping a test kills the whole process group rather than leaving
an orphan behind.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

SUCCESS_RESULT = {
    "type": "result",
    "timestamp": "2026-08-04T09:15:00Z",
    "ping": {"jitter": 1.234, "latency": 10.5, "low": 9.1, "high": 12.7},
    "download": {
        "bandwidth": 11875000,  # bytes/s -> 95 Mbps
        "bytes": 118750000,
        "elapsed": 10000,
        "latency": {"iqm": 15.25, "low": 12.0, "high": 30.0, "jitter": 2.5},
    },
    "upload": {
        "bandwidth": 2375000,  # bytes/s -> 19 Mbps
        "bytes": 23750000,
        "elapsed": 10000,
        "latency": {"iqm": 18.75, "low": 14.0, "high": 40.0, "jitter": 3.0},
    },
    "packetLoss": 0,
    "isp": "Example Internet",
    "interface": {
        "internalIp": "192.168.1.20",
        "name": "enp3s0",
        "macAddr": "AA:BB:CC:DD:EE:FF",
        "isVpn": False,
        "externalIp": "203.0.113.42",
    },
    "server": {
        "id": 12345,
        "host": "speedtest.example.net",
        "port": 8080,
        "name": "Example Telecom",
        "location": "Manchester",
        "country": "United Kingdom",
        "ip": "198.51.100.7",
    },
    "result": {"id": "abc-123", "url": "https://example.invalid/result/abc-123", "persisted": False},
}

# Everything optional stripped out: no jitter, no packet loss, no per-direction
# latency, no server detail. Those must come back as NULL, not as zero.
SPARSE_RESULT = {
    "type": "result",
    "download": {"bandwidth": 1250000},
    "upload": {"bandwidth": 625000},
    "ping": {"latency": 42.0},
    "interface": {"name": "wlp2s0"},
}

# A connection that measured genuine zeros. These must survive as 0, because
# "measured nothing" and "did not measure" are different facts.
ZERO_RESULT = {
    "type": "result",
    "download": {"bandwidth": 0},
    "upload": {"bandwidth": 0},
    "ping": {"latency": 0, "jitter": 0},
    "packetLoss": 0,
    "isp": "Example Internet",
    "interface": {"name": "enp3s0", "externalIp": "203.0.113.42"},
    "server": {"id": 12345, "name": "Example Telecom", "location": "Manchester"},
}

DNS_FAILURE_TEXT = (
    "[error] Configuration - Could not retrieve or read the configuration "
    "(Cannot read: Temporary failure in name resolution)"
)

GENERIC_FAILURE_TEXT = "[error] Engine stopped unexpectedly while measuring upload."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--behaviour", default="success")
    parser.add_argument("--delay", type=float, default=0.0)
    arguments = parser.parse_args()

    if arguments.delay > 0:
        time.sleep(arguments.delay)

    behaviour = arguments.behaviour

    if behaviour == "success":
        print(json.dumps(SUCCESS_RESULT))
        return 0

    if behaviour == "sparse":
        print(json.dumps(SPARSE_RESULT))
        return 0

    if behaviour == "zeros":
        print(json.dumps(ZERO_RESULT))
        return 0

    if behaviour == "malformed":
        # Exits successfully but prints something unusable, which is the
        # nastier failure: the runner has to notice by itself.
        print("Speedtest by Ookla")
        print("  Download: 95.00 Mbps  <- not JSON at all")
        return 0

    if behaviour == "failure":
        print(GENERIC_FAILURE_TEXT, file=sys.stderr)
        return 1

    if behaviour == "dns-failure":
        print(DNS_FAILURE_TEXT, file=sys.stderr)
        return 2

    if behaviour in {"hang", "slow"}:
        # A child in the same process group. If the runner only signalled the
        # direct child, this one would survive.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        marker = os.environ.get("FAKE_ENGINE_CHILD_PID_FILE")
        if marker:
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
        time.sleep(600)
        return 0

    print(f"unknown behaviour: {behaviour}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
