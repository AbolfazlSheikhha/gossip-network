#!/usr/bin/env python3
"""
Independent functional test:
- Start 10 gossip nodes on dynamically allocated localhost UDP ports.
- Inject one GOSSIP message from a non-node sender.
- Assert that at least 9 distinct nodes receive the message.

This test does NOT modify or replace existing tests/logs.
It writes its own artifacts under logs/tests_10_nodes_min9/<timestamp>/.
"""

import json
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import List, Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent
NODE_SCRIPT = PROJECT_ROOT / "gossip_node.py"

NODES_COUNT = 10
MIN_RECEIVERS = 9
WARMUP_SECONDS = 5.0
PROPAGATION_SECONDS = 20.0

GOSSIP_RECEIVED_RE = re.compile(
    r"\bGOSSIP_RECEIVED\b.*?\bport=(?P<port>\d+)\b.*?\bmsg_id=(?P<msg_id>[^\s]+)\b"
)


def _reserve_free_udp_port_block(size: int, tries: int = 80) -> int:
    for _ in range(tries):
        base = 12000 + int(time.time() * 1000) % 20000
        base += int(uuid.uuid4().int % 15000)
        if base + size + 1 > 65000:
            base = 20000

        sockets = []
        ok = True
        try:
            for p in range(base, base + size):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(("127.0.0.1", p))
                sockets.append(s)
        except OSError:
            ok = False
        finally:
            for s in sockets:
                s.close()
        if ok:
            return base
    raise RuntimeError(f"Unable to reserve a free UDP port block of size {size}")


def _start_node(log_file: Path, port: int, bootstrap: Tuple[str, int] | None, seed: int) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(NODE_SCRIPT),
        "--port", str(port),
        "--fanout", "4",
        "--ttl", "10",
        "--peer-limit", "20",
        "--seed", str(seed),
        "--ping-interval", "2",
        "--peer-timeout", "10",
        "--pull-interval", "2",
        "--discovery-interval", "2",
        "--pow-k", "0",
        "--stdin", "false",
    ]
    if bootstrap is not None:
        cmd += ["--bootstrap", f"{bootstrap[0]}:{bootstrap[1]}"]

    f = open(log_file, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc._test_log_handle = f  # type: ignore[attr-defined]
    return proc


def _stop_processes(procs: List[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
    time.sleep(1.2)
    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(0.8)
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
    for p in procs:
        try:
            p.wait(timeout=2)
        except Exception:
            pass
        h = getattr(p, "_test_log_handle", None)
        if h:
            try:
                h.close()
            except Exception:
                pass


def _inject_gossip(target_port: int, msg_id: str) -> None:
    now_ms = int(time.time() * 1000)
    msg = {
        "version": 1,
        "msg_id": msg_id,
        "msg_type": "GOSSIP",
        "sender_id": "injector-test",
        "sender_addr": "127.0.0.1:0",
        "timestamp_ms": now_ms,
        "ttl": 10,
        "payload": {
            "topic": "test",
            "data": "10_nodes_min_9_receivers",
            "origin_id": "injector-test",
            "origin_timestamp_ms": now_ms,
        },
    }
    payload = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, ("127.0.0.1", target_port))
    finally:
        sock.close()


def _count_receivers(logs_dir: Path, msg_id: str) -> Set[int]:
    receivers: Set[int] = set()
    for lf in sorted(logs_dir.glob("node_*.log")):
        with lf.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = GOSSIP_RECEIVED_RE.search(line)
                if not m:
                    continue
                if m.group("msg_id") == msg_id:
                    receivers.add(int(m.group("port")))
                    break
    return receivers


def main() -> int:
    run_id = int(time.time() * 1000)
    logs_dir = PROJECT_ROOT / "logs" / "tests_10_nodes_min9" / f"run_{run_id}"
    logs_dir.mkdir(parents=True, exist_ok=True)

    base_port = _reserve_free_udp_port_block(NODES_COUNT)
    ports = [base_port + i for i in range(NODES_COUNT)]
    bootstrap = ("127.0.0.1", ports[0])

    procs: List[subprocess.Popen] = []
    try:
        for i, p in enumerate(ports):
            boot = None if i == 0 else bootstrap
            procs.append(_start_node(log_file=logs_dir / f"node_{p}.log", port=p, bootstrap=boot, seed=500 + i))

        time.sleep(WARMUP_SECONDS)
        msg_id = str(uuid.uuid4())
        _inject_gossip(target_port=ports[1], msg_id=msg_id)
        time.sleep(PROPAGATION_SECONDS)
    finally:
        _stop_processes(procs)

    receivers = _count_receivers(logs_dir, msg_id)
    passed = len(receivers) >= MIN_RECEIVERS
    summary = {
        "test": "10_nodes_min9_receivers",
        "msg_id": msg_id,
        "base_port": base_port,
        "receivers_count": len(receivers),
        "required_min_receivers": MIN_RECEIVERS,
        "receivers_ports": sorted(receivers),
        "logs_dir": str(logs_dir),
        "result": "PASS" if passed else "FAIL",
    }
    with (logs_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
