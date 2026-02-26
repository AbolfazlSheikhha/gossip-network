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


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def ensure_ports_available(base_port: int, n: int) -> None:
    sockets: list[socket.socket] = []
    try:
        for port in range(base_port, base_port + n):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", port))
            sockets.append(sock)
    except OSError as exc:
        raise RuntimeError(f"port {port} is unavailable: {exc}") from exc
    finally:
        for sock in sockets:
            sock.close()


@dataclass
class NodeSpec:
    index: int
    port: int
    seed: int
    bootstrap: str
    stdout_path: Path
    stderr_path: Path
    jsonl_path: Path
    cmd: list[str]


@dataclass
class NodeProcess:
    spec: NodeSpec
    process: subprocess.Popen[str]
    stdout_fp: TextIO
    stderr_fp: TextIO


class JsonlTail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.remainder = b""

    def set_to_eof(self) -> None:
        if self.path.exists():
            self.offset = self.path.stat().st_size
        self.remainder = b""

    def read_new(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        with self.path.open("rb") as fp:
            fp.seek(self.offset)
            chunk = fp.read()
            self.offset = fp.tell()

        if not chunk and not self.remainder:
            return []

        data = self.remainder + chunk
        lines = data.split(b"\n")
        self.remainder = lines.pop() if lines else b""

        records: list[dict[str, Any]] = []
        for raw_line in lines:
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records


class ExperimentRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo_root = Path(__file__).resolve().parents[1]
        self.run_root = self._make_run_root()
        self.run_started_iso = now_iso_utc()
        self.interrupted = False
        self._active_nodes: list[NodeProcess] = []
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, _sig: int, _frame: Any) -> None:
        self.interrupted = True

    def _make_run_root(self) -> Path:
        outdir = Path(self.args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)

        if self.args.run_id:
            root = outdir / self.args.run_id
            root.mkdir(parents=True, exist_ok=False)
            return root

        base = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = outdir / base
        suffix = 0
        while candidate.exists():
            suffix += 1
            candidate = outdir / f"{base}-{suffix}"
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate

    def _log(self, message: str) -> None:
        stamp = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)

    def _build_node_specs(self, run_dir: Path, n: int, seed: int) -> list[NodeSpec]:
        specs: list[NodeSpec] = []
        bootstrap_addr = f"127.0.0.1:{self.args.base_port}"
        for i in range(n):
            port = self.args.base_port + i
            node_seed = seed * 1000 + i
            node_bootstrap = bootstrap_addr if i > 0 else f"127.0.0.1:{port}"
            stdout_path = run_dir / f"node_{i}_stdout.log"
            stderr_path = run_dir / f"node_{i}_stderr.log"
            jsonl_path = run_dir / f"node_{i}.jsonl"
            cmd = [
                sys.executable,
                "-m",
                "src",
                "--port",
                str(port),
                "--bootstrap",
                node_bootstrap,
                "--fanout",
                str(self.args.fanout),
                "--ttl",
                str(self.args.ttl),
                "--peer-limit",
                str(self.args.peer_limit),
                "--ping-interval",
                str(self.args.ping_interval),
                "--peer-timeout",
                str(self.args.peer_timeout),
                "--seed",
                str(node_seed),
                "--pull-interval",
                str(self.args.pull_interval),
                "--ids-max-ihave",
                str(self.args.ids_max_ihave),
                "--k-pow",
                str(self.args.k_pow),
                "--log-file",
                str(jsonl_path),
            ]
            specs.append(
                NodeSpec(
                    index=i,
                    port=port,
                    seed=node_seed,
                    bootstrap=node_bootstrap,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    jsonl_path=jsonl_path,
                    cmd=cmd,
                )
            )
        return specs

    def _start_node(self, spec: NodeSpec) -> NodeProcess:
        stdout_fp = spec.stdout_path.open("w", encoding="utf-8")
        stderr_fp = spec.stderr_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            spec.cmd,
            cwd=self.repo_root,
            stdin=subprocess.PIPE,
            stdout=stdout_fp,
            stderr=stderr_fp,
            text=True,
            bufsize=1,
        )
        node = NodeProcess(spec=spec, process=process, stdout_fp=stdout_fp, stderr_fp=stderr_fp)
        self._active_nodes.append(node)
        return node

    def _stop_nodes(self, grace_seconds: float) -> dict[int, int | None]:
        nodes = list(self._active_nodes)
        if not nodes:
            return {}

        for node in nodes:
            if node.process.poll() is not None:
                continue
            try:
                node.process.send_signal(signal.SIGINT)
            except OSError:
                pass

        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if all(node.process.poll() is not None for node in nodes):
                break
            time.sleep(0.1)

        for node in nodes:
            if node.process.poll() is not None:
                continue
            try:
                node.process.kill()
            except OSError:
                pass

        exit_codes: dict[int, int | None] = {}
        for node in nodes:
            try:
                node.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            exit_codes[node.spec.index] = node.process.poll()
            if node.process.stdin:
                try:
                    node.process.stdin.close()
                except OSError:
                    pass
            node.stdout_fp.close()
            node.stderr_fp.close()

        self._active_nodes.clear()
        return exit_codes

    def _wait_for_boot(self, node: NodeProcess, wait_seconds: float) -> None:
        time.sleep(wait_seconds)
        rc = node.process.poll()
        if rc is None:
            return
        detail = node.spec.stderr_path.read_text(encoding="utf-8", errors="ignore").strip()
        raise RuntimeError(
            f"node {node.spec.index} exited early with code {rc}"
            + (f": {detail[-240:]}" if detail else "")
        )

    def _scan_until_done(
        self,
        *,
        tails: list[JsonlTail],
        origin_index: int,
        n: int,
        timeout_seconds: float,
    ) -> tuple[bool, str | None, set[int], int | None, str]:
        target_nodes = math.ceil(0.95 * n)
        seen_nodes: set[int] = set()
        target_msg_id: str | None = None
        convergence_ts_ms: int | None = None
        status = "timeout"
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            if self.interrupted:
                status = "interrupted"
                break

            for idx, tail in enumerate(tails):
                for record in tail.read_new():
                    event = record.get("event")
                    msg_id = record.get("msg_id")
                    if event == "gossip_originated" and idx == origin_index and isinstance(msg_id, str):
                        if target_msg_id is None:
                            target_msg_id = msg_id

                    if (
                        target_msg_id
                        and isinstance(msg_id, str)
                        and msg_id == target_msg_id
                        and event in {"gossip_originated", "gossip_first_seen"}
                    ):
                        seen_nodes.add(idx)

            if target_msg_id and len(seen_nodes) >= target_nodes:
                status = "converged"
                convergence_ts_ms = now_ms()
                break

            crashed = [node.spec.index for node in self._active_nodes if node.process.poll() is not None]
            if crashed:
                status = f"node_exited:{','.join(str(i) for i in crashed)}"
                break

            time.sleep(self.args.poll_interval_seconds)

        return status == "converged", target_msg_id, seen_nodes, convergence_ts_ms, status

    def run_single(self, n: int, seed: int) -> dict[str, Any]:
        run_dir = self.run_root / f"n={n}" / f"seed={seed}"
        run_dir.mkdir(parents=True, exist_ok=False)
        meta_path = run_dir / "meta.json"
        start_ts_ms = now_ms()

        gossip_message = self.args.gossip_message or f"stage6 gossip n={n} seed={seed}"
        meta: dict[str, Any] = {
            "run_id": self.run_root.name,
            "n": n,
            "seed": seed,
            "start_ts_ms": start_ts_ms,
            "start_iso_utc": now_iso_utc(),
            "status": "starting",
            "origin_index": self.args.origin_index,
            "gossip_message": gossip_message,
            "stabilize_seconds": self.args.stabilize_seconds,
            "run_timeout_seconds": self.args.run_timeout_seconds,
            "target_coverage_ratio": 0.95,
            "target_coverage_nodes": math.ceil(0.95 * n),
            "base_port": self.args.base_port,
            "params": {
                "fanout": self.args.fanout,
                "ttl": self.args.ttl,
                "peer_limit": self.args.peer_limit,
                "ping_interval": self.args.ping_interval,
                "peer_timeout": self.args.peer_timeout,
                "pull_interval": self.args.pull_interval,
                "ids_max_ihave": self.args.ids_max_ihave,
                "k_pow": self.args.k_pow,
            },
            "node_processes": [],
            "target_msg_id": None,
            "injected_ts_ms": None,
            "converged_nodes": [],
            "convergence_ts_ms": None,
            "end_ts_ms": None,
            "end_iso_utc": None,
            "duration_ms": None,
            "interrupted": False,
            "run_dir": str(run_dir),
        }
        write_json(meta_path, meta)

        try:
            ensure_ports_available(self.args.base_port, n)
        except RuntimeError as exc:
            meta["status"] = "port_check_failed"
            meta["error"] = str(exc)
            meta["end_ts_ms"] = now_ms()
            meta["end_iso_utc"] = now_iso_utc()
            meta["duration_ms"] = meta["end_ts_ms"] - start_ts_ms
            write_json(meta_path, meta)
            return meta

        specs = self._build_node_specs(run_dir, n, seed)
        meta["node_processes"] = [
            {
                "index": spec.index,
                "port": spec.port,
                "seed": spec.seed,
                "bootstrap": spec.bootstrap,
                "stdout_log": str(spec.stdout_path),
                "stderr_log": str(spec.stderr_path),
                "jsonl_log": str(spec.jsonl_path),
                "command": spec.cmd,
                "pid": None,
                "exit_code": None,
            }
            for spec in specs
        ]
        write_json(meta_path, meta)

        try:
            self._log(f"launch n={n} seed={seed} on ports {self.args.base_port}-{self.args.base_port + n - 1}")
            bootstrap_node = self._start_node(specs[0])
            meta["node_processes"][0]["pid"] = bootstrap_node.process.pid
            write_json(meta_path, meta)
            self._wait_for_boot(bootstrap_node, self.args.bootstrap_wait_seconds)

            for spec in specs[1:]:
                if self.interrupted:
                    break
                node = self._start_node(spec)
                meta["node_processes"][spec.index]["pid"] = node.process.pid
                self._wait_for_boot(node, self.args.joiner_wait_seconds)
            write_json(meta_path, meta)

            if self.interrupted:
                meta["status"] = "interrupted"
                return meta

            meta["status"] = "stabilizing"
            write_json(meta_path, meta)
            time.sleep(self.args.stabilize_seconds)

            tails = [JsonlTail(spec.jsonl_path) for spec in specs]
            for tail in tails:
                tail.set_to_eof()

            origin = self._active_nodes[self.args.origin_index]
            if origin.process.stdin is None:
                raise RuntimeError("origin stdin is unavailable")
            origin.process.stdin.write(gossip_message + "\n")
            origin.process.stdin.flush()
            injected_ts_ms = now_ms()
            meta["status"] = "running"
            meta["injected_ts_ms"] = injected_ts_ms
            write_json(meta_path, meta)
            self._log(f"injected gossip from node_{self.args.origin_index} (n={n}, seed={seed})")

            converged, target_msg_id, seen_nodes, convergence_ts_ms, status = self._scan_until_done(
                tails=tails,
                origin_index=self.args.origin_index,
                n=n,
                timeout_seconds=self.args.run_timeout_seconds,
            )
            meta["status"] = "converged" if converged else status
            meta["target_msg_id"] = target_msg_id
            meta["converged_nodes"] = sorted(seen_nodes)
            meta["convergence_ts_ms"] = convergence_ts_ms

        except Exception as exc:
            meta["status"] = "failed"
            meta["error"] = str(exc)
        finally:
            exit_codes = self._stop_nodes(grace_seconds=self.args.shutdown_grace_seconds)
            for i, node_meta in enumerate(meta["node_processes"]):
                if i < len(specs):
                    node_meta["exit_code"] = exit_codes.get(i)
            meta["interrupted"] = self.interrupted or meta["status"] == "interrupted"
            meta["end_ts_ms"] = now_ms()
            meta["end_iso_utc"] = now_iso_utc()
            meta["duration_ms"] = meta["end_ts_ms"] - start_ts_ms
            write_json(meta_path, meta)

        return meta

    def run_all(self) -> int:
        run_matrix: list[tuple[int, int]] = []
        for n in self.args.n_values:
            if self.args.origin_index < 0 or self.args.origin_index >= n:
                raise ValueError(f"origin-index={self.args.origin_index} must be in [0, {n - 1}] for n={n}")
            for seed in self.args.seeds:
                run_matrix.append((n, seed))

        self._log(f"run root: {self.run_root}")
        statuses: list[str] = []
        exit_code = 0
        for n, seed in run_matrix:
            if self.interrupted:
                exit_code = 1
                break
            result = self.run_single(n=n, seed=seed)
            status = str(result.get("status"))
            statuses.append(status)
            self._log(f"finished n={n} seed={seed} status={status}")
            if status in {"port_check_failed", "failed", "interrupted"} or status.startswith("node_exited:"):
                exit_code = 1
                if status != "interrupted":
                    break

        summary = {
            "run_id": self.run_root.name,
            "started_iso_utc": self.run_started_iso,
            "finished_iso_utc": now_iso_utc(),
            "n_values": self.args.n_values,
            "seeds": self.args.seeds,
            "statuses": statuses,
        }
        write_json(self.run_root / "summary.json", summary)
        return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 6 experiment harness for multi-node gossip runs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--n", type=positive_int, help="single network size")
    group.add_argument("--ns", type=positive_int, nargs="+", help="multiple network sizes")

    parser.add_argument("--seeds", type=int, nargs="+", required=True, help="run seeds")
    parser.add_argument("--base-port", type=positive_int, default=5000)
    parser.add_argument("--fanout", type=positive_int, default=3)
    parser.add_argument("--ttl", type=non_negative_int, default=8)
    parser.add_argument("--peer-limit", type=positive_int, default=30)
    parser.add_argument("--ping-interval", type=positive_int, default=1)
    parser.add_argument("--peer-timeout", type=positive_int, default=6)
    parser.add_argument("--pull-interval", type=positive_int, default=2)
    parser.add_argument("--ids-max-ihave", type=positive_int, default=32)
    parser.add_argument("--k-pow", type=non_negative_int, default=0)
    parser.add_argument("--stabilize-seconds", type=positive_float, default=3.0)
    parser.add_argument("--run-timeout-seconds", type=positive_float, default=30.0)
    parser.add_argument("--origin-index", type=non_negative_int, default=0)
    parser.add_argument("--outdir", type=str, default="logs/experiments")
    parser.add_argument("--run-id", type=str, default=None, help="optional explicit run directory name")
    parser.add_argument("--gossip-message", type=str, default=None)
    parser.add_argument("--poll-interval-seconds", type=positive_float, default=0.2)
    parser.add_argument("--bootstrap-wait-seconds", type=positive_float, default=0.8)
    parser.add_argument("--joiner-wait-seconds", type=positive_float, default=0.2)
    parser.add_argument("--shutdown-grace-seconds", type=positive_float, default=3.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.n_values = [args.n] if args.n is not None else args.ns

    try:
        runner = ExperimentRunner(args)
        return runner.run_all()
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
