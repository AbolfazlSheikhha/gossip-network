# Gossip Protocol Project Documentation

This document explains the full implementation, experiment pipeline, and how to reproduce report-ready results.

---

## 1) Project Overview

This project implements a **UDP-based gossip network** with:

- **Push mode** (`GOSSIP` forwarding)
- **Hybrid push-pull mode** (`IHAVE` / `IWANT`)
- **Peer discovery and maintenance** (`HELLO`, `GET_PEERS`, `PEERS_LIST`, `PING`, `PONG`)
- **Optional Sybil-resistance** using **Proof-of-Work** on `HELLO`

It also includes:

- Automated multi-run simulation for required scenarios
- Log parsing and metric extraction
- Plot generation for convergence and message overhead
- Separate directories for each experiment part and their analyses

---

## 2) Code Structure

- `gossip_node.py`
  - Core UDP node protocol and runtime loops
- `simulation.py`
  - Experiment runner (part2 and part3), process orchestration, log generation
- `analyze_logs.py`
  - Parses logs, computes metrics, builds CSV summaries and plots
- `results_production/`
  - Final production logs and analysis outputs (5 seeds per condition)

---

## 3) Gossip Node Implementation (`gossip_node.py`)

### 3.1 Transport and Message Envelope

- Uses `asyncio.DatagramProtocol` over UDP.
- Messages are JSON with fields:
  - `version`, `msg_id`, `msg_type`, `sender_id`, `sender_addr`, `timestamp_ms`, `ttl`, `payload`

### 3.2 Supported Message Types

- Membership and bootstrap:
  - `HELLO`
  - `GET_PEERS`
  - `PEERS_LIST`
- Liveness:
  - `PING`
  - `PONG`
- Gossip dissemination:
  - `GOSSIP`
- Hybrid push-pull:
  - `IHAVE`
  - `IWANT`

### 3.3 Internal State

- `peers`: known peers (`node_id`, `addr`, timestamps, ping state)
- `seen_msgs`: dedup set to prevent reprocessing/rebroadcast loops
- `gossip_cache`: cached full gossip messages for `IWANT` replies
- `random`: seeded PRNG for deterministic fanout selection

### 3.4 Push Mode Behavior

On `GOSSIP` receive:

1. Discard if `msg_id` already seen.
2. Add to `seen_msgs`.
3. Log `GOSSIP_RECEIVED ...`.
4. Cache full message in `gossip_cache`.
5. If `ttl > 0`, forward to random peers up to `fanout` with `ttl-1`.

### 3.5 Hybrid Mode Behavior

- Periodic hybrid loop (`pull_interval`):
  - sends `IHAVE(ids)` to up to `fanout` peers.
- On `IHAVE` receive:
  - requests unknown IDs via `IWANT`.
- On `IWANT` receive:
  - returns full cached `GOSSIP` for requested IDs.

### 3.6 Peer Discovery and Maintenance

Implemented loops:

- `PING` loop: checks liveness, updates/removes dead peers
- **Discovery loop**: periodically sends `GET_PEERS` to maintain connectivity

Additional connectivity enhancement:

- On receiving `PEERS_LIST`, for newly discovered peers, node sends `HELLO` immediately to accelerate bidirectional graph formation.

### 3.7 PoW on HELLO (Optional)

Config: `--pow-k` leading hex zeros required in `sha256(node_id || nonce)`.

- Sender computes PoW once and embeds in `HELLO.payload.pow`.
- Receiver verifies difficulty and digest consistency before accepting peer.

---

## 4) Simulation Implementation (`simulation.py`)

`simulation.py` supports required experiment parts and creates structured outputs.

### 4.1 Why it was reworked

Earlier runs could fail with:

- `OSError: [Errno 48] Address already in use`

Fix:

- Each run allocates a **fresh free UDP port block** for all `N` nodes.
- Prevents cross-run and stale-process port collisions.

### 4.2 Run Pipeline

For each condition:

1. Spawn `N` node processes (`gossip_node.py`)
2. Wait warmup
3. Inject one UDP `GOSSIP` message
4. Wait runtime
5. Terminate all processes gracefully
6. Save per-node logs into run-specific folder

### 4.3 Implemented Experiment Parts

#### Part 2: N scaling with push vs hybrid

- `N = 10, 20, 50`
- same default `ttl` and `fanout`
- run for both `push` and `hybrid`
- multiple seeds (production: 5)

Logs path:

- `results_production/part2_n_scaling/logs/push/...`
- `results_production/part2_n_scaling/logs/hybrid/...`

#### Part 3: Push mode parameter sweeps (`N=50`)

- Constant fanout, varying TTL (`ttl_values`)
- Constant TTL, varying fanout (`fanout_values`)
- push mode only

Logs path:

- `results_production/part3_push_ttl_fanout/logs/ttl_sweep/...`
- `results_production/part3_push_ttl_fanout/logs/fanout_sweep/...`

### 4.4 Run Manifest

Each simulation batch writes:

- `results_production/run_manifest.csv`

with part, mode, N, fanout, ttl, seed, base port, message ID, and run directory.

---

