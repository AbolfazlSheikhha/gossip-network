# Stage 3 - Bootstrap and Peer Discovery

## Scope
This stage implements bootstrap join and bounded peer discovery using canonical wire message types:
- `HELLO`
- `GET_PEERS`
- `PEERS_LIST`

`PING`/`PONG` are only handled as one-off request/response (no periodic liveness loops yet).
Gossip dissemination is unchanged and still deferred.

## Bootstrap Sequence (Exact Runtime Flow)
When a node starts:
1. Node binds UDP socket and starts receive loop.
2. If `--bootstrap` is different from `self_addr`:
   - Send `HELLO` to bootstrap.
   - Send `GET_PEERS` with payload `{ "max_peers": peer_limit }` to bootstrap.
3. Node keeps running even if bootstrap is unreachable (send errors are logged; process does not crash).

When bootstrap receives:
1. `HELLO`:
   - Validate payload (`capabilities`, and `pow` if `k_pow > 0`).
   - Upsert sender into peer store.
2. `GET_PEERS`:
   - Validate payload.
   - Respond with `PEERS_LIST` (bounded list, no duplicates, no self, exclude requester).

When joiner receives `PEERS_LIST`:
1. Validate each peer entry independently.
2. Ignore invalid entries, duplicates, and self.
3. Merge valid peers into bounded peer store.
4. Log merge counters: added, updated, ignored, evicted.

## Peer List Policy (Cap + Deterministic Replacement)
Peer record fields:
- `addr` (`ip:port`)
- `node_id` (nullable if unknown)
- `last_seen_ts_ms` (nullable)
- `consecutive_ping_failures`
- `last_ping_sent_ms`
- `pending_ping_id`
- `pending_ping_seq`
- `rtt_ms`
- `is_verified_hello`
- `source`

Capacity:
- Store is strictly capped at `peer_limit`.

Upsert rules:
1. Existing `addr` -> update metadata (`peer_update`).
2. New `addr` with free space -> add (`peer_add`).
3. New `addr` when full -> deterministic candidate selection:
   - Score each existing peer with tuple `(consecutive_ping_failures, now_ms - last_seen_or_zero, addr)`.
   - Candidate is lexicographically maximum score.
   - Evict only if `consecutive_ping_failures >= 3` OR staleness `> peer_timeout * 1000`.
   - Otherwise reject newcomer (`peer_limit_reject` path).

This matches Stage 1 Section E.2 semantics and guarantees `len(peer_store) <= peer_limit`.

## Message Format and Naming Mapping
Canonical on-wire names are used exactly as required by Stage 1:
- `GET_PEERS`
- `PEERS_LIST`

Mapping to `project-doc.md` terms:
- `PEERS_GET` -> `GET_PEERS`
- `LIST_PEERS` -> `PEERS_LIST`

Implemented payload handling:
- `HELLO.payload`:
  - `capabilities` must include `udp` and `json`.
  - If `k_pow > 0`, `pow` must satisfy:
    - `hash_alg == "sha256"`
    - `difficulty_k == k_pow`
    - `digest_hex == SHA256_HEX(str(nonce) + sender_id)`
    - leading `k_pow` zeros.
- `GET_PEERS.payload`:
  - optional `max_peers` (int >= 1).
- `PEERS_LIST.payload`:
  - `peers` array with entries containing `addr` and optional `node_id` if known.

## Structured Logging Added
New/used events:
- `peer_add`
- `peer_update`
- `peer_evict`
- `bootstrap_hello_sent`
- `bootstrap_get_peers_sent`
- `peers_list_sent`
- `peers_list_received`
- `hello_accepted`
- `hello_rejected`

Existing Stage 2 message send/recv schema remains intact (`recv_ok`, `send_ok`, invalid schema/json drops, etc.).

## Local Multi-Node Reproduction
Bootstrap node:
```bash
python3 -m src \
  --port 9500 \
  --bootstrap 127.0.0.1:9500 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 2 --peer-timeout 6 \
  --seed 42
```

Joiners:
```bash
python3 -m src \
  --port 9501 \
  --bootstrap 127.0.0.1:9500 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 2 --peer-timeout 6 \
  --seed 43
```

```bash
python3 -m src \
  --port 9502 \
  --bootstrap 127.0.0.1:9500 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 2 --peer-timeout 6 \
  --seed 44
```

Verify from `logs/`:
- joiners logged `bootstrap_hello_sent` and `bootstrap_get_peers_sent`
- bootstrap logged `peers_list_sent`
- joiners logged `peers_list_received`
- peer events (`peer_add`/`peer_update`) appear and store remains bounded.

## Deviations / Assumptions
- One-hop expansion to newly discovered peers is not enabled in this stage (optional in Stage 1).
- Periodic liveness probing/eviction loops are intentionally not implemented yet (Stage 4 scope).
- Stage 2 CLI contract is preserved; malformed `--bootstrap` fails fast via parser error.
- Fixed an existing CLI bug: `--pull-interval` is now correctly read from `args.pull_interval`.
