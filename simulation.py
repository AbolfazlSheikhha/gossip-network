#!/usr/bin/env python3
import argparse
import csv
import json
import random
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_START_SEED = 50
DEFAULT_BASE_PORT = 8000


def _make_run_dir_name(tag: str, N: int, fanout: int, ttl: int, seed: int, mode: str, suffix: int) -> str:
    return f"run_{tag}_N{N}_F{fanout}_T{ttl}_seed{seed}_{mode}_{suffix}"


def _allocate_free_port_block(n_ports: int, tries: int = 80) -> int:
    for _ in range(tries):
        base = random.randint(12000, 50000 - n_ports - 1)
        sockets = []
        ok = True
        try:
            for p in range(base, base + n_ports):
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
    raise RuntimeError(f"Could not reserve a free UDP port block of size {n_ports}")


def _start_node_process(
    python_exe: str,
    node_script: Path,
    port: int,
    bootstrap: Optional[Tuple[str, int]],
    fanout: int,
    ttl: int,
    peer_limit: int,
    seed: int,
    mode: str,
    pull_interval: float,
    ping_interval: float,
    peer_timeout: float,
    discovery_interval: float,
    ihave_max_ids: int,
    pow_k: int,
    logfile: Path,
) -> subprocess.Popen:
    cmd = [
        python_exe, str(node_script),
        "--port", str(port),
        "--fanout", str(fanout),
        "--ttl", str(ttl),
        "--peer-limit", str(peer_limit),
        "--seed", str(seed),
        "--ping-interval", str(ping_interval),
        "--peer-timeout", str(peer_timeout),
        "--pull-interval", "0" if mode == "push" else str(pull_interval),
        "--discovery-interval", str(discovery_interval),
        "--ihave-max-ids", str(ihave_max_ids),
        "--pow-k", str(pow_k),
        "--stdin", "false",
    ]
    if bootstrap is not None:
        cmd += ["--bootstrap", f"{bootstrap[0]}:{bootstrap[1]}"]

    logfile.parent.mkdir(parents=True, exist_ok=True)
    f = open(logfile, "wb", buffering=0)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )


def _terminate_processes(procs: List[subprocess.Popen], grace_seconds: float = 2.0) -> None:
    for p in procs:
        if p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if all(p.poll() is not None for p in procs):
            return
        time.sleep(0.05)

    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if all(p.poll() is not None for p in procs):
            return
        time.sleep(0.05)

    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def _inject_udp_gossip(port: int, ttl: int, text: str) -> str:
    msg_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    msg = {
        "version": 1,
        "msg_id": msg_id,
        "msg_type": "GOSSIP",
        "sender_id": "injector",
        "sender_addr": "127.0.0.1:0",
        "timestamp_ms": now_ms,
        "ttl": ttl,
        "payload": {
            "topic": "user",
            "data": text,
            "origin_id": "injector",
            "origin_timestamp_ms": now_ms,
        },
    }
    data = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(data, ("127.0.0.1", port))
    finally:
        sock.close()
    return msg_id


def _run_single(
    *,
    node_script: Path,
    out_dir: Path,
    tag: str,
    mode: str,
    N: int,
    ttl: int,
    fanout: int,
    seed: int,
    fixed_base_port: Optional[int],
    warmup_s: float,
    runtime_s: float,
    inject_message: str,
    peer_limit: Optional[int],
    pull_interval: float,
    ping_interval: float,
    peer_timeout: float,
    discovery_interval: float,
    ihave_max_ids: int,
    pow_k: int,
    python_exe: str,
) -> Tuple[Path, int, str]:
    suffix = int(time.time() * 1000)
    run_dir = out_dir / _make_run_dir_name(tag, N, fanout, ttl, seed, mode, suffix)
    run_dir.mkdir(parents=True, exist_ok=True)
    base_port = fixed_base_port if fixed_base_port is not None else _allocate_free_port_block(N)

    plimit = peer_limit if peer_limit is not None else N
    procs: List[subprocess.Popen] = []
    mid = ""
    try:
        bootstrap = ("127.0.0.1", base_port)
        procs.append(
            _start_node_process(
                python_exe, node_script, base_port, None, fanout, ttl, plimit, seed, mode,
                pull_interval, ping_interval, peer_timeout, discovery_interval,
                ihave_max_ids, pow_k, run_dir / f"node_{base_port}.log"
            )
        )
        for i in range(1, N):
            port = base_port + i
            procs.append(
                _start_node_process(
                    python_exe, node_script, port, bootstrap, fanout, ttl, plimit, seed + i, mode,
                    pull_interval, ping_interval, peer_timeout, discovery_interval,
                    ihave_max_ids, pow_k, run_dir / f"node_{port}.log"
                )
            )
        time.sleep(max(0.0, warmup_s))
        mid = _inject_udp_gossip(base_port, ttl, inject_message)
        print(f"[INJECT] msg_id={mid} mode={mode} tag={tag} N={N} F={fanout} T={ttl} port={base_port}")
        time.sleep(max(0.0, runtime_s))
    finally:
        _terminate_processes(procs, grace_seconds=2.0)
    return run_dir, base_port, mid


