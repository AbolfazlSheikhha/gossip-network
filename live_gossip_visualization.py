#!/usr/bin/env python3
"""
Generate animated gossip propagation visualizations for push and hybrid modes.

This script does NOT modify existing simulation/analysis codepaths. It runs
an isolated demo under visualization_outputs/ and renders GIF animations.
"""

import argparse
import json
import math
import random
import re
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.animation as animation
import matplotlib.pyplot as plt


PROJECT_ROOT = Path(__file__).resolve().parent
NODE_SCRIPT = PROJECT_ROOT / "gossip_node.py"

RECV_RE = re.compile(
    r"\bGOSSIP_RECEIVED\b.*?\bport=(?P<port>\d+)\b.*?\bmsg_id=(?P<msg_id>[^\s]+)\b.*?\bat_ms=(?P<at_ms>\d+)\b"
)
SEND_RE = re.compile(
    r"\bSEND\b.*?\btype=GOSSIP\b.*?\bmsg_id=(?P<msg_id>[^\s]+)\b.*?\bdest=(?P<dest>[^:\s]+):(?P<port>\d+)\b.*?\bat_ms=(?P<at_ms>\d+)\b"
)


@dataclass
class RunArtifacts:
    mode: str
    run_dir: Path
    msg_id: str
    base_port: int
    ports: List[int]
    start_times_ms: Dict[int, int]


def _reserve_free_udp_port_block(size: int, tries: int = 100) -> int:
    for i in range(tries):
        base = 14000 + ((int(time.time() * 1000) + i * 911) % 36000)
        if base + size + 1 > 65000:
            base = 20000 + i * 10
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
    raise RuntimeError(f"Could not reserve UDP port block size={size}")


def _start_node(
    *,
    port: int,
    bootstrap: Optional[Tuple[str, int]],
    mode: str,
    fanout: int,
    ttl: int,
    peer_limit: int,
    seed: int,
    pull_interval: float,
    ping_interval: float,
    peer_timeout: float,
    discovery_interval: float,
    logfile: Path,
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        str(NODE_SCRIPT),
        "--port", str(port),
        "--fanout", str(fanout),
        "--ttl", str(ttl),
        "--peer-limit", str(peer_limit),
        "--seed", str(seed),
        "--ping-interval", str(ping_interval),
        "--peer-timeout", str(peer_timeout),
        "--pull-interval", "0" if mode == "push" else str(pull_interval),
        "--discovery-interval", str(discovery_interval),
        "--pow-k", "0",
        "--stdin", "false",
    ]
    if bootstrap is not None:
        cmd += ["--bootstrap", f"{bootstrap[0]}:{bootstrap[1]}"]
    f = open(logfile, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=f,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc._viz_log_file = f  # type: ignore[attr-defined]
    return proc


def _stop_processes(procs: List[subprocess.Popen]) -> None:
    for p in procs:
        if p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)
            except Exception:
                pass
    time.sleep(1.0)
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
        f = getattr(p, "_viz_log_file", None)
        if f:
            try:
                f.close()
            except Exception:
                pass


def _inject_gossip(port: int, ttl: int, text: str) -> str:
    msg_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    msg = {
        "version": 1,
        "msg_id": msg_id,
        "msg_type": "GOSSIP",
        "sender_id": "viz-injector",
        "sender_addr": "127.0.0.1:0",
        "timestamp_ms": now_ms,
        "ttl": ttl,
        "payload": {
            "topic": "viz",
            "data": text,
            "origin_id": "viz-injector",
            "origin_timestamp_ms": now_ms,
        },
    }
    payload = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, ("127.0.0.1", port))
    finally:
        sock.close()
    return msg_id