## 5) Analysis and Plotting (`analyze_logs.py`)

### 5.1 Parsing

For every run directory:

- Detect injected message by `from=127.0.0.1:0`
- Collect first receive timestamp per node
- Parse send lines for overhead counting

### 5.2 Metrics

- **Convergence time**:
  - time until target fraction (`--converge-ratio`, default `0.95`) of nodes receive the injected message
- **Coverage fraction**:
  - fraction of nodes receiving injected message
- **Message overhead** (used for trend checks/plots):
  - number of forwards of the **target injected gossip** (`GOSSIP` with same `msg_id`)

### 5.3 Output Files

Part 2 analysis:

- `part2_per_run.csv`
- `part2_summary.csv`
- multiple PNG plots

Part 3 analysis:

- `part3_ttl_per_run.csv`, `part3_ttl_summary.csv`
- `part3_fanout_per_run.csv`, `part3_fanout_summary.csv`
- `part3_trend_checks.txt`
- multiple PNG plots

Skipped runs:

- `part2_skipped_runs.txt`
- `part3_skipped_runs.txt`

In production outputs these are empty (no skipped runs).

### 5.4 Plot Readability Improvements

Plot style was upgraded for report use:

- consistent white-grid style
- larger readable labels
- line+errorbar trend plots
- bar+error summary plots
- scatter-per-run + mean overlays

This avoids relying only on box plots and makes seed variability explicit.

---

## 6) Final Production Results Generated

Production run used:

- **5 seeds per condition**
- output root: `results_production`
- generated **22 plots**

Trend checks (`results_production/part3_push_ttl_fanout/analysis/part3_trend_checks.txt`):

- TTL sweep overhead monotonic non-decreasing: `True`
- Fanout sweep convergence monotonic non-increasing: `True`
- Fanout sweep overhead monotonic non-decreasing: `True`

---

## 7) How To Run

## 7.1 Environment Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 7.2 Quick Validation Run (faster)

```bash
./venv/bin/python simulation.py \
  --part all \
  --out-root results_quick \
  --runs-per-n 2 \
  --warmup-s 3 \
  --runtime-s 12

MPLCONFIGDIR="/Users/amirmohammad.gomar/CN_Project/.mplconfig" \
./venv/bin/python analyze_logs.py \
  --results-root results_quick \
  --part all \
  --converge-ratio 0.95
```

## 7.3 Production Run (report-ready)

```bash
./venv/bin/python simulation.py \
  --part all \
  --out-root results_production \
  --runs-per-n 5 \
  --warmup-s 5 \
  --runtime-s 30 \
  --peer-timeout 10 \
  --discovery-interval 2 \
  --pull-interval 2

MPLCONFIGDIR="/Users/amirmohammad.gomar/CN_Project/.mplconfig" \
./venv/bin/python analyze_logs.py \
  --results-root results_production \
  --part all \
  --converge-ratio 0.95
```

## 7.4 Run Part-by-Part

Part 2 only:

```bash
./venv/bin/python simulation.py --part part2 --out-root results_production --runs-per-n 5
MPLCONFIGDIR="/Users/amirmohammad.gomar/CN_Project/.mplconfig" \
./venv/bin/python analyze_logs.py --results-root results_production --part part2
```

Part 3 only:

```bash
./venv/bin/python simulation.py --part part3 --out-root results_production --runs-per-n 5
MPLCONFIGDIR="/Users/amirmohammad.gomar/CN_Project/.mplconfig" \
./venv/bin/python analyze_logs.py --results-root results_production --part part3
```

## 7.5 Standalone Functional Test (10 nodes, at least 9 receivers)

This is an independent test that does not affect previous tests/results.

Test file:

- `test_gossip_10_nodes_min9_receivers.py`

What it validates:

- Creates exactly 10 nodes on dynamically chosen free UDP ports
- Injects one `GOSSIP` message from a non-node sender (`127.0.0.1:0`)
- Asserts that at least 9 distinct nodes receive the message
- Writes proof artifacts in a dedicated path

Run command:

```bash
./venv/bin/python test_gossip_10_nodes_min9_receivers.py
```

Output artifacts:

- `logs/tests_10_nodes_min9/run_<timestamp>/node_<port>.log`
- `logs/tests_10_nodes_min9/run_<timestamp>/summary.json`

The `summary.json` includes:

- `msg_id`
- `receivers_count`
- `required_min_receivers` (9)
- `receivers_ports`
- `result` (`PASS` or `FAIL`)

Example proven run:

- `logs/tests_10_nodes_min9/run_1772114409341/summary.json`
- Result: `PASS`
- `receivers_count`: `10` (required `>=9`)

## 7.6 Live Video Visualization (Push + Hybrid)

Standalone visualization script:

- `live_gossip_visualization.py`

What it shows:

- nodes being added over time
- gossip send activity as animated links
- nodes changing state once they receive the target message
- separate animations for `push` and `hybrid`

Implementation notes (important):

- Send links are drawn from **actual sender -> actual receiver** events parsed from node logs.
- Node positions use a stable pseudo-random layout (not a ring), so the animation does not imply circular/sequential propagation.
- Multiple simultaneous sends are visible as bursts (`active sends` in subtitle), matching fanout behavior.

