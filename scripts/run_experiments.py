#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCALHOST = "127.0.0.1"
MESSAGE_EVENTS = {"gossip_originated", "gossip_first_seen"}

INTERRUPTED = False


def now_ts_ms() -> int:
    return int(time.time() * 1000)


def now_iso_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def make_run_id() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0, got {value}")
    return value


def non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"value must be >= 0, got {value}")
    return value


def positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run reproducible multi-node gossip experiments on localhost.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--n", type=positive_int, help="single network size")
    group.add_argument("--ns", type=positive_int, nargs="+", help="list of network sizes")

    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument("--base-port", type=positive_int, default=15000)
    parser.add_argument("--fanout", type=positive_int, default=3)
    parser.add_argument("--ttl", type=non_negative_int, default=8)
    parser.add_argument("--peer-limit", type=positive_int, default=30)
    parser.add_argument("--ping-interval", type=positive_int, default=1)
    parser.add_argument("--peer-timeout", type=positive_int, default=6)
    parser.add_argument("--pull-interval", type=positive_int, default=2)
    parser.add_argument("--ids-max-ihave", type=positive_int, default=32)
    parser.add_argument("--k-pow", type=non_negative_int, default=0)
    parser.add_argument("--stabilize-seconds", type=positive_float, default=1.0)
    parser.add_argument("--run-timeout-seconds", type=positive_float, default=30.0)
    parser.add_argument("--origin-index", type=non_negative_int, default=0)
    parser.add_argument("--outdir", type=Path, default=Path("logs/experiments"))
    parser.add_argument("--python-exec", type=str, default=sys.executable)
    return parser


@dataclass
class NodeProcess:
    index: int
    port: int
    seed: int
    bootstrap: str
    jsonl_log: Path
    stdout_log: Path
    stderr_log: Path
    command: list[str]
    process: subprocess.Popen[str]
    stdout_fp: TextIO
    stderr_fp: TextIO


def on_signal(_signum: int, _frame: Any) -> None:
    global INTERRUPTED
    INTERRUPTED = True


def parse_jsonl_since(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], offset

    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        fp.seek(offset)
        for line in fp:
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
        return events, fp.tell()


def check_ports_available(base_port: int, n: int) -> None:
    sockets: list[socket.socket] = []
    try:
        for port in range(base_port, base_port + n):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((LOCALHOST, port))
            except OSError as exc:
                raise RuntimeError(f"port {port} is not available: {exc}") from exc
            sockets.append(sock)
    finally:
        for sock in sockets:
            sock.close()


def build_node_command(
    *,
    python_exec: str,
    port: int,
    bootstrap: str,
    node_seed: int,
    fanout: int,
    ttl: int,
    peer_limit: int,
    ping_interval: int,
    peer_timeout: int,
    pull_interval: int,
    ids_max_ihave: int,
    k_pow: int,
    jsonl_log: Path,
) -> list[str]:
    return [
        python_exec,
        "-m",
        "src",
        "--port",
        str(port),
        "--bootstrap",
        bootstrap,
        "--fanout",
        str(fanout),
        "--ttl",
        str(ttl),
        "--peer-limit",
        str(peer_limit),
        "--ping-interval",
        str(ping_interval),
        "--peer-timeout",
        str(peer_timeout),
        "--seed",
        str(node_seed),
        "--pull-interval",
        str(pull_interval),
        "--ids-max-ihave",
        str(ids_max_ihave),
        "--k-pow",
        str(k_pow),
        "--log-file",
        str(jsonl_log),
    ]


