# Stage 4 - Liveness (PING/PONG + Dead Peer Eviction)

## Scope
This stage adds periodic peer liveness management on top of Stage 3 discovery:
- periodic `PING` scheduling
- immediate `PONG` replies
- peer liveness bookkeeping (`last_seen_ts_ms`, `last_ping_ts_ms`, `consecutive_ping_failures`)
- dead peer eviction with structured logs

Not implemented in this stage:
- gossip dissemination
- Hybrid Push-Pull (`IHAVE`/`IWANT`)
- additional PoW enforcement changes

## Liveness Algorithm

### Peer state fields
Each peer record now tracks:
- `last_seen_ts_ms`: last valid activity timestamp (updated on `HELLO`, `GET_PEERS`, `PEERS_LIST`, `PING`, `PONG`)
- `last_ping_ts_ms`: timestamp of most recent outbound `PING`
- `consecutive_ping_failures`: number of consecutive probe timeouts
- `pending_ping_id`, `pending_ping_seq`: current outstanding probe correlation info
- `next_ping_seq`: monotonic local sequence per peer

New peer grace behavior:
- when a peer is added via discovery and no explicit `last_seen_ts_ms` exists yet, it is initialized to `now`.
- this prevents immediate false eviction for newly added peers.

### Periodic scheduler
A background async task runs every `ping_interval` seconds and does:
1. Timeout processing:
- for each peer with pending ping, if no `PONG` arrived before the next interval, mark one ping failure.
2. Dead-peer eviction:
- evict peer if either condition holds:
  - `now_ms - last_seen_ts_ms > peer_timeout_ms`
  - `consecutive_ping_failures >= K`
3. Probe send:
- send new `PING` to all remaining peers that do not currently have a pending ping.

This loop is non-blocking (`asyncio`) and runs concurrently with receive/dispatch logic.

### Timeout/failure definition
A ping failure is defined as:
- `PING` sent to peer
- no correlated `PONG` received by the next liveness cycle (`ping_interval` boundary)

## Constants and Policy Choices
- `PING_FAILURE_THRESHOLD (K) = 3`
  - chosen to avoid aggressive eviction on transient packet loss while still removing dead peers quickly in localhost experiments.
- `peer_timeout_ms = peer_timeout * 1000`
  - eviction can happen by staleness timeout even if failures do not reach `K` first.

## Correlation and Memory Safety
- outbound probes are tracked in runtime map:
  - `pending_pings[peer_addr][ping_id] = sent_ts_ms`
- on matched `PONG`:
  - pending entry is removed
  - failures reset to `0`
  - RTT is recorded
- stale pending entries are periodically cleaned up (bounded by timeout), so state cannot grow unbounded.

## Structured Logging Additions
Stage 4 adds/ensures these events:
- `ping_sent`
- `ping_received`
- `pong_sent`
- `pong_received`
- `peer_evict_dead`

Additional diagnostic event:
- `ping_timeout`

Common keys used across liveness events:
- `ts_ms`, `event`, `node_id`
- `peer`, `peer_addr`, `peer_id`
- `msg_id`
- `ping_id`, `seq`
- `reason`
- `last_seen_age_ms`
- `failures`
- `rtt_ms` (for `pong_received` on matches)

## Local Reproduction (3-5 nodes)

### 1) Start bootstrap node
```bash
python3 -m src \
  --port 9600 \
  --bootstrap 127.0.0.1:9600 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 1 --peer-timeout 4 \
  --seed 42
```

### 2) Start peer nodes
```bash
python3 -m src \
  --port 9601 \
  --bootstrap 127.0.0.1:9600 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 1 --peer-timeout 4 \
  --seed 43
```

```bash
python3 -m src \
  --port 9602 \
  --bootstrap 127.0.0.1:9600 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 1 --peer-timeout 4 \
  --seed 44
```

Optional: add nodes `9603` and `9604` similarly.

### 3) Observe liveness traffic in logs
```bash
LATEST_BOOT=$(ls -1t logs/node-9600-*.jsonl | head -n1)
rg '"event":"(ping_sent|ping_received|pong_sent|pong_received)"' "$LATEST_BOOT"
```
Expected:
- recurring `ping_sent`
- matching `ping_received`/`pong_sent` on peers
- matching `pong_received` with `status:"matched"`

### 4) Kill one node and verify eviction
Terminate node `9602` (Ctrl+C in its terminal), then after ~4-5 seconds:
```bash
LATEST_BOOT=$(ls -1t logs/node-9600-*.jsonl | head -n1)
rg '"event":"(ping_timeout|peer_evict_dead)"' "$LATEST_BOOT"
```
Expected:
- one or more `ping_timeout` events for the dead node
- then `peer_evict_dead` with reason (`peer_timeout` and/or `ping_failures`)

### 5) Malformed PING/PONG safety check
```bash
python3 scripts/send_test.py --port 9600 --kind invalid-schema
```
Expected:
- node keeps running
- invalid message logged (`recv_invalid_schema`, or `ping_invalid` / `pong_invalid` for malformed payloads)

## Notes
- `peer_limit` bound is preserved by existing `PeerStore` cap and replacement policy.
- liveness loop is canceled during shutdown, and UDP transport/logger are closed cleanly on Ctrl+C.
