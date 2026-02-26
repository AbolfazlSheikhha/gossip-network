#!/usr/bin/env python3
import argparse
import math
import re
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

plt.style.use("seaborn-v0_8-whitegrid")
plt.rcParams.update(
    {
        "figure.figsize": (8, 5),
        "axes.titlesize": 12,
        "axes.labelsize": 11,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 10,
    }
)

RUN_RE = re.compile(
    r"run_(?P<tag>[A-Za-z0-9]+)_N(?P<N>\d+)_F(?P<F>\d+)_T(?P<T>\d+)_seed(?P<seed>\d+)_(?P<mode>push|hybrid)_(?P<ts>\d+)"
)
RECV_RE = re.compile(
    r"\bGOSSIP_RECEIVED\b.*?\bmsg_id=([^\s]+)\b.*?\bfrom=([^\s]+)\b.*?\bat_ms=(\d+)\b.*?\borigin_ts=([^\s]+)\b"
)
SEND_RE = re.compile(r"\bSEND\b.*?\btype=([^\s]+)\b(?:.*?\bat_ms=(\d+)\b)?")


def _mean_std(values: List[float]) -> Tuple[float, float]:
    if not values:
        return (math.nan, math.nan)
    if len(values) == 1:
        return (values[0], 0.0)
    return (statistics.mean(values), statistics.stdev(values))


def _parse_run(run_dir: Path, converge_ratio: float) -> Tuple[Optional[Dict], Optional[str]]:
    m = RUN_RE.search(run_dir.name)
    if not m:
        return None, "run name does not match expected format"

    node_logs = sorted(run_dir.glob("node_*.log"))
    if not node_logs:
        return None, "no node logs"

    injected_msg_id = None
    origin_ts = None
    injected_recv_ms = None
    for lf in node_logs:
        with lf.open("r", errors="ignore") as f:
            for line in f:
                rm = RECV_RE.search(line)
                if not rm:
                    continue
                if rm.group(2) != "127.0.0.1:0":
                    continue
                injected_msg_id = rm.group(1)
                injected_recv_ms = int(rm.group(3))
                try:
                    origin_ts = int(rm.group(4))
                except Exception:
                    return None, "invalid origin_ts"
                break
        if injected_msg_id is not None:
            break

    if injected_msg_id is None or origin_ts is None:
        return None, "could not find injected message in logs"

    first_recv_at = {}
    send_events: List[Tuple[Optional[int], str, Optional[str]]] = []
    for lf in node_logs:
        first = None
        with lf.open("r", errors="ignore") as f:
            for line in f:
                sm = SEND_RE.search(line)
                if sm:
                    msg_type = sm.group(1)
                    at_ms = int(sm.group(2)) if sm.group(2) else None
                    msg_id = None
                    msg_id_m = re.search(r"\bmsg_id=([^\s]+)\b", line)
                    if msg_id_m:
                        msg_id = msg_id_m.group(1)
                    send_events.append((at_ms, msg_type, msg_id))
                rm = RECV_RE.search(line)
                if rm and rm.group(1) == injected_msg_id:
                    at_ms = int(rm.group(3))
                    if first is None or at_ms < first:
                        first = at_ms
        if first is not None:
            first_recv_at[lf.name] = first

    received_count = len(first_recv_at)
    total_nodes = len(node_logs)
    if received_count == 0:
        return None, "injected message was not received by any node"

    recv_times = sorted(first_recv_at.values())
    k_needed = max(1, math.ceil(converge_ratio * total_nodes))
    converged = 1 if received_count >= k_needed else 0
    t_conv = recv_times[k_needed - 1] if converged else recv_times[-1]
    convergence_ms = t_conv - origin_ts

    if any(ts is not None for ts, _, _ in send_events):
        overhead_until_conv = sum(1 for ts, _, _ in send_events if ts is not None and ts <= t_conv)
    else:
        overhead_until_conv = len(send_events)
    gossip_target_until_conv = sum(
        1
        for ts, msg_type, msg_id in send_events
        if msg_type == "GOSSIP" and msg_id == injected_msg_id and (ts is None or ts <= t_conv)
    )
    gossip_target_all_time = sum(
        1 for _, msg_type, msg_id in send_events if msg_type == "GOSSIP" and msg_id == injected_msg_id
    )

    return {
        "tag": m.group("tag"),
        "N": int(m.group("N")),
        "fanout": int(m.group("F")),
        "ttl": int(m.group("T")),
        "seed": int(m.group("seed")),
        "mode": m.group("mode"),
        "run_dir": str(run_dir),
        "msg_id": injected_msg_id,
        "origin_ts": origin_ts,
        "injected_recv_ms": injected_recv_ms,
        "convergence_ms": convergence_ms,
        "overhead_until_convergence": overhead_until_conv,
        "message_overhead_target_gossip_until_convergence": gossip_target_until_conv,
        "message_overhead_target_gossip_all_time": gossip_target_all_time,
        "coverage_fraction": received_count / float(total_nodes),
        "converged_to_ratio": converged,
        "required_receivers": k_needed,
        "received_receivers": received_count,
        "node_count": total_nodes,
        "total_sends_all_time": len(send_events),
    }, None