def start_nodes(args: argparse.Namespace, n: int, seed: int, run_dir: Path) -> list[NodeProcess]:
    check_ports_available(args.base_port, n)

    bootstrap = f"{LOCALHOST}:{args.base_port}"
    nodes: list[NodeProcess] = []

    try:
        for index in range(n):
            port = args.base_port + index
            node_seed = (seed * 1000) + index
            jsonl_log = run_dir / f"node_{index}.jsonl"
            stdout_log = run_dir / f"node_{index}_stdout.log"
            stderr_log = run_dir / f"node_{index}_stderr.log"
            command = build_node_command(
                python_exec=args.python_exec,
                port=port,
                bootstrap=bootstrap,
                node_seed=node_seed,
                fanout=args.fanout,
                ttl=args.ttl,
                peer_limit=args.peer_limit,
                ping_interval=args.ping_interval,
                peer_timeout=args.peer_timeout,
                pull_interval=args.pull_interval,
                ids_max_ihave=args.ids_max_ihave,
                k_pow=args.k_pow,
                jsonl_log=jsonl_log,
            )

            stdout_fp = stdout_log.open("w", encoding="utf-8")
            stderr_fp = stderr_log.open("w", encoding="utf-8")
            process = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                stdin=subprocess.PIPE,
                stdout=stdout_fp,
                stderr=stderr_fp,
                text=True,
            )
            node = NodeProcess(
                index=index,
                port=port,
                seed=node_seed,
                bootstrap=bootstrap,
                jsonl_log=jsonl_log,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                command=command,
                process=process,
                stdout_fp=stdout_fp,
                stderr_fp=stderr_fp,
            )
            nodes.append(node)

            if index == 0:
                time.sleep(0.8)
            else:
                time.sleep(0.05)

            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f"node {index} exited immediately with code {exit_code}")
    except Exception:
        terminate_nodes(nodes)
        raise

    return nodes


def terminate_nodes(nodes: list[NodeProcess], graceful_wait_s: float = 3.0) -> None:
    for node in nodes:
        if node.process.poll() is None:
            try:
                node.process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + graceful_wait_s
    while time.monotonic() < deadline:
        if all(node.process.poll() is not None for node in nodes):
            break
        time.sleep(0.1)

    for node in nodes:
        if node.process.poll() is None:
            node.process.kill()

    for node in nodes:
        try:
            node.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass

        if node.process.stdin is not None:
            try:
                node.process.stdin.close()
            except OSError:
                pass
        node.stdout_fp.close()
        node.stderr_fp.close()


def inject_gossip(nodes: list[NodeProcess], origin_index: int, message: str) -> None:
    origin = nodes[origin_index]
    if origin.process.poll() is not None:
        raise RuntimeError("origin node is not running at injection time")
    if origin.process.stdin is None:
        raise RuntimeError("origin stdin is not available")

    origin.process.stdin.write(message + "\n")
    origin.process.stdin.flush()


def wait_for_convergence(
    *,
    nodes: list[NodeProcess],
    n: int,
    origin_index: int,
    run_timeout_seconds: float,
) -> tuple[str, str | None, list[int], int | None]:
    target_nodes = int(math.ceil(0.95 * n))
    observed_nodes: set[int] = set()
    target_msg_id: str | None = None
    convergence_ts_ms: int | None = None
    offsets = {node.index: 0 for node in nodes}
    deadline = time.monotonic() + run_timeout_seconds

    status = "timeout"
    while time.monotonic() < deadline:
        if INTERRUPTED:
            return "interrupted", target_msg_id, sorted(observed_nodes), convergence_ts_ms

        for node in nodes:
            events, new_offset = parse_jsonl_since(node.jsonl_log, offsets[node.index])
            offsets[node.index] = new_offset

            for event in events:
                event_name = event.get("event")
                msg_id = event.get("msg_id")
                if (
                    target_msg_id is None
                    and node.index == origin_index
                    and event_name == "gossip_originated"
                    and isinstance(msg_id, str)
                ):
                    target_msg_id = msg_id

                if (
                    target_msg_id is not None
                    and isinstance(msg_id, str)
                    and msg_id == target_msg_id
                    and event_name in MESSAGE_EVENTS
                ):
                    observed_nodes.add(node.index)

        if len(observed_nodes) >= target_nodes:
            convergence_ts_ms = now_ts_ms()
            status = "converged"
            break

        time.sleep(0.2)

    return status, target_msg_id, sorted(observed_nodes), convergence_ts_ms


def node_process_record(node: NodeProcess) -> dict[str, Any]:
    return {
        "index": node.index,
        "pid": node.process.pid,
        "port": node.port,
        "seed": node.seed,
        "bootstrap": node.bootstrap,
        "command": node.command,
        "stdout_log": str(node.stdout_log),
        "stderr_log": str(node.stderr_log),
        "jsonl_log": str(node.jsonl_log),
        "exit_code": node.process.poll(),
    }


