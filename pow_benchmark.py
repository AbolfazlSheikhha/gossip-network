import argparse
import statistics
import time
from typing import List

from gossip_node import GossipNode, NodeConfig


def measure_pow_time(k: int, samples: int) -> List[float]:
    node_cfg = NodeConfig(
        port=0,
        bootstrap=None,
        fanout=1,
        ttl=1,
        peer_limit=1,
        ping_interval=1.0,
        peer_timeout=1.0,
        seed=42,
    )
    node = GossipNode(node_cfg)
    times: List[float] = []
    for _ in range(samples):
        t0 = time.perf_counter()
        node._compute_pow(node.node_id, k)
        t1 = time.perf_counter()
        times.append(t1 - t0)
    return times


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark PoW generation time for different pow_k values")
    parser.add_argument("--k-values", type=int, nargs="+", default=[2, 3, 4])
    parser.add_argument("--samples", type=int, default=5)
    args = parser.parse_args()

    print("pow_k, avg_time_s, min_time_s, max_time_s")
    for k in args.k_values:
        times = measure_pow_time(k, args.samples)
        avg_t = statistics.mean(times)
        min_t = min(times)
        max_t = max(times)
        print(f"{k}, {avg_t:.4f}, {min_t:.4f}, {max_t:.4f}")


if __name__ == "__main__":
    main()