def _run_demo_for_mode(
    *,
    mode: str,
    out_root: Path,
    n_nodes: int,
    fanout: int,
    ttl: int,
    startup_stagger_s: float,
    warmup_after_start_s: float,
    runtime_s: float,
    pull_interval: float,
    ping_interval: float,
    peer_timeout: float,
    discovery_interval: float,
) -> RunArtifacts:
    run_dir = out_root / mode / f"run_{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    base_port = _reserve_free_udp_port_block(n_nodes)
    ports = [base_port + i for i in range(n_nodes)]
    bootstrap = ("127.0.0.1", base_port)
    start_times_ms: Dict[int, int] = {}

    procs: List[subprocess.Popen] = []
    msg_id = ""
    try:
        for i, p in enumerate(ports):
            start_times_ms[p] = int(time.time() * 1000)
            proc = _start_node(
                port=p,
                bootstrap=None if i == 0 else bootstrap,
                mode=mode,
                fanout=fanout,
                ttl=ttl,
                peer_limit=max(20, n_nodes),
                seed=700 + i,
                pull_interval=pull_interval,
                ping_interval=ping_interval,
                peer_timeout=peer_timeout,
                discovery_interval=discovery_interval,
                logfile=run_dir / f"node_{p}.log",
            )
            procs.append(proc)
            time.sleep(max(0.0, startup_stagger_s))

        time.sleep(max(0.0, warmup_after_start_s))
        msg_id = _inject_gossip(ports[1], ttl, text=f"live-visualization-{mode}")
        time.sleep(max(0.0, runtime_s))
    finally:
        _stop_processes(procs)

    return RunArtifacts(
        mode=mode,
        run_dir=run_dir,
        msg_id=msg_id,
        base_port=base_port,
        ports=ports,
        start_times_ms=start_times_ms,
    )


def _collect_events(
    artifacts: RunArtifacts,
) -> Tuple[List[Tuple[int, int, int]], List[Tuple[int, int]], int, int]:
    """
    Returns:
    - sends: list of (t_ms, src_port, dest_port)
    - recvs: list of (t_ms, recv_port)
    - t0_ms
    - t_end_ms
    """
    sends: List[Tuple[int, int, int]] = []
    recvs: List[Tuple[int, int]] = []

    t0_ms = min(artifacts.start_times_ms.values())
    t_end_ms = t0_ms

    for p in artifacts.ports:
        lf = artifacts.run_dir / f"node_{p}.log"
        if not lf.exists():
            continue
        with lf.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                sm = SEND_RE.search(line)
                if sm and sm.group("msg_id") == artifacts.msg_id:
                    at_ms = int(sm.group("at_ms"))
                    dport = int(sm.group("port"))
                    sends.append((at_ms, p, dport))
                    t_end_ms = max(t_end_ms, at_ms)
                rm = RECV_RE.search(line)
                if rm and rm.group("msg_id") == artifacts.msg_id:
                    at_ms = int(rm.group("at_ms"))
                    rport = int(rm.group("port"))
                    recvs.append((at_ms, rport))
                    t_end_ms = max(t_end_ms, at_ms)

    if t_end_ms <= t0_ms:
        t_end_ms = t0_ms + 1000
    return sends, recvs, t0_ms, t_end_ms


def _positions_random(ports: List[int], seed: int = 1337) -> Dict[int, Tuple[float, float]]:
    """
    Fixed pseudo-random layout so visual flow doesn't imply a ring topology.
    """
    rng = random.Random(seed)
    pos: Dict[int, Tuple[float, float]] = {}
    min_dist = 0.35
    attempts_limit = 2000

    for p in ports:
        placed = False
        for _ in range(attempts_limit):
            x = rng.uniform(-1.0, 1.0)
            y = rng.uniform(-1.0, 1.0)
            if x * x + y * y > 1.05:
                continue
            ok = True
            for qx, qy in pos.values():
                if (x - qx) ** 2 + (y - qy) ** 2 < (min_dist ** 2):
                    ok = False
                    break
            if ok:
                pos[p] = (x, y)
                placed = True
                break
        if not placed:
            # fallback placement on noisy ring if dense
            i = len(pos)
            ang = (2.0 * math.pi * i / max(1, len(ports))) + rng.uniform(-0.2, 0.2)
            r = rng.uniform(0.65, 0.95)
            pos[p] = (r * math.cos(ang), r * math.sin(ang))
    return pos


