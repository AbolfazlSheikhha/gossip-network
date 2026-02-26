# Stage 6 - Experiment Harness (Automated Multi-Node Runs + Log Collection)

## Scope
This stage adds a reproducible Python harness that automates local multi-node gossip experiments and writes all artifacts needed for Stage 7 analysis.

Implemented files:
- `scripts/run_experiments.py`
- minimal node CLI/logging extension for per-node log routing:
  - `src/config.py` (`--log-file`)
  - `src/logging_jsonl.py`
  - `src/node.py`

## How the Harness Works
`scripts/run_experiments.py` runs one or more `(N, seed)` experiments sequentially.

For each run:
1. Pre-check required UDP ports `base_port .. base_port+N-1`.
2. Launch node `0` first on `base_port` as bootstrap.
3. Launch nodes `1..N-1` with `--bootstrap 127.0.0.1:<base_port>`.
4. Assign deterministic node seeds:
   - `node_seed = seed * 1000 + node_index`
5. Wait `--stabilize-seconds`.
6. Inject exactly one gossip line to `origin-index` via stdin pipe.
7. Tail JSONL logs and track:
   - `target_msg_id` from origin `gossip_originated`
   - unique nodes that logged `gossip_originated` or `gossip_first_seen` for that `msg_id`
8. Stop when:
   - convergence reaches `ceil(0.95 * N)`, or
   - `--run-timeout-seconds` is reached.
9. Graceful shutdown for all nodes (`SIGINT`, then kill if needed).

## Node Entrypoint Used
The harness uses the existing node entrypoint from previous stages:
- `python -m src`

No new runtime entrypoint was introduced.

## CLI Usage
Single run:

```bash
python3 scripts/run_experiments.py \
  --n 10 \
  --seeds 1 \
  --base-port 5000 \
  --fanout 3 --ttl 8 --peer-limit 30 \
  --ping-interval 1 --peer-timeout 6 \
  --stabilize-seconds 3 \
  --run-timeout-seconds 30 \
  --origin-index 0 \
  --outdir logs/experiments
```

Multi-run matrix (N=10/20/50, 5 seeds):

```bash
python3 scripts/run_experiments.py \
  --ns 10 20 50 \
  --seeds 1 2 3 4 5 \
  --base-port 5000 \
  --fanout 3 --ttl 8 --peer-limit 30 \
  --ping-interval 1 --peer-timeout 6 \
  --stabilize-seconds 3 \
  --run-timeout-seconds 45 \
  --origin-index 0 \
  --outdir logs/experiments
```

## Output Directory Structure
Example:

```text
logs/experiments/
  20260226T111450Z/
    summary.json
    n=10/
      seed=1/
        meta.json
        node_0_stdout.log
        node_0_stderr.log
        node_0.jsonl
        ...
        node_9_stdout.log
        node_9_stderr.log
        node_9.jsonl
```

Run directory naming is deterministic in format (`<UTC timestamp>`; collision-safe suffix if needed).

## `meta.json` Contents
Per run, `meta.json` includes:
- run identity and timing:
  - `run_id`, `start_ts_ms`, `end_ts_ms`, ISO UTC timestamps, `duration_ms`
- experiment parameters:
  - `n`, `seed`, `base_port`, `origin_index`
  - `fanout`, `ttl`, `peer_limit`, `ping_interval`, `peer_timeout`, plus optional node params
- gossip injection data:
  - `gossip_message`
  - `injected_ts_ms`
  - `target_msg_id` (from origin `gossip_originated`)
- convergence data:
  - `target_coverage_nodes`
  - `converged_nodes`
  - `convergence_ts_ms`
  - final `status` (`converged`, `timeout`, `failed`, etc.)
- per-node launch artifacts:
  - command, pid, exit code
  - stdout/stderr path
  - JSONL log path

## Gossip Injection Mechanism
Each node is launched with `stdin=PIPE`.

After stabilization, the harness writes one line (plus newline) to the chosen origin node’s stdin:
- this triggers the existing Stage 5 stdin-origin path
- origin node logs `gossip_originated`
- the harness captures its `msg_id` and tracks spread across nodes via `gossip_first_seen`/`gossip_originated`.

## Robustness/Safety Behavior
- Fails fast if required ports are in use.
- Handles interruption signals and still terminates all child nodes.
- Writes/updates `meta.json` during lifecycle so partial runs remain inspectable.
- Uses only Python standard library.
