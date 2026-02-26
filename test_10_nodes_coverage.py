import json
import re
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import List, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
NODES_COUNT = 10
BASE_PORT = 8000
BOOTSTRAP_WAIT_S = 6
PROPAGATION_WAIT_S = 25
MIN_RECEIVERS = 9

GOSSIP_RECEIVED_RE = re.compile(
    r"GOSSIP_RECEIVED node_id=(?P<node_id>[^ ]+) port=(?P<port>\d+) msg_id=(?P<msg_id>[^ ]+)\s"
)


def launch_10_nodes(logs_dir: Path) -> Tuple[List[subprocess.Popen], List]:
    node_script = str(PROJECT_ROOT / "gossip_node.py")
    processes: List[subprocess.Popen] = []
    log_handles: List = []

    for i in range(NODES_COUNT):
        port = BASE_PORT + i
        is_bootstrap = i == 0
        bootstrap_arg = [] if is_bootstrap else ["--bootstrap", f"127.0.0.1:{BASE_PORT}"]

        args = [
            sys.executable,
            node_script,
            "--port", str(port),
            "--fanout", "4",
            "--ttl", "10",
            "--peer-limit", "20",
            "--ping-interval", "2",
            "--peer-timeout", "6",
            "--seed", str(42 + i),
            "--pow-k", "0",
        ] + bootstrap_arg

        log_file = open(logs_dir / f"node_{port}.log", "w")
        log_handles.append(log_file)
        proc = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(proc)

    return processes, log_handles


def inject_gossip_via_udp(msg_id: str, inject_port: int) -> None:
    now_ms = int(time.time() * 1000)
    payload = {
        "topic": "test_coverage",
        "data": "test_10_nodes_at_least_9_receive",
        "origin_id": "injector-nonexistent-port",
        "origin_timestamp_ms": now_ms,
    }
    msg = {
        "version": 1,
        "msg_id": msg_id,
        "msg_type": "GOSSIP",
        "sender_id": "injector-nonexistent-port",
        "sender_addr": "127.0.0.1:0",
        "timestamp_ms": now_ms,
        "ttl": 8,
        "payload": payload,
    }
    data = json.dumps(msg).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(data, ("127.0.0.1", inject_port))
    sock.close()


def count_receivers(logs_dir: Path, msg_id: str) -> Set[int]:
    receivers: Set[int] = set()
    for log_path in sorted(logs_dir.glob("node_*.log")):
        with log_path.open("r") as f:
            for line in f:
                m = GOSSIP_RECEIVED_RE.search(line)
                if m and m.group("msg_id") == msg_id:
                    receivers.add(int(m.group("port")))
                    break
    return receivers


def run_test() -> bool:
    logs_dir = PROJECT_ROOT / "logs" / "test_10_coverage"
    logs_dir.mkdir(parents=True, exist_ok=True)

    procs, log_handles = launch_10_nodes(logs_dir)
    time.sleep(BOOTSTRAP_WAIT_S)

    msg_id = str(uuid.uuid4())
    inject_gossip_via_udp(msg_id, BASE_PORT + 1)

    time.sleep(PROPAGATION_WAIT_S)

    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    for f in log_handles:
        f.close()

    receivers = count_receivers(logs_dir, msg_id)
    n = len(receivers)
    ok = n >= MIN_RECEIVERS
    print(f"msg_id={msg_id}")
    print(f"Nodes that received the message (by port): {sorted(receivers)}")
    print(f"Count = {n} (required >= {MIN_RECEIVERS}) -> {'PASS' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
