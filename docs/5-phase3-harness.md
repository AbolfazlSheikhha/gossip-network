# Stage 6 - Phase 3 Experiment Harness (Simulation + Data Collection)

## Scope
This stage adds an automated experiment runner for Phase 3 requirements:
- automatic runs for `N in {10, 20, 50}`
- at least 5 seeds per setting
- deterministic run layout and metadata
- preserving per-node stdout/stderr + structured JSONL logs
- capturing the originated gossip `msg_id` for later analysis

Metrics computation and charts are intentionally deferred to Stage 7.

## Implemented Files
- `scripts/run_phase3_experiments.py`
- `src/config.py` (adds optional `--log-dir`)
- `src/node.py` (uses configured log dir in `JsonlLogger`)

## Supported Experiment Groups
- `baseline_by_n`: vary only `N`, keep `fanout/ttl/policy` fixed.
- `fanout_sweep`: vary only `fanout`, keep `N/ttl/policy` fixed.
- `ttl_sweep`: vary only `ttl`, keep `N/fanout/policy` fixed.
- `neighbor_policy_sweep`: vary only neighbor policy if runtime supports a policy flag.

If neighbor policy is not supported by the current node CLI, this group is skipped and recorded in `summary.json > groups_skipped`.

## CLI Examples

Baseline-by-N (Phase 3 mandatory set):
```bash
python3 scripts/run_phase3_experiments.py \
  --groups baseline_by_n \
  --ns 10 20 50 \
  --seeds 1 2 3 4 5 \
  --baseline-fanout 3 \
  --baseline-ttl 8 \
  --baseline-neighbor-policy default
```

Fanout sweep (3 fanout values, fixed N/ttl/policy):
```bash
python3 scripts/run_phase3_experiments.py \
  --groups fanout_sweep \
  --sweep-n 20 \
  --fanouts 2 3 4 \
  --sweep-ttl 8 \
  --sweep-neighbor-policy default \
  --seeds 1 2 3 4 5
```

TTL sweep (3 ttl values, fixed N/fanout/policy):
```bash
python3 scripts/run_phase3_experiments.py \
  --groups ttl_sweep \
  --sweep-n 20 \
  --sweep-fanout 3 \
  --ttls 4 8 12 \
  --sweep-neighbor-policy default \
  --seeds 1 2 3 4 5
```

Neighbor-policy sweep (only if node runtime has a policy flag):
```bash
python3 scripts/run_phase3_experiments.py \
  --groups neighbor_policy_sweep \
  --sweep-n 20 \
  --sweep-fanout 3 \
  --sweep-ttl 8 \
  --neighbor-policies policyA policyB policyC \
  --neighbor-policy-flag --neighbor-policy \
  --seeds 1 2 3 4 5
```

Current codebase note: `python3 -m src --help` does not expose a neighbor-policy flag, so policy sweep is currently unavailable and automatically skipped.

## Output Layout
Default output root:
- `logs/phase3/<UTC_TIMESTAMP>/`

Per-run directory:
- `logs/phase3/<UTC_TIMESTAMP>/<group>/n=<N>/fanout=<F>/ttl=<T>/policy=<P>/seed=<S>/`

Contents per run:
- `meta.json`
- `nodes/node-XX.stdout.log`
- `nodes/node-XX.stderr.log`
- `jsonl/node-<port>-<ts>-<nodeid>.jsonl`

Top-level run-set file:
- `summary.json` with run status, skipped groups, and per-run pointers.

## `meta.json` Fields
Each run stores:
- experiment group and parameters (`n`, `fanout`, `ttl`, policy, seed)
- `node_seeds` (deterministically derived from run seed)
- liveness/config knobs (`peer_limit`, `ping_interval`, `peer_timeout`, etc.)
- `origin_index`
- exact `injected_message_text`
- `run_start_time_utc`, `injection_time_utc`, `run_end_time_utc`
- `bootstrap_port`
- node launch commands
- per-node stdout/stderr/jsonl paths
- `target_msg_id` (if extracted)
- run status + exit codes (+ error details on failure)

## How `target_msg_id` Is Determined
After stabilization, the harness injects exactly one message into the origin node via stdin.
It then tails the origin node JSONL log and reads the first `gossip_originated` event after injection time, extracting:
- `msg_id` -> saved as `target_msg_id` in `meta.json`

## Required Log Events/Fields for Stage 7
Stage 7 analysis relies on these existing events:

- `gossip_originated`
  - `msg_id`
  - `origin_ts_ms`
  - `node_id`
  - `ts_ms`

- `gossip_first_seen`
  - `msg_id`
  - `recv_ts_ms` (receive time required by Phase 3 data collection)
  - `node_id`
  - `from_peer`
  - `ts_ms`

- `send_ok` (for message overhead counting)
  - `msg_type`
  - `msg_id`
  - `node_id`
  - `ts_ms`

These fields are sufficient to compute convergence time and message overhead in the next stage.
