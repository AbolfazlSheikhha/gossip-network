# Stage 5 - Push Gossip Dissemination

## Scope
This stage implements baseline push-based rumor spreading on top of Stage 4:
- `GOSSIP` handling
- dedup via `seen_set`
- TTL-based forwarding stop
- random fanout forwarding
- user stdin trigger for gossip origin

Not implemented in this stage:
- Stage 6 experiment harness
- Stage 7 metrics scripts
- Stage 8 `IHAVE`/`IWANT` behavior
- Stage 9 additional PoW work

## Algorithm Summary

### State additions
`NodeState` now maintains:
- `seen_set: set[str]` for O(1) duplicate checks
- `message_store: dict[msg_id, envelope]` (full stored gossip envelope)
- `message_first_seen_ts_ms: dict[msg_id, int]`

Policy choice:
- `seen_set` and `message_store` are currently unbounded for project-scope correctness and simpler at-most-once behavior.

### Local origin (stdin)
When user types a non-empty line:
1. Create `GOSSIP` with:
- new `msg_id`
- payload:
  - `topic`
  - `data` (user text)
  - `origin_id = self.node_id`
  - `origin_timestamp_ms = now_ms`
- `ttl = config.ttl`
2. Add to `seen_set` and `message_store` immediately.
3. Select up to `fanout` peers and send.

### Receive path (`GOSSIP`)
On valid incoming gossip:
1. Validate payload fields (`topic`, `data`, `origin_id`, `origin_timestamp_ms`) and `ttl`.
2. If `msg_id` already in `seen_set`:
- do not forward
- log duplicate
3. Else:
- mark seen + store
- log first-seen
- compute `ttl_out = ttl_in - 1`
- forward only if `ttl_out > 0`

### TTL rule (canonical)
Implemented according to Stage 1 spec:
- For a NEW received gossip: `ttl_out = ttl_in - 1`
- Forward only when `ttl_out > 0`
- If `ttl_out <= 0`, stop forwarding (`ttl_exhausted`)

This is the termination-safe interpretation and avoids infinite propagation.

### Fanout and target selection
- Eligible peers = current peer list excluding:
  - self address
  - immediate sender address (for received messages)
- Choose `k = min(fanout, eligible_count)` distinct targets.
- If eligible peers are fewer than fanout, forward to all.

RNG decision:
- Uses dedicated seeded RNG `random.Random(seed)` in runtime (`self._rng`) for reproducible peer sampling.

## Logging Added (Stage 5)

### New gossip events
- `gossip_originated`
  - keys: `msg_id`, `origin_ts_ms`, `ttl_initial`, `text_len`
- `gossip_first_seen`
  - keys: `msg_id`, `recv_ts_ms`, `from_peer`, `ttl_in`
- `gossip_duplicate_ignored`
  - keys: `msg_id`, `from_peer`, `ttl_in`
- `gossip_forward_decision`
  - keys: `msg_id`, `ttl_in`, `ttl_out`, `fanout`, `candidate_count`, `num_targets`, `reason`
- `gossip_forwarded` (per target)
  - keys: `msg_id`, `ttl`, `peer_addr`

### Existing send logs retained
Every UDP send still includes `msg_type`, `msg_id`, `bytes`, `peer` via existing `send_ok` logs.

## Reproducible Local Test Commands (10 nodes)
Recommended parameters:
- `fanout=3`
- `ttl=8`
- `peer_limit=30`
- `ping_interval=1`
- `peer_timeout=6`

### 1) Start bootstrap node
```bash
python3 -m src \
  --port 9720 \
  --bootstrap 127.0.0.1:9720 \
  --fanout 3 --ttl 8 --peer-limit 30 \
  --ping-interval 1 --peer-timeout 6 \
  --seed 220
```

### 2) Start 9 peers
Example for one peer:
```bash
python3 -m src \
  --port 9721 \
  --bootstrap 127.0.0.1:9720 \
  --fanout 3 --ttl 8 --peer-limit 30 \
  --ping-interval 1 --peer-timeout 6 \
  --seed 221
```

Launch remaining peers similarly on ports `9722..9729` with different `--seed`.

### 3) Trigger gossip from one node
In node `9729` terminal, type and press Enter:
```text
hello push gossip
```

### 4) Validate spread
Check each node log for first-seen/origin events:
```bash
for f in logs/node-972*.jsonl; do
  echo "=== $f"
  rg '"event":"(gossip_originated|gossip_first_seen)"' "$f"
done
```

Expected:
- one node has `gossip_originated`
- at least 9/10 nodes have `gossip_first_seen` under recommended params
- discovery/liveness logs still present (`peers_list_*`, `ping_*`, `pong_*`)

### 5) Duplicate suppression check
```bash
for f in logs/node-972*.jsonl; do
  rg '"event":"gossip_duplicate_ignored"' "$f"
done
```
Expected:
- duplicates are logged
- duplicates are not re-forwarded

### 6) TTL stop check
Run a small network with low TTL:
```bash
python3 -m src --port 9730 --bootstrap 127.0.0.1:9730 --fanout 3 --ttl 1 --peer-limit 20 --ping-interval 1 --peer-timeout 6 --seed 330
```
(plus peers `9731`, `9732`, `9733` bootstrapped to `9730`)

After originating a gossip from `9733`, inspect:
```bash
for f in logs/node-973*.jsonl; do
  rg '"event":"gossip_forward_decision"' "$f"
done
```
Expected:
- receivers log `ttl_out: 0` and `reason: "ttl_exhausted"`
- no further forwarding beyond first hop

## Design Decisions
- Canonical payload key kept as `origin_timestamp_ms` (per Stage 1 spec).
- Immediate sender is excluded from forwarding targets to reduce ping-pong backflow.
- Dedicated RNG (`random.Random(seed)`) is used for deterministic fanout sampling.
- Seen/store structures are unbounded in this stage; bounded eviction can be added later if needed.
