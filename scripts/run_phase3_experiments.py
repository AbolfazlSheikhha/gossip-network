#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import suppress
import json
import random
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_NS = [10, 20, 50]
DEFAULT_SEEDS = [1, 2, 3, 4, 5]
DEFAULT_FANOUTS = [2, 3, 4]
DEFAULT_TTLS = [4, 8, 12]
DEFAULT_NEIGHBOR_POLICIES = ["default"]

GROUP_BASELINE = "baseline_by_n"
GROUP_FANOUT = "fanout_sweep"
GROUP_TTL = "ttl_sweep"
GROUP_POLICY = "neighbor_policy_sweep"
KNOWN_GROUPS = [GROUP_BASELINE, GROUP_FANOUT, GROUP_TTL, GROUP_POLICY]

POLICY_FLAG_CANDIDATES = [
    "--neighbor-policy",
    "--neighbour-policy",
    "--peer-policy",
    "--peer-selection-policy",
]


@dataclass(frozen=True)
class ExperimentCase:
    group: str
    n: int
    fanout: int
    ttl: int
    neighbor_policy: str
    run_seed: int


@dataclass(frozen=True)
class NodeProc:
    index: int
    port: int
    seed: int
    command: list[str]
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[str]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 6 harness for Phase 3 simulation scenario and data collection.",
    )
    parser.add_argument(
        "--groups",
        nargs="+",
        choices=KNOWN_GROUPS,
        default=[GROUP_BASELINE],
        help="Experiment groups to run.",
    )
    parser.add_argument("--ns", nargs="+", type=int, default=DEFAULT_NS)
    parser.add_argument("--seeds", nargs="+", type=int, default=DEFAULT_SEEDS)
    parser.add_argument("--fanouts", nargs="+", type=int, default=DEFAULT_FANOUTS)
    parser.add_argument("--ttls", nargs="+", type=int, default=DEFAULT_TTLS)
    parser.add_argument(
        "--neighbor-policies",
        nargs="+",
        default=DEFAULT_NEIGHBOR_POLICIES,
        help="Values to use for policy sweep if runtime supports a policy flag.",
    )

    parser.add_argument("--baseline-fanout", type=int, default=3)
    parser.add_argument("--baseline-ttl", type=int, default=8)
    parser.add_argument("--baseline-neighbor-policy", type=str, default="default")

    parser.add_argument("--sweep-n", type=int, default=20)
    parser.add_argument("--sweep-fanout", type=int, default=3)
    parser.add_argument("--sweep-ttl", type=int, default=8)
    parser.add_argument("--sweep-neighbor-policy", type=str, default="default")

    parser.add_argument("--peer-limit", type=int, default=30)
    parser.add_argument("--ping-interval", type=int, default=1)
    parser.add_argument("--peer-timeout", type=int, default=6)
    parser.add_argument("--pull-interval", type=int, default=2)
    parser.add_argument("--ids-max-ihave", type=int, default=32)
    parser.add_argument("--k-pow", type=int, default=0)
    parser.add_argument("--base-port", type=int, default=9700)
    parser.add_argument("--stabilize-seconds", type=float, default=3.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--origin-index", type=int, default=0)

    parser.add_argument(
        "--entrypoint",
        type=str,
        default="python3 -m src",
        help="Node command prefix used to launch a single node process.",
    )
    parser.add_argument(
        "--neighbor-policy-flag",
        type=str,
        default="auto",
        help="Set explicit flag (for example --neighbor-policy) or 'auto'.",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        default=None,
        help="Output root directory. Default: logs/phase3/<UTC-stamp>.",
    )
    parser.add_argument(
        "--message-text",
        type=str,
        default=None,
        help="Optional fixed message text. By default a deterministic per-run text is generated.",
    )
    parser.add_argument(
        "--allow-noncompliant",
        action="store_true",
        help="Allow smaller sweeps/runs for smoke tests (not Phase 3 compliant).",
    )
    return parser.parse_args(argv)


def validate_common_args(args: argparse.Namespace) -> None:
    if args.origin_index < 0:
        raise ValueError("--origin-index must be >= 0")
    if args.base_port < 1 or args.base_port > 65535:
        raise ValueError("--base-port must be between 1 and 65535")
    if args.stabilize_seconds < 0:
        raise ValueError("--stabilize-seconds must be >= 0")
    if args.run_timeout_seconds <= 0:
        raise ValueError("--run-timeout-seconds must be > 0")
    if args.peer_limit <= 0 or args.ping_interval <= 0 or args.peer_timeout <= 0:
        raise ValueError("peer and liveness parameters must be positive")
    if args.baseline_fanout <= 0 or args.baseline_ttl <= 0:
        raise ValueError("--baseline-fanout and --baseline-ttl must be > 0")
    if args.sweep_n <= 0 or args.sweep_fanout <= 0 or args.sweep_ttl <= 0:
        raise ValueError("--sweep-n, --sweep-fanout and --sweep-ttl must be > 0")
    if args.base_port + max(args.ns + [args.sweep_n]) - 1 > 65535:
        raise ValueError("--base-port + max(N) exceeds max UDP port 65535")

    for name, values in (
        ("--ns", args.ns),
        ("--seeds", args.seeds),
        ("--fanouts", args.fanouts),
        ("--ttls", args.ttls),
    ):
        if not values:
            raise ValueError(f"{name} must not be empty")
        if any(value <= 0 for value in values):
            raise ValueError(f"{name} values must be > 0")

    if not args.allow_noncompliant:
        if len(set(args.seeds)) < 5:
            raise ValueError("Phase 3 requires at least 5 distinct --seeds values")
        if GROUP_FANOUT in args.groups and len(set(args.fanouts)) < 3:
            raise ValueError("Fanout sweep requires at least 3 distinct --fanouts values")
        if GROUP_TTL in args.groups and len(set(args.ttls)) < 3:
            raise ValueError("TTL sweep requires at least 3 distinct --ttls values")
        if GROUP_POLICY in args.groups and len(set(args.neighbor_policies)) < 2:
            raise ValueError("Neighbor policy sweep requires at least 2 distinct --neighbor-policies values")


def load_node_help(entrypoint_cmd: list[str]) -> tuple[str, set[str]]:
    command = [*entrypoint_cmd, "--help"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    help_text = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to probe node help via {' '.join(command)} (exit={result.returncode})"
        )
    options: set[str] = set()
    for token in help_text.split():
        if token.startswith("--"):
            options.add(token.rstrip(","))
    return help_text, options


def detect_neighbor_policy_flag(
    args: argparse.Namespace,
    supported_options: set[str],
) -> str | None:
    requested = args.neighbor_policy_flag
    if requested != "auto":
        return requested
    for candidate in POLICY_FLAG_CANDIDATES:
        if candidate in supported_options:
            return candidate
    return None


def build_cases(
    args: argparse.Namespace,
    neighbor_policy_supported: bool,
) -> tuple[list[ExperimentCase], list[dict[str, Any]]]:
    cases: list[ExperimentCase] = []
    skipped: list[dict[str, Any]] = []

    distinct_seeds = sorted(set(args.seeds))
    ns = sorted(set(args.ns))
    fanouts = sorted(set(args.fanouts))
    ttls = sorted(set(args.ttls))
    policies = list(dict.fromkeys(args.neighbor_policies))

    if GROUP_BASELINE in args.groups:
        for n in ns:
            for run_seed in distinct_seeds:
                cases.append(
                    ExperimentCase(
                        group=GROUP_BASELINE,
                        n=n,
                        fanout=args.baseline_fanout,
                        ttl=args.baseline_ttl,
                        neighbor_policy=args.baseline_neighbor_policy,
                        run_seed=run_seed,
                    )
                )

    if GROUP_FANOUT in args.groups:
        for fanout in fanouts:
            for run_seed in distinct_seeds:
                cases.append(
                    ExperimentCase(
                        group=GROUP_FANOUT,
                        n=args.sweep_n,
                        fanout=fanout,
                        ttl=args.sweep_ttl,
                        neighbor_policy=args.sweep_neighbor_policy,
                        run_seed=run_seed,
                    )
                )

    if GROUP_TTL in args.groups:
        for ttl in ttls:
            for run_seed in distinct_seeds:
                cases.append(
                    ExperimentCase(
                        group=GROUP_TTL,
                        n=args.sweep_n,
                        fanout=args.sweep_fanout,
                        ttl=ttl,
                        neighbor_policy=args.sweep_neighbor_policy,
                        run_seed=run_seed,
                    )
                )

    if GROUP_POLICY in args.groups:
        if not neighbor_policy_supported:
            skipped.append(
                {
                    "group": GROUP_POLICY,
                    "reason": "neighbor_policy_not_supported_by_runtime",
                }
            )
        else:
            for policy in policies:
                for run_seed in distinct_seeds:
                    cases.append(
                        ExperimentCase(
                            group=GROUP_POLICY,
                            n=args.sweep_n,
                            fanout=args.sweep_fanout,
                            ttl=args.sweep_ttl,
                            neighbor_policy=policy,
                            run_seed=run_seed,
                        )
                    )

    return cases, skipped


def sanitize_policy(policy: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in policy.strip())
    return cleaned or "default"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def derive_node_seeds(run_seed: int, n: int) -> list[int]:
    rng = random.Random(run_seed)
    seeds: list[int] = []
    seen: set[int] = set()
    while len(seeds) < n:
        candidate = rng.randrange(1, 2_147_483_647)
        if candidate in seen:
            continue
        seen.add(candidate)
        seeds.append(candidate)
    return seeds


def check_ports_available(host: str, start_port: int, count: int) -> list[int]:
    unavailable: list[int] = []
    sockets: list[socket.socket] = []
    try:
        for port in range(start_port, start_port + count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((host, port))
            except OSError:
                unavailable.append(port)
                sock.close()
                continue
            sockets.append(sock)
    finally:
        for sock in sockets:
            sock.close()
    return unavailable


def wait_for_jsonl_path(log_dir: Path, port: int, timeout_seconds: float) -> Path | None:
    deadline = time.monotonic() + timeout_seconds
    pattern = f"node-{port}-*.jsonl"
    while time.monotonic() < deadline:
        matches = sorted(log_dir.glob(pattern))
        if matches:
            return matches[-1]
        time.sleep(0.1)
    return None


def read_new_jsonl_records(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], offset
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        fp.seek(offset)
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        new_offset = fp.tell()
    return records, new_offset


def wait_for_originated_msg_id(
    jsonl_path: Path | None,
    earliest_ts_ms: int,
    timeout_seconds: float,
) -> str | None:
    if jsonl_path is None:
        return None

    deadline = time.monotonic() + timeout_seconds
    offset = 0
    while time.monotonic() < deadline:
        records, offset = read_new_jsonl_records(jsonl_path, offset)
        for record in records:
            if record.get("event") != "gossip_originated":
                continue
            ts_ms = record.get("ts_ms")
            if isinstance(ts_ms, int) and ts_ms < earliest_ts_ms:
                continue
            msg_id = record.get("msg_id")
            if isinstance(msg_id, str) and msg_id:
                return msg_id
        time.sleep(0.1)
    return None


def all_exit_codes(nodes: list[NodeProc]) -> dict[str, int | None]:
    return {f"node_{node.index}": node.process.poll() for node in nodes}


def terminate_nodes(nodes: list[NodeProc], grace_seconds: float = 3.0) -> dict[str, int | None]:
    for node in nodes:
        if node.process.poll() is None:
            node.process.terminate()

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if all(node.process.poll() is not None for node in nodes):
            break
        time.sleep(0.1)

    for node in nodes:
        if node.process.poll() is None:
            node.process.kill()

    for node in nodes:
        with suppress(Exception):
            node.process.wait(timeout=5)

    return all_exit_codes(nodes)


def make_case_run_dir(outdir: Path, case: ExperimentCase) -> Path:
    return (
        outdir
        / case.group
        / f"n={case.n}"
        / f"fanout={case.fanout}"
        / f"ttl={case.ttl}"
        / f"policy={sanitize_policy(case.neighbor_policy)}"
        / f"seed={case.run_seed}"
    )


def default_message_text(case: ExperimentCase, origin_index: int) -> str:
    return (
        f"phase3|group={case.group}|n={case.n}|fanout={case.fanout}|ttl={case.ttl}"
        f"|policy={case.neighbor_policy}|seed={case.run_seed}|origin={origin_index}"
    )


def run_case(
    *,
    case: ExperimentCase,
    case_index: int,
    total_cases: int,
    args: argparse.Namespace,
    entrypoint_cmd: list[str],
    policy_flag: str | None,
    supports_log_dir: bool,
    stop_requested: dict[str, bool],
    summary_records: list[dict[str, Any]],
) -> None:
    run_dir = make_case_run_dir(Path(args.outdir), case)
    nodes_dir = run_dir / "nodes"
    jsonl_dir = run_dir / "jsonl"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    jsonl_dir.mkdir(parents=True, exist_ok=True)

    node_seeds = derive_node_seeds(case.run_seed, case.n)
    node_seed_map = {str(index): seed for index, seed in enumerate(node_seeds)}
    bootstrap_port = args.base_port
    message_text = args.message_text or default_message_text(case, args.origin_index)
    run_started_iso = utc_now_iso()

    meta: dict[str, Any] = {
        "harness_stage": "stage6_phase3_harness",
        "case_index": case_index,
        "case_count": total_cases,
        "status": "starting",
        "group": case.group,
        "n": case.n,
        "seed": case.run_seed,
        "node_seeds": node_seed_map,
        "fanout": case.fanout,
        "ttl": case.ttl,
        "peer_limit": args.peer_limit,
        "ping_interval": args.ping_interval,
        "peer_timeout": args.peer_timeout,
        "neighbor_policy": case.neighbor_policy,
        "origin_index": args.origin_index,
        "injected_message_text": message_text,
        "run_start_time_utc": run_started_iso,
        "bootstrap_port": bootstrap_port,
        "entrypoint_command": " ".join(entrypoint_cmd),
        "target_msg_id": None,
        "node_commands": {},
        "node_ports": {str(i): args.base_port + i for i in range(case.n)},
        "node_stdout": {},
        "node_stderr": {},
        "node_jsonl": {},
        "notes": [],
    }
    meta_path = run_dir / "meta.json"
    write_json(meta_path, meta)

    unavailable_ports = check_ports_available("127.0.0.1", args.base_port, case.n)
    if unavailable_ports:
        meta["status"] = "failed"
        meta["error"] = f"port_conflict:{','.join(str(port) for port in unavailable_ports)}"
        meta["run_end_time_utc"] = utc_now_iso()
        write_json(meta_path, meta)
        summary_records.append(
            {
                "group": case.group,
                "n": case.n,
                "fanout": case.fanout,
                "ttl": case.ttl,
                "policy": case.neighbor_policy,
                "seed": case.run_seed,
                "status": meta["status"],
                "run_dir": str(run_dir),
                "error": meta["error"],
            }
        )
        return

    if args.origin_index >= case.n:
        meta["status"] = "failed"
        meta["error"] = f"origin_index_out_of_bounds:{args.origin_index}"
        meta["run_end_time_utc"] = utc_now_iso()
        write_json(meta_path, meta)
        summary_records.append(
            {
                "group": case.group,
                "n": case.n,
                "fanout": case.fanout,
                "ttl": case.ttl,
                "policy": case.neighbor_policy,
                "seed": case.run_seed,
                "status": meta["status"],
                "run_dir": str(run_dir),
                "error": meta["error"],
            }
        )
        return

    if policy_flag is None and case.neighbor_policy != "default":
        meta["status"] = "failed"
        meta["error"] = "neighbor_policy_requested_but_runtime_has_no_policy_flag"
        meta["run_end_time_utc"] = utc_now_iso()
        write_json(meta_path, meta)
        summary_records.append(
            {
                "group": case.group,
                "n": case.n,
                "fanout": case.fanout,
                "ttl": case.ttl,
                "policy": case.neighbor_policy,
                "seed": case.run_seed,
                "status": meta["status"],
                "run_dir": str(run_dir),
                "error": meta["error"],
            }
        )
        return

    nodes: list[NodeProc] = []
    launch_error: str | None = None
    interrupted = False

    try:
        for index in range(case.n):
            port = args.base_port + index
            seed = node_seeds[index]
            bootstrap = f"127.0.0.1:{bootstrap_port}"
            command = [
                *entrypoint_cmd,
                "--port",
                str(port),
                "--bootstrap",
                bootstrap,
                "--fanout",
                str(case.fanout),
                "--ttl",
                str(case.ttl),
                "--peer-limit",
                str(args.peer_limit),
                "--ping-interval",
                str(args.ping_interval),
                "--peer-timeout",
                str(args.peer_timeout),
                "--seed",
                str(seed),
                "--pull-interval",
                str(args.pull_interval),
                "--ids-max-ihave",
                str(args.ids_max_ihave),
                "--k-pow",
                str(args.k_pow),
            ]
            if supports_log_dir:
                command.extend(["--log-dir", str(jsonl_dir)])
            if policy_flag is not None:
                command.extend([policy_flag, case.neighbor_policy])

            stdout_path = nodes_dir / f"node-{index:02d}.stdout.log"
            stderr_path = nodes_dir / f"node-{index:02d}.stderr.log"
            stdout_file = stdout_path.open("w", encoding="utf-8")
            stderr_file = stderr_path.open("w", encoding="utf-8")

            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    bufsize=1,
                )
            except Exception as exc:
                stdout_file.close()
                stderr_file.close()
                launch_error = f"node_{index}_launch_error:{exc}"
                break
            nodes.append(
                NodeProc(
                    index=index,
                    port=port,
                    seed=seed,
                    command=command,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    process=process,
                )
            )

            meta["node_commands"][str(index)] = " ".join(command)
            meta["node_stdout"][str(index)] = str(stdout_path)
            meta["node_stderr"][str(index)] = str(stderr_path)
            write_json(meta_path, meta)

            time.sleep(0.25 if index == 0 else 0.05)
            if process.poll() is not None:
                launch_error = f"node_{index}_exited_early:{process.poll()}"
                break

        if launch_error is not None:
            meta["status"] = "failed"
            meta["error"] = launch_error
            return

        for node in nodes:
            jsonl_path = wait_for_jsonl_path(jsonl_dir, node.port, timeout_seconds=5.0)
            if jsonl_path is not None:
                meta["node_jsonl"][str(node.index)] = str(jsonl_path)
            else:
                meta["notes"].append(f"node_{node.index}_jsonl_not_found_yet")
            write_json(meta_path, meta)

        stabilize_deadline = time.monotonic() + args.stabilize_seconds
        while time.monotonic() < stabilize_deadline:
            if stop_requested["stop"]:
                interrupted = True
                break
            dead_nodes = [node.index for node in nodes if node.process.poll() is not None]
            if dead_nodes:
                launch_error = f"nodes_exited_during_stabilization:{dead_nodes}"
                break
            time.sleep(0.1)

        if interrupted:
            meta["status"] = "interrupted"
            meta["error"] = "keyboard_interrupt"
            return
        if launch_error is not None:
            meta["status"] = "failed"
            meta["error"] = launch_error
            return

        origin = nodes[args.origin_index]
        origin_stdin = origin.process.stdin
        if origin_stdin is None:
            meta["status"] = "failed"
            meta["error"] = f"origin_stdin_unavailable:{args.origin_index}"
            return

        inject_ts_ms = int(time.time() * 1000)
        origin_stdin.write(message_text + "\n")
        origin_stdin.flush()
        meta["injection_time_utc"] = utc_now_iso()
        write_json(meta_path, meta)

        origin_jsonl = None
        origin_jsonl_raw = meta["node_jsonl"].get(str(args.origin_index))
        if isinstance(origin_jsonl_raw, str):
            origin_jsonl = Path(origin_jsonl_raw)

        target_msg_id = wait_for_originated_msg_id(
            origin_jsonl,
            earliest_ts_ms=inject_ts_ms,
            timeout_seconds=min(10.0, args.run_timeout_seconds),
        )
        if target_msg_id is not None:
            meta["target_msg_id"] = target_msg_id
        else:
            meta["notes"].append("target_msg_id_not_found_from_origin_log")
        write_json(meta_path, meta)

        run_deadline = time.monotonic() + args.run_timeout_seconds
        while time.monotonic() < run_deadline:
            if stop_requested["stop"]:
                interrupted = True
                break
            dead_nodes = [node.index for node in nodes if node.process.poll() is not None]
            if dead_nodes:
                meta["notes"].append(f"nodes_exited_before_timeout:{dead_nodes}")
                break
            time.sleep(0.2)

        if interrupted:
            meta["status"] = "interrupted"
            meta["error"] = "keyboard_interrupt"
        elif meta.get("status") != "failed":
            meta["status"] = "completed"
    finally:
        exit_codes = terminate_nodes(nodes)
        for node in nodes:
            with suppress(Exception):
                if node.process.stdin is not None:
                    node.process.stdin.close()
            with suppress(Exception):
                if node.process.stdout is not None:
                    node.process.stdout.close()
            with suppress(Exception):
                if node.process.stderr is not None:
                    node.process.stderr.close()

        meta["exit_codes"] = exit_codes
        meta["run_end_time_utc"] = utc_now_iso()
        if "status" not in meta:
            meta["status"] = "failed"
        write_json(meta_path, meta)

        summary_records.append(
            {
                "group": case.group,
                "n": case.n,
                "fanout": case.fanout,
                "ttl": case.ttl,
                "policy": case.neighbor_policy,
                "seed": case.run_seed,
                "status": meta["status"],
                "run_dir": str(run_dir),
                "target_msg_id": meta.get("target_msg_id"),
                "error": meta.get("error"),
            }
        )


def install_signal_handlers(stop_requested: dict[str, bool]) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        stop_requested["stop"] = True
        print(f"[phase3-harness] received signal {signum}, stopping...", file=sys.stderr, flush=True)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validate_common_args(args)

    entrypoint_cmd = shlex.split(args.entrypoint)
    if not entrypoint_cmd:
        raise ValueError("--entrypoint must not be empty")

    if args.outdir is None:
        args.outdir = str(Path("logs") / "phase3" / utc_stamp())
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    help_text, supported_options = load_node_help(entrypoint_cmd)
    policy_flag = detect_neighbor_policy_flag(args, supported_options)
    supports_policy = policy_flag is not None
    supports_log_dir = "--log-dir" in supported_options

    cases, skipped_groups = build_cases(args, neighbor_policy_supported=supports_policy)
    summary: dict[str, Any] = {
        "generated_at_utc": utc_now_iso(),
        "outdir": str(outdir),
        "groups_requested": args.groups,
        "groups_skipped": skipped_groups,
        "runtime_capabilities": {
            "supports_log_dir_flag": supports_log_dir,
            "detected_neighbor_policy_flag": policy_flag,
        },
        "entrypoint": " ".join(entrypoint_cmd),
        "entrypoint_help_excerpt": "\n".join(help_text.splitlines()[:60]),
        "run_count": len(cases),
        "runs": [],
    }
    write_json(outdir / "summary.json", summary)

    if not supports_log_dir:
        raise RuntimeError(
            "Runtime does not expose --log-dir, which is required to keep per-run JSONL logs structured."
        )

    stop_requested = {"stop": False}
    install_signal_handlers(stop_requested)

    total = len(cases)
    for index, case in enumerate(cases, start=1):
        if stop_requested["stop"]:
            break
        print(
            (
                f"[phase3-harness] {index}/{total} group={case.group} n={case.n} "
                f"fanout={case.fanout} ttl={case.ttl} policy={case.neighbor_policy} seed={case.run_seed}"
            ),
            flush=True,
        )
        run_case(
            case=case,
            case_index=index,
            total_cases=total,
            args=args,
            entrypoint_cmd=entrypoint_cmd,
            policy_flag=policy_flag,
            supports_log_dir=supports_log_dir,
            stop_requested=stop_requested,
            summary_records=summary["runs"],
        )
        write_json(outdir / "summary.json", summary)

    summary["completed_at_utc"] = utc_now_iso()
    write_json(outdir / "summary.json", summary)
    completed = sum(1 for run in summary["runs"] if run.get("status") == "completed")
    failed = sum(1 for run in summary["runs"] if run.get("status") == "failed")
    interrupted = sum(1 for run in summary["runs"] if run.get("status") == "interrupted")
    print(
        (
            f"[phase3-harness] done: completed={completed} failed={failed} interrupted={interrupted} "
            f"skipped_groups={len(skipped_groups)} outdir={outdir}"
        ),
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
