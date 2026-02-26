# Stage 6 - Experiment Harness (Automated Multi-Node Runs + Log Collection)

## What Was Added

- New runner script: `scripts/run_experiments.py`
- Minimal node CLI extension: `--log-file` (optional) to write each node JSONL into the run directory directly.

The runner automates reproducible localhost experiments for Stage 6:
- launches `N` nodes (`python -m src`) on deterministic ports
- starts bootstrap node first, then joiners with `--bootstrap 127.0.0.1:<base_port>`
- derives per-node seeds deterministically: `node_seed = seed * 1000 + node_index`
- injects exactly one gossip line through origin node `stdin`
- monitors logs for convergence (`>= ceil(0.95 * N)` nodes seeing the same `msg_id`)
- stops on convergence or timeout
- terminates all processes cleanly (SIGINT first, SIGKILL fallback)
- writes structured artifacts (`meta.json`, stdout/stderr/jsonl logs, `summary.json`)

## CLI Usage

Single run example:

```bash
python3 scripts/run_experiments.py \
  --n 10 \
  --seeds 1 \
  --base-port 5000 \
  --fanout 3 --ttl 8 \
  --peer-limit 30 \
  --ping-interval 1 --peer-timeout 6 \
  --stabilize-seconds 1.5 \
  --run-timeout-seconds 20 \
  --origin-index 0 \
  --outdir logs/experiments_test
```

Simulation scenario (N = 10, 20, 50; 5 seeds):

```bash
python3 scripts/run_experiments.py \
  --ns 10 20 50 \
  --seeds 1 2 3 4 5 \
  --base-port 5000 \
  --fanout 3 --ttl 8 \
  --peer-limit 30 \
  --ping-interval 1 --peer-timeout 6 \
  --stabilize-seconds 2 \
  --run-timeout-seconds 45 \
  --origin-index 0 \
  --outdir logs/experiments
```

## Output Structure

For each invocation, a UTC run id directory is created:

```text
logs/experiments/<RUN_ID>/
  summary.json
  n=10/
    seed=1/
      meta.json
      node_0.jsonl
      node_0_stdout.log
      node_0_stderr.log
      ...
      node_9.jsonl
  n=20/
    seed=1/
      ...
  n=50/
    seed=5/
      ...
```

Per run directory:
- `meta.json`: run parameters and outcome
- `node_<i>.jsonl`: structured node events (including `gossip_originated`, `gossip_first_seen`, `send_ok`, control-plane events)
- `node_<i>_stdout.log`, `node_<i>_stderr.log`: process output streams

Top-level:
- `summary.json`: all `(N, seed)` statuses for the invocation

## `meta.json` Content

Each run writes at least:
- run identity and timing: `run_id`, `run_dir`, `start_ts_ms`, `end_ts_ms`, `duration_ms`
- experiment config: `n`, `seed`, `base_port`, `origin_index`, `params`
- stopping config: `stabilize_seconds`, `run_timeout_seconds`, `target_coverage_ratio`, `target_coverage_nodes`
- gossip info: `gossip_message`, `injected_ts_ms`, `target_msg_id`
- convergence info: `status`, `converged_nodes`, `convergence_ts_ms`
- process records: `node_processes[]` with command, pid, exit code, per-node log paths

## Gossip Injection Method

- Nodes are started with `stdin=PIPE`.
- After stabilization delay, the runner writes one non-empty line to origin node stdin:
  - message format: `stage6 gossip n=<N> seed=<seed>`
- The origin logs `gossip_originated` in its JSONL with `msg_id`.
- Runner tracks that `msg_id` as `target_msg_id` and monitors `gossip_originated`/`gossip_first_seen` events across nodes to decide convergence.