def _collect_runs(logs_root: Path, converge_ratio: float) -> Tuple[List[Dict], List[Tuple[str, str]]]:
    rows: List[Dict] = []
    skipped: List[Tuple[str, str]] = []
    for d in sorted(logs_root.rglob("run_*")):
        if not d.is_dir():
            continue
        row, reason = _parse_run(d, converge_ratio=converge_ratio)
        if row is None:
            skipped.append((str(d), reason or "unknown"))
        else:
            rows.append(row)
    return rows, skipped


def _save_df(df: pd.DataFrame, out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)


def _plot_single_series(df: pd.DataFrame, x_col: str, y_col: str, title: str, ylabel: str, out_png: Path) -> None:
    xs = sorted(df[x_col].unique().tolist())
    means = []
    stds = []
    for x in xs:
        vals = df[df[x_col] == x][y_col].astype(float).tolist()
        m, s = _mean_std(vals)
        means.append(m)
        stds.append(s)
    plt.figure()
    plt.errorbar(xs, means, yerr=stds, marker="o", capsize=5)
    plt.title(title)
    plt.xlabel(x_col)
    plt.ylabel(ylabel)
    plt.grid(True, axis="y", linestyle="--", linewidth=0.5)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=170)
    plt.close()


def _plot_compare_modes(df: pd.DataFrame, x_col: str, y_col: str, title: str, ylabel: str, out_png: Path) -> None:
    plt.figure()
    for mode in ["push", "hybrid"]:
        sub = df[df["mode"] == mode]
        if sub.empty:
            continue
        xs = sorted(sub[x_col].unique().tolist())
        means = []
        stds = []
        for x in xs:
            vals = sub[sub[x_col] == x][y_col].astype(float).tolist()
            m, s = _mean_std(vals)
            means.append(m)
            stds.append(s)
        plt.errorbar(xs, means, yerr=stds, marker="o", capsize=5, label=mode)
    plt.title(title)
    plt.xlabel(x_col)
    plt.ylabel(ylabel)
    plt.grid(True, axis="y", linestyle="--", linewidth=0.5)
    plt.legend()
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=170)
    plt.close()


def _plot_bar_with_error(df: pd.DataFrame, x_col: str, y_col: str, title: str, ylabel: str, out_png: Path) -> None:
    xs = sorted(df[x_col].unique().tolist())
    means = []
    stds = []
    labels = [str(x) for x in xs]
    for x in xs:
        vals = df[df[x_col] == x][y_col].astype(float).tolist()
        m, s = _mean_std(vals)
        means.append(m)
        stds.append(s)
    xpos = list(range(len(xs)))
    plt.figure()
    plt.bar(xpos, means, yerr=stds, capsize=5, alpha=0.85, color="#4c78a8")
    plt.xticks(xpos, labels)
    plt.title(title)
    plt.xlabel(x_col)
    plt.ylabel(ylabel)
    plt.grid(True, axis="y", linestyle="--", linewidth=0.5)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=170)
    plt.close()