def _render_gif(
    artifacts: RunArtifacts,
    *,
    fps: int = 10,
    playback_slowdown: float = 3.0,
    min_video_seconds: float = 16.0,
    send_window_ms: int = 700,
    recv_pulse_ms: int = 900,
) -> Path:
    sends, recvs, t0_ms, t_end_ms = _collect_events(artifacts)
    pos = _positions_random(artifacts.ports, seed=artifacts.base_port)
    recv_time_by_port: Dict[int, int] = {}
    for t_ms, p in sorted(recvs, key=lambda x: x[0]):
        recv_time_by_port.setdefault(p, t_ms)

    send_events = sorted(sends, key=lambda x: x[0])
    recv_events = sorted(recvs, key=lambda x: x[0])
    join_events = sorted([(t, p) for p, t in artifacts.start_times_ms.items()], key=lambda x: x[0])

    real_span_ms = max(1200, t_end_ms - t0_ms + 1200)
    video_seconds = max(min_video_seconds, (real_span_ms / 1000.0) * max(1.0, playback_slowdown))
    n_frames = max(45, int(video_seconds * fps))

    fig, ax = plt.subplots(figsize=(8.5, 8.5))
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-1.3, 1.3)
    ax.set_aspect("equal")
    ax.axis("off")

    title = ax.text(0.0, 1.22, "", ha="center", va="center", fontsize=13, fontweight="bold")
    subtitle = ax.text(0.0, 1.14, "", ha="center", va="center", fontsize=10)

    for p in artifacts.ports:
        x, y = pos[p]
        ax.text(x * 1.08, y * 1.08, str(p), fontsize=8, ha="center", va="center")

    out_gif = artifacts.run_dir / f"live_{artifacts.mode}.gif"
    send_line_artists: List = []

    base_xy = [pos[p] for p in artifacts.ports]
    node_scatter = ax.scatter(
        [x for x, _ in base_xy],
        [y for _, y in base_xy],
        c=["#d3d3d3"] * len(base_xy),
        s=[430] * len(base_xy),
        edgecolors="black",
        linewidths=0.7,
        zorder=3,
    )

    def color_for_node(port: int, current_t: int) -> str:
        join_t = artifacts.start_times_ms[port]
        if current_t < join_t:
            return "#d3d3d3"  # not added yet
        recv_t = recv_time_by_port.get(port)
        if recv_t is not None and current_t >= recv_t:
            return "#2ca02c"  # received
        return "#1f77b4"      # active, not received yet

    def update(frame_idx: int):
        for ln in send_line_artists:
            try:
                ln.remove()
            except Exception:
                pass
        send_line_artists.clear()

        progress = frame_idx / max(1, n_frames - 1)
        current_t = t0_ms + int(progress * real_span_ms)
        sim_sec = (current_t - t0_ms) / 1000.0
        video_sec = frame_idx / max(1, fps)

        # Draw send lines as short-lived flashes (300ms window).
        for send_t, src_port, dport in send_events:
            if 0 <= (current_t - send_t) <= send_window_ms:
                sx, sy = pos[src_port]
                dx, dy = pos.get(dport, (sx, sy))
                ln = ax.plot([sx, dx], [sy, dy], color="#ff7f0e", linewidth=2, alpha=0.9)[0]
                send_line_artists.append(ln)

        xs = []
        ys = []
        cs = []
        ss = []
        for p in artifacts.ports:
            x, y = pos[p]
            xs.append(x)
            ys.append(y)
            cs.append(color_for_node(p, current_t))
            recv_t = recv_time_by_port.get(p)
            pulse = 1.0
            if recv_t is not None and 0 <= (current_t - recv_t) <= recv_pulse_ms:
                pulse = 1.6
            ss.append(430 * pulse)

        node_scatter.set_offsets(list(zip(xs, ys)))
        node_scatter.set_sizes(ss)
        node_scatter.set_facecolors(cs)

        joined = sum(1 for _, p in join_events if artifacts.start_times_ms[p] <= current_t)
        received = sum(1 for p, rt in recv_time_by_port.items() if rt <= current_t)
        sends_in_window = sum(1 for t, _, _ in send_events if 0 <= (current_t - t) <= send_window_ms)
        title.set_text(f"Gossip Live Visualization - {artifacts.mode.upper()} mode")
        subtitle.set_text(
            f"sim_t={sim_sec:5.2f}s (video_t={video_sec:5.2f}s) | nodes added={joined}/{len(artifacts.ports)} | "
            f"received={received}/{len(artifacts.ports)} | active sends={sends_in_window} | "
            f"msg_id={artifacts.msg_id[:8]}..."
        )
        return []

    ani = animation.FuncAnimation(fig, update, frames=n_frames, interval=1000 / fps, blit=False)
    writer = animation.PillowWriter(fps=fps)
    ani.save(out_gif, writer=writer)
    plt.close(fig)
    return out_gif