Run command:

```bash
MPLCONFIGDIR="/Users/amirmohammad.gomar/CN_Project/.mplconfig" \
./.venv/bin/python live_gossip_visualization.py \
  --mode both \
  --out-dir visualization_outputs \
  --n-nodes 10 \
  --runtime-s 6 \
  --startup-stagger-s 0.3 \
  --warmup-after-start-s 1.5 \
  --fps 10 \
  --playback-slowdown 4 \
  --min-video-seconds 20 \
  --send-window-ms 900 \
  --recv-pulse-ms 1200
```

Generated outputs:

- `visualization_outputs/push/run_<timestamp>/live_push.gif`
- `visualization_outputs/hybrid/run_<timestamp>/live_hybrid.gif`
- per-run metadata:
  - `visualization_outputs/<mode>/run_<timestamp>/viz_summary.json`

Example generated runs:

- `visualization_outputs/push/run_1772114760378/live_push.gif`
- `visualization_outputs/hybrid/run_1772114775699/live_hybrid.gif`

Corrected visual behavior example runs:

- `visualization_outputs/push/run_1772115032322/live_push.gif`
- `visualization_outputs/hybrid/run_1772115047849/live_hybrid.gif`

Slower human-readable example runs:

- `visualization_outputs/push/run_1772134854644/live_push.gif`
- `visualization_outputs/hybrid/run_1772134876390/live_hybrid.gif`

Useful tuning flags:

- `--playback-slowdown`: stretches simulation time in the video
- `--min-video-seconds`: enforces minimum final duration
- `--send-window-ms`: keeps send links visible longer
- `--recv-pulse-ms`: keeps receive pulse longer
- `--fps`: output frame rate (lower can be easier to follow)

---

## 8) Important CLI Parameters

Simulation (`simulation.py`):

- `--part {all,part2,part3}`
- `--runs-per-n`
- `--Ns` (for part2 N values)
- `--default-ttl`, `--default-fanout`
- `--ttl-values` (part3 ttl sweep)
- `--fanout-values` (part3 fanout sweep)
- `--n-fixed` (part3 N, default 50)
- `--warmup-s`, `--runtime-s`
- `--ping-interval`, `--peer-timeout`, `--discovery-interval`
- `--pull-interval` (hybrid behavior)
- `--pow-k` (PoW difficulty)

Analyzer (`analyze_logs.py`):

- `--results-root`
- `--part {all,part2,part3}`
- `--converge-ratio` (default 0.95)

Node (`gossip_node.py`):

- `--port`, `--bootstrap`
- `--fanout`, `--ttl`, `--peer-limit`
- `--ping-interval`, `--peer-timeout`
- `--pull-interval`, `--ihave-max-ids`
- `--discovery-interval`
- `--pow-k`
- `--stdin true|false`

---

## 9) Log Format Notes

Key lines used by analyzer:

- Receive:
  - `GOSSIP_RECEIVED ... msg_id=... from=... at_ms=... origin_ts=...`
- Send:
  - `SEND type=... msg_id=... dest=... at_ms=...`

Injected message is identified by:

- `from=127.0.0.1:0`

---

## 10) Troubleshooting

### Address already in use

- Cause: fixed/reused ports across overlapping runs
- Fix in current implementation: per-run free port block allocation

### Matplotlib crashes in headless environment

- Analyzer already uses `Agg` backend
- Also run with:
  - `MPLCONFIGDIR="/Users/amirmohammad.gomar/CN_Project/.mplconfig"`

### Low convergence in harder settings

If needed for stronger coverage:

- increase `--runtime-s`
- reduce `--pull-interval` (hybrid)
- increase `--fanout`
- increase `--discovery-interval` frequency (smaller interval value)

---

## 11) What Was Completed vs Requirements

- UDP gossip protocol implemented: `push` + `hybrid`
- Simulation for `N=10,20,50` with push/hybrid comparison
- Separate convergence and overhead visualizations for push vs hybrid
- Push-only sweeps on `N=50`:
  - constant fanout + varying TTL
  - constant TTL + varying fanout
- Trend expectations validated in production outputs
- Logs and analysis separated by experiment part directories
- At least 12 plots: exceeded (22 generated)

---

## 12) Recommended Figures for Report

Core comparison:

- `part2_convergence_vs_n_push_vs_hybrid.png`
- `part2_overhead_vs_n_push_vs_hybrid.png`
- `part2_convergence_runs_n_mode.png`
- `part2_overhead_runs_n_mode.png`

Part 3 trends:

- `part3_convergence_vs_ttl.png`
- `part3_overhead_vs_ttl.png`
- `part3_convergence_vs_fanout.png`
- `part3_overhead_vs_fanout.png`
- `part3_convergence_runs_ttl.png`
- `part3_overhead_runs_ttl.png`
- `part3_convergence_runs_fanout.png`
- `part3_overhead_runs_fanout.png`

---

If you want, I can also prepare a **`report_figures.md`** with ready-to-paste captions and interpretation text for each selected figure.