def run_single(args: argparse.Namespace, run_root: Path, run_id: str, n: int, seed: int) -> dict[str, Any]:
    if args.origin_index >= n:
        raise RuntimeError(f"origin-index {args.origin_index} must be in [0, {n - 1}]")

    run_dir = run_root / f"n={n}" / f"seed={seed}"
    run_dir.mkdir(parents=True, exist_ok=False)
    message = f"stage6 gossip n={n} seed={seed}"

    meta: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "start_iso_utc": now_iso_utc(),
        "start_ts_ms": now_ts_ms(),
        "n": n,
        "seed": seed,
        "base_port": args.base_port,
        "origin_index": args.origin_index,
        "gossip_message": message,
        "stabilize_seconds": args.stabilize_seconds,
        "run_timeout_seconds": args.run_timeout_seconds,
        "target_coverage_ratio": 0.95,
        "target_coverage_nodes": int(math.ceil(0.95 * n)),
        "params": {
            "fanout": args.fanout,
            "ttl": args.ttl,
            "peer_limit": args.peer_limit,
            "ping_interval": args.ping_interval,
            "peer_timeout": args.peer_timeout,
            "pull_interval": args.pull_interval,
            "ids_max_ihave": args.ids_max_ihave,
            "k_pow": args.k_pow,
        },
        "status": "failed",
        "interrupted": False,
    }

    nodes: list[NodeProcess] = []
    meta_path = run_dir / "meta.json"
    injected_ts_ms: int | None = None
    converged_nodes: list[int] = []
    convergence_ts_ms: int | None = None
    target_msg_id: str | None = None
    error_message: str | None = None

    try:
        nodes = start_nodes(args, n, seed, run_dir)
        time.sleep(args.stabilize_seconds)
        if INTERRUPTED:
            meta["interrupted"] = True
            meta["status"] = "interrupted"
            return meta

        inject_gossip(nodes, args.origin_index, message)
        injected_ts_ms = now_ts_ms()

        status, target_msg_id, converged_nodes, convergence_ts_ms = wait_for_convergence(
            nodes=nodes,
            n=n,
            origin_index=args.origin_index,
            run_timeout_seconds=args.run_timeout_seconds,
        )
        meta["status"] = status
        meta["interrupted"] = status == "interrupted"
    except KeyboardInterrupt:
        meta["status"] = "interrupted"
        meta["interrupted"] = True
    except Exception as exc:
        error_message = str(exc)
        meta["status"] = "failed"
    finally:
        terminate_nodes(nodes)
        meta["node_processes"] = [node_process_record(node) for node in nodes]
        meta["target_msg_id"] = target_msg_id
        meta["converged_nodes"] = converged_nodes
        meta["convergence_ts_ms"] = convergence_ts_ms
        meta["injected_ts_ms"] = injected_ts_ms
        meta["end_ts_ms"] = now_ts_ms()
        meta["end_iso_utc"] = now_iso_utc()
        meta["duration_ms"] = meta["end_ts_ms"] - meta["start_ts_ms"]
        if error_message is not None:
            meta["error"] = error_message

        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    return meta


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.n is not None:
        n_values = [args.n]
    else:
        n_values = list(dict.fromkeys(args.ns))

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    run_id = make_run_id()
    run_root = args.outdir / run_id
    run_root.mkdir(parents=True, exist_ok=False)

    summary: dict[str, Any] = {
        "run_id": run_id,
        "started_iso_utc": now_iso_utc(),
        "n_values": n_values,
        "seeds": args.seeds,
        "statuses": [],
        "runs": [],
    }
    summary_path = run_root / "summary.json"

    try:
        for n in n_values:
            for seed in args.seeds:
                if INTERRUPTED:
                    break
                run_meta = run_single(args, run_root, run_id, n=n, seed=seed)
                summary["statuses"].append(run_meta["status"])
                summary["runs"].append(
                    {
                        "n": n,
                        "seed": seed,
                        "status": run_meta["status"],
                        "run_dir": run_meta["run_dir"],
                        "meta_path": str(Path(run_meta["run_dir"]) / "meta.json"),
                    }
                )
            if INTERRUPTED:
                break
    except KeyboardInterrupt:
        summary["interrupted"] = True
    finally:
        summary["finished_iso_utc"] = now_iso_utc()
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    if any(status == "failed" for status in summary["statuses"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