def _plot_scatter_with_mean(df: pd.DataFrame, x_col: str, y_col: str, title: str, ylabel: str, out_png: Path) -> None:
    xs = sorted(df[x_col].unique().tolist())
    pos = {x: i for i, x in enumerate(xs)}
    plt.figure()
    for x in xs:
        vals = df[df[x_col] == x][y_col].astype(float).tolist()
        jittered_x = [pos[x] + (0.04 * ((i % 5) - 2)) for i in range(len(vals))]
        plt.scatter(jittered_x, vals, alpha=0.7, s=28, color="#72b7b2")
        m, _ = _mean_std(vals)
        plt.plot([pos[x] - 0.18, pos[x] + 0.18], [m, m], color="#e45756", linewidth=2)
    plt.xticks(list(range(len(xs))), [str(x) for x in xs])
    plt.title(title)
    plt.xlabel(x_col)
    plt.ylabel(ylabel)
    plt.grid(True, axis="y", linestyle="--", linewidth=0.5)
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png, dpi=170)
    plt.close()


def _analyze_part2(results_root: Path, converge_ratio: float) -> int:
    logs_root = results_root / "part2_n_scaling" / "logs"
    out_dir = results_root / "part2_n_scaling" / "analysis"
    rows, skipped = _collect_runs(logs_root, converge_ratio=converge_ratio)
    if not rows:
        print(f"[WARN] part2: no valid runs in {logs_root}")
        return 0

    df = pd.DataFrame(rows)
    _save_df(df, out_dir / "part2_per_run.csv")

    summary = (
        df.groupby(["mode", "N"], as_index=False)
        .agg(
            convergence_mean=("convergence_ms", "mean"),
            convergence_std=("convergence_ms", "std"),
            overhead_mean=("message_overhead_target_gossip_all_time", "mean"),
            overhead_std=("message_overhead_target_gossip_all_time", "std"),
            coverage_mean=("coverage_fraction", "mean"),
            converged_ratio_mean=("converged_to_ratio", "mean"),
            runs=("run_dir", "count"),
        )
    )
    _save_df(summary, out_dir / "part2_summary.csv")

    plots = 0
    _plot_compare_modes(
        df, "N", "convergence_ms",
        "Part2: Convergence vs N (Push vs Hybrid)",
        f"Convergence time to T{int(converge_ratio * 100)} (ms)",
        out_dir / "part2_convergence_vs_n_push_vs_hybrid.png",
    )
    plots += 1
    _plot_compare_modes(
        df, "N", "message_overhead_target_gossip_all_time",
        "Part2: Message Overhead vs N (Push vs Hybrid)",
        "Target injected message GOSSIP forwards",
        out_dir / "part2_overhead_vs_n_push_vs_hybrid.png",
    )
    plots += 1

    for mode in ["push", "hybrid"]:
        sub = df[df["mode"] == mode]
        if sub.empty:
            continue
        _plot_single_series(
            sub, "N", "convergence_ms",
            f"Part2 ({mode}): Convergence vs N",
            f"Convergence time to T{int(converge_ratio * 100)} (ms)",
            out_dir / f"part2_convergence_vs_n_{mode}.png",
        )
        plots += 1
        _plot_single_series(
            sub, "N", "message_overhead_target_gossip_all_time",
            f"Part2 ({mode}): Overhead vs N",
            "Target injected message GOSSIP forwards",
            out_dir / f"part2_overhead_vs_n_{mode}.png",
        )
        plots += 1

    df["N_mode"] = df["N"].astype(str) + "_" + df["mode"]
    _plot_bar_with_error(
        df, "N_mode", "convergence_ms",
        "Part2: Convergence by (N, mode)",
        "Convergence time (ms)",
        out_dir / "part2_convergence_bar_n_mode.png",
    )
    plots += 1
    _plot_bar_with_error(
        df, "N_mode", "message_overhead_target_gossip_all_time",
        "Part2: Overhead by (N, mode)",
        "Target injected message GOSSIP forwards",
        out_dir / "part2_overhead_bar_n_mode.png",
    )
    plots += 1
    _plot_scatter_with_mean(
        df, "N_mode", "convergence_ms",
        "Part2: Convergence per run (with means)",
        "Convergence time (ms)",
        out_dir / "part2_convergence_runs_n_mode.png",
    )
    plots += 1
    _plot_scatter_with_mean(
        df, "N_mode", "message_overhead_target_gossip_all_time",
        "Part2: Overhead per run (with means)",
        "Target injected message GOSSIP forwards",
        out_dir / "part2_overhead_runs_n_mode.png",
    )
    plots += 1

    with (out_dir / "part2_skipped_runs.txt").open("w", encoding="utf-8") as f:
        for name, reason in skipped:
            f.write(f"{name} :: {reason}\n")
    return plots


