import argparse
import time
from typing import List

import matplotlib.pyplot as plt
import numpy as np

from gossip_node import GossipNode, NodeConfig


def measure_pow_times(node_id: str, k_values: List[int], trials: int) -> List[dict]:
    results = []
    dummy_cfg = NodeConfig(
        port=0,
        bootstrap=None,
        fanout=1,
        ttl=1,
        peer_limit=1,
        ping_interval=1.0,
        peer_timeout=1.0,
        seed=0,
    )
    dummy_node = GossipNode(dummy_cfg)

    for k in k_values:
        times = []
        for _ in range(trials):
            t0 = time.time()
            _ = dummy_node._compute_pow(node_id, k)
            t1 = time.time()
            times.append(t1 - t0)
        arr = np.array(times)
        results.append(
            {
                "pow_k": k,
                "time_avg_s": float(arr.mean()),
                "time_std_s": float(arr.std(ddof=0)),
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark PoW difficulty vs time")
    parser.add_argument("--node-id", type=str, default="benchmark-node")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--k-values", type=int, nargs="+", default=[0, 2, 3, 4])
    parser.add_argument("--out-csv", type=str, default="k_pow_bench/pow_bench.csv")
    parser.add_argument("--out-png", type=str, default="k_pow_bench/pow_bench.png")
    args = parser.parse_args()

    results = measure_pow_times(args.node_id, args.k_values, args.trials)

    for r in results:
        print(
            f"pow_k={r['pow_k']}: avg={r['time_avg_s']:.4f}s std={r['time_std_s']:.4f}s "
            f"(over {args.trials} trials)"
        )

    # ذخیره در CSV
    import csv
    from pathlib import Path

    csv_path = Path(args.out_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pow_k", "time_avg_s", "time_std_s"])
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    # رسم نمودار زمان تولید PoW بر حسب pow_k
    ks = [r["pow_k"] for r in results]
    avgs = [r["time_avg_s"] for r in results]

    plt.figure()
    plt.plot(ks, avgs, marker="o")
    plt.xlabel("pow_k (leading zero hex digits)")
    plt.ylabel("Average PoW generation time (s)")
    plt.title("PoW cost vs difficulty")
    plt.grid(True)
    png_path = Path(args.out_png)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(png_path, dpi=150)


if __name__ == "__main__":
    main()