def main() -> None:
    ap = argparse.ArgumentParser(description="Create live animated gossip visualizations for push and hybrid")
    ap.add_argument("--mode", choices=["push", "hybrid", "both"], default="both")
    ap.add_argument("--out-dir", default="visualization_outputs")
    ap.add_argument("--n-nodes", type=int, default=10)
    ap.add_argument("--fanout", type=int, default=3)
    ap.add_argument("--ttl", type=int, default=8)
    ap.add_argument("--startup-stagger-s", type=float, default=0.4)
    ap.add_argument("--warmup-after-start-s", type=float, default=2.0)
    ap.add_argument("--runtime-s", type=float, default=8.0)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--playback-slowdown", type=float, default=3.0, help="Stretch simulation time in output video")
    ap.add_argument("--min-video-seconds", type=float, default=16.0)
    ap.add_argument("--send-window-ms", type=int, default=700, help="How long send links remain visible")
    ap.add_argument("--recv-pulse-ms", type=int, default=900, help="How long node receive pulse lasts")
    ap.add_argument("--pull-interval", type=float, default=2.0)
    ap.add_argument("--ping-interval", type=float, default=2.0)
    ap.add_argument("--peer-timeout", type=float, default=10.0)
    ap.add_argument("--discovery-interval", type=float, default=2.0)
    args = ap.parse_args()

    out_root = Path(args.out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    modes = [args.mode] if args.mode in ("push", "hybrid") else ["push", "hybrid"]
    produced = []
    for mode in modes:
        artifacts = _run_demo_for_mode(
            mode=mode,
            out_root=out_root,
            n_nodes=args.n_nodes,
            fanout=args.fanout,
            ttl=args.ttl,
            startup_stagger_s=args.startup_stagger_s,
            warmup_after_start_s=args.warmup_after_start_s,
            runtime_s=args.runtime_s,
            pull_interval=args.pull_interval,
            ping_interval=args.ping_interval,
            peer_timeout=args.peer_timeout,
            discovery_interval=args.discovery_interval,
        )
        gif_path = _render_gif(
            artifacts,
            fps=max(1, args.fps),
            playback_slowdown=max(1.0, args.playback_slowdown),
            min_video_seconds=max(1.0, args.min_video_seconds),
            send_window_ms=max(100, args.send_window_ms),
            recv_pulse_ms=max(100, args.recv_pulse_ms),
        )
        summary = {
            "mode": mode,
            "run_dir": str(artifacts.run_dir),
            "gif": str(gif_path),
            "msg_id": artifacts.msg_id,
            "node_ports": artifacts.ports,
        }
        with (artifacts.run_dir / "viz_summary.json").open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        produced.append(summary)
        print(json.dumps(summary, indent=2))

    print(f"[DONE] Produced {len(produced)} visualization(s) under {out_root}")


if __name__ == "__main__":
    main()