def _analyze_part3(results_root: Path, converge_ratio: float) -> int:
    ttl_logs = results_root / "part3_push_ttl_fanout" / "logs" / "ttl_sweep"
    fan_logs = results_root / "part3_push_ttl_fanout" / "logs" / "fanout_sweep"
    out_dir = results_root / "part3_push_ttl_fanout" / "analysis"
    ttl_rows, ttl_skipped = _collect_runs(ttl_logs, converge_ratio=converge_ratio)
    fan_rows, fan_skipped = _collect_runs(fan_logs, converge_ratio=converge_ratio)
    if not ttl_rows and not fan_rows:
        print("[WARN] part3: no valid runs found")
        return 0

    plots = 0
    trend_lines: List[str] = []

    if ttl_rows:
        ttl_df = pd.DataFrame(ttl_rows)
        _save_df(ttl_df, out_dir / "part3_ttl_per_run.csv")
        ttl_summary = (
            ttl_df.groupby(["ttl"], as_index=False)
            .agg(
                convergence_mean=("convergence_ms", "mean"),
                convergence_std=("convergence_ms", "std"),
                overhead_mean=("message_overhead_target_gossip_all_time", "mean"),
                overhead_std=("message_overhead_target_gossip_all_time", "std"),
                coverage_mean=("coverage_fraction", "mean"),
                runs=("run_dir", "count"),
            )
            .sort_values("ttl")
        )
        _save_df(ttl_summary, out_dir / "part3_ttl_summary.csv")
        _plot_single_series(
            ttl_df, "ttl", "convergence_ms",
            "Part3 (push): Convergence vs TTL (constant fanout)",
            f"Convergence time to T{int(converge_ratio * 100)} (ms)",
            out_dir / "part3_convergence_vs_ttl.png",
        )
        plots += 1
        _plot_single_series(
            ttl_df, "ttl", "message_overhead_target_gossip_all_time",
            "Part3 (push): Overhead vs TTL (constant fanout)",
            "Target injected message GOSSIP forwards",
            out_dir / "part3_overhead_vs_ttl.png",
        )
        plots += 1
        _plot_bar_with_error(
            ttl_df, "ttl", "convergence_ms",
            "Part3 (push): Convergence by TTL",
            "Convergence time (ms)",
            out_dir / "part3_convergence_bar_ttl.png",
        )
        plots += 1
        _plot_bar_with_error(
            ttl_df, "ttl", "message_overhead_target_gossip_all_time",
            "Part3 (push): Overhead by TTL",
            "Target injected message GOSSIP forwards",
            out_dir / "part3_overhead_bar_ttl.png",
        )
        plots += 1
        _plot_scatter_with_mean(
            ttl_df, "ttl", "convergence_ms",
            "Part3 (push): Convergence per run by TTL",
            "Convergence time (ms)",
            out_dir / "part3_convergence_runs_ttl.png",
        )
        plots += 1
        _plot_scatter_with_mean(
            ttl_df, "ttl", "message_overhead_target_gossip_all_time",
            "Part3 (push): Overhead per run by TTL",
            "Target injected message GOSSIP forwards",
            out_dir / "part3_overhead_runs_ttl.png",
        )
        plots += 1

        ov = ttl_summary["overhead_mean"].tolist()
        monotonic_ov = all(ov[i] <= ov[i + 1] for i in range(len(ov) - 1))
        trend_lines.append(f"TTL sweep overhead monotonic non-decreasing: {monotonic_ov}")

    if fan_rows:
        fan_df = pd.DataFrame(fan_rows)
        _save_df(fan_df, out_dir / "part3_fanout_per_run.csv")
        fan_summary = (
            fan_df.groupby(["fanout"], as_index=False)
            .agg(
                convergence_mean=("convergence_ms", "mean"),
                convergence_std=("convergence_ms", "std"),
                overhead_mean=("message_overhead_target_gossip_all_time", "mean"),
                overhead_std=("message_overhead_target_gossip_all_time", "std"),
                coverage_mean=("coverage_fraction", "mean"),
                runs=("run_dir", "count"),
            )
            .sort_values("fanout")
        )
        _save_df(fan_summary, out_dir / "part3_fanout_summary.csv")
        _plot_single_series(
            fan_df, "fanout", "convergence_ms",
            "Part3 (push): Convergence vs Fanout (constant TTL)",
            f"Convergence time to T{int(converge_ratio * 100)} (ms)",
            out_dir / "part3_convergence_vs_fanout.png",
        )
        plots += 1
        _plot_single_series(
            fan_df, "fanout", "message_overhead_target_gossip_all_time",
            "Part3 (push): Overhead vs Fanout (constant TTL)",
            "Target injected message GOSSIP forwards",
            out_dir / "part3_overhead_vs_fanout.png",
        )
        plots += 1
        _plot_bar_with_error(
            fan_df, "fanout", "convergence_ms",
            "Part3 (push): Convergence by fanout",
            "Convergence time (ms)",
            out_dir / "part3_convergence_bar_fanout.png",
        )
        plots += 1
        _plot_bar_with_error(
            fan_df, "fanout", "message_overhead_target_gossip_all_time",
            "Part3 (push): Overhead by fanout",
            "Target injected message GOSSIP forwards",
            out_dir / "part3_overhead_bar_fanout.png",
        )
        plots += 1
        _plot_scatter_with_mean(
            fan_df, "fanout", "convergence_ms",
            "Part3 (push): Convergence per run by fanout",
            "Convergence time (ms)",
            out_dir / "part3_convergence_runs_fanout.png",
        )
        plots += 1
        _plot_scatter_with_mean(
            fan_df, "fanout", "message_overhead_target_gossip_all_time",
            "Part3 (push): Overhead per run by fanout",
            "Target injected message GOSSIP forwards",
            out_dir / "part3_overhead_runs_fanout.png",
        )
        plots += 1

        conv = fan_summary["convergence_mean"].tolist()
        ov = fan_summary["overhead_mean"].tolist()
        monotonic_conv = all(conv[i] >= conv[i + 1] for i in range(len(conv) - 1))
        monotonic_ov = all(ov[i] <= ov[i + 1] for i in range(len(ov) - 1))
        trend_lines.append(f"Fanout sweep convergence monotonic non-increasing: {monotonic_conv}")
        trend_lines.append(f"Fanout sweep overhead monotonic non-decreasing: {monotonic_ov}")

    with (out_dir / "part3_trend_checks.txt").open("w", encoding="utf-8") as f:
        for line in trend_lines:
            f.write(line + "\n")
    with (out_dir / "part3_skipped_runs.txt").open("w", encoding="utf-8") as f:
        for name, reason in ttl_skipped + fan_skipped:
            f.write(f"{name} :: {reason}\n")
    return plots


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze separated experiment logs and generate plots")
    ap.add_argument("--results-root", default="results")
    ap.add_argument("--part", choices=["all", "part2", "part3"], default="all")
    ap.add_argument("--converge-ratio", type=float, default=0.95, help="Target convergence fraction")
    args = ap.parse_args()

    results_root = Path(args.results_root).resolve()
    total_plots = 0
    if args.part in ("all", "part2"):
        total_plots += _analyze_part2(results_root, converge_ratio=args.converge_ratio)
    if args.part in ("all", "part3"):
        total_plots += _analyze_part3(results_root, converge_ratio=args.converge_ratio)
    print(f"[DONE] Generated {total_plots} plot(s) under {results_root}")


if __name__ == "__main__":
    main()