def _write_manifest_csv(out_root: Path, rows: List[dict]) -> None:
    if not rows:
        return
    manifest = out_root / "run_manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "part",
                "tag",
                "mode",
                "N",
                "fanout",
                "ttl",
                "seed",
                "base_port",
                "msg_id",
                "run_dir",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"[DONE] manifest: {manifest.resolve()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run required Gossip experiments with separated outputs")
    ap.add_argument("--part", choices=["all", "part2", "part3"], default="all")
    ap.add_argument("--mode", choices=["push", "hybrid", "both"], default="both", help="Used for part2")
    ap.add_argument("--out-root", default="results")
    ap.add_argument("--N", type=int, default=None, help="legacy single-N override")
    ap.add_argument("--Ns", type=int, nargs="+", default=[10, 20, 50], help="N values for part2")
    ap.add_argument("--runs-per-n", type=int, default=5)
    ap.add_argument("--base-port", type=int, default=None, help="fixed base port; default allocates per run")
    ap.add_argument("--seed", type=int, default=DEFAULT_START_SEED)
    ap.add_argument("--warmup-s", type=float, default=5.0)
    ap.add_argument("--runtime-s", type=float, default=35.0)
    ap.add_argument("--default-ttl", type=int, default=8)
    ap.add_argument("--default-fanout", type=int, default=3)
    ap.add_argument("--ttl-values", type=int, nargs="+", default=[4, 8, 12])
    ap.add_argument("--fanout-values", type=int, nargs="+", default=[2, 3, 5, 8])
    ap.add_argument("--n-fixed", type=int, default=50, help="N used in part3 sweeps")
    ap.add_argument("--peer-limit", type=int, default=None)
    ap.add_argument("--pull-interval", type=float, default=2.0)
    ap.add_argument("--ping-interval", type=float, default=2.0)
    ap.add_argument("--peer-timeout", type=float, default=8.0)
    ap.add_argument("--discovery-interval", type=float, default=2.0)
    ap.add_argument("--ihave-max-ids", type=int, default=32)
    ap.add_argument("--pow-k", type=int, default=0)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--node-script", default="gossip_node.py")
    ap.add_argument("--inject-message", default="hello")
    args = ap.parse_args()

    out_root = Path(args.out_root)
    node_script = Path(args.node_script)
    n_values = [args.N] if args.N is not None else sorted(set(args.Ns))
    modes = [args.mode] if args.mode in ("push", "hybrid") else ["push", "hybrid"]
    manifest_rows: List[dict] = []

    part2_logs = out_root / "part2_n_scaling" / "logs"
    part3_logs = out_root / "part3_push_ttl_fanout" / "logs"
    (part2_logs / "push").mkdir(parents=True, exist_ok=True)
    (part2_logs / "hybrid").mkdir(parents=True, exist_ok=True)
    (part3_logs / "ttl_sweep").mkdir(parents=True, exist_ok=True)
    (part3_logs / "fanout_sweep").mkdir(parents=True, exist_ok=True)

    if args.part in ("all", "part2"):
        for mode in modes:
            for N in n_values:
                for i in range(args.runs_per_n):
                    seed = args.seed + i
                    tag = "part2"
                    run_dir, base_port, msg_id = _run_single(
                        node_script=node_script,
                        out_dir=part2_logs / mode,
                        tag=tag,
                        mode=mode,
                        N=N,
                        ttl=args.default_ttl,
                        fanout=args.default_fanout,
                        seed=seed,
                        fixed_base_port=args.base_port,
                        warmup_s=args.warmup_s,
                        runtime_s=args.runtime_s,
                        inject_message=args.inject_message,
                        peer_limit=args.peer_limit,
                        pull_interval=args.pull_interval,
                        ping_interval=args.ping_interval,
                        peer_timeout=args.peer_timeout,
                        discovery_interval=args.discovery_interval,
                        ihave_max_ids=args.ihave_max_ids,
                        pow_k=args.pow_k,
                        python_exe=args.python,
                    )
                    manifest_rows.append(
                        {
                            "part": "part2",
                            "tag": tag,
                            "mode": mode,
                            "N": N,
                            "fanout": args.default_fanout,
                            "ttl": args.default_ttl,
                            "seed": seed,
                            "base_port": base_port,
                            "msg_id": msg_id,
                            "run_dir": str(run_dir),
                        }
                    )
                    print(f"[OK] part2 mode={mode} N={N} seed={seed} -> {run_dir}")

    if args.part in ("all", "part3"):
        mode = "push"
        for ttl in sorted(set(args.ttl_values)):
            for i in range(args.runs_per_n):
                seed = args.seed + i
                run_dir, base_port, msg_id = _run_single(
                    node_script=node_script,
                    out_dir=part3_logs / "ttl_sweep",
                    tag="part3ttl",
                    mode=mode,
                    N=args.n_fixed,
                    ttl=ttl,
                    fanout=args.default_fanout,
                    seed=seed,
                    fixed_base_port=args.base_port,
                    warmup_s=args.warmup_s,
                    runtime_s=args.runtime_s,
                    inject_message=args.inject_message,
                    peer_limit=args.peer_limit,
                    pull_interval=args.pull_interval,
                    ping_interval=args.ping_interval,
                    peer_timeout=args.peer_timeout,
                    discovery_interval=args.discovery_interval,
                    ihave_max_ids=args.ihave_max_ids,
                    pow_k=args.pow_k,
                    python_exe=args.python,
                )
                manifest_rows.append(
                    {
                        "part": "part3_ttl",
                        "tag": "part3ttl",
                        "mode": mode,
                        "N": args.n_fixed,
                        "fanout": args.default_fanout,
                        "ttl": ttl,
                        "seed": seed,
                        "base_port": base_port,
                        "msg_id": msg_id,
                        "run_dir": str(run_dir),
                    }
                )
                print(f"[OK] part3 ttl_sweep N={args.n_fixed} F={args.default_fanout} T={ttl} seed={seed} -> {run_dir}")

        for fanout in sorted(set(args.fanout_values)):
            for i in range(args.runs_per_n):
                seed = args.seed + i
                run_dir, base_port, msg_id = _run_single(
                    node_script=node_script,
                    out_dir=part3_logs / "fanout_sweep",
                    tag="part3fan",
                    mode=mode,
                    N=args.n_fixed,
                    ttl=args.default_ttl,
                    fanout=fanout,
                    seed=seed,
                    fixed_base_port=args.base_port,
                    warmup_s=args.warmup_s,
                    runtime_s=args.runtime_s,
                    inject_message=args.inject_message,
                    peer_limit=args.peer_limit,
                    pull_interval=args.pull_interval,
                    ping_interval=args.ping_interval,
                    peer_timeout=args.peer_timeout,
                    discovery_interval=args.discovery_interval,
                    ihave_max_ids=args.ihave_max_ids,
                    pow_k=args.pow_k,
                    python_exe=args.python,
                )
                manifest_rows.append(
                    {
                        "part": "part3_fanout",
                        "tag": "part3fan",
                        "mode": mode,
                        "N": args.n_fixed,
                        "fanout": fanout,
                        "ttl": args.default_ttl,
                        "seed": seed,
                        "base_port": base_port,
                        "msg_id": msg_id,
                        "run_dir": str(run_dir),
                    }
                )
                print(f"[OK] part3 fanout_sweep N={args.n_fixed} F={fanout} T={args.default_ttl} seed={seed} -> {run_dir}")

    _write_manifest_csv(out_root, manifest_rows)
    print(f"[DONE] part2 logs: {(out_root / 'part2_n_scaling' / 'logs').resolve()}")
    print(f"[DONE] part3 logs: {(out_root / 'part3_push_ttl_fanout' / 'logs').resolve()}")


if __name__ == "__main__":
    main()
