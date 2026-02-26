# Stage 1 Protocol Specification (Normative)

## A) Overview

### A.1 Problem Summary
This protocol defines decentralized peer discovery and gossip dissemination for a peer-to-peer network over UDP using JSON messages. Nodes discover neighbors through a seed/bootstrap node and spread events using bounded fanout and TTL.

### A.2 Design Goals
- The transport layer MUST be UDP.
- Message encoding MUST be JSON.
- Dissemination MUST be bounded (TTL + dedup).
- Duplicate processing MUST be prevented using a Seen Set keyed by `msg_id`.
- Peer list growth MUST be bounded by `peer_limit`.
- Neighbor liveness MUST be tracked with `PING`/`PONG`.
- Behavior SHOULD be reproducible across runs when `--seed` is fixed.
- Protocol extensions for Hybrid Push-Pull (`IHAVE`/`IWANT`) and HELLO PoW MUST be defined now.

### A.3 Canonical Naming (Required Disambiguation)
This specification uses the following canonical message names for implementation:
- `GET_PEERS`
- `PEERS_LIST`

Mapping to terms used in `project-doc.md`:
- `PEERS_GET` == `GET_PEERS` (same semantic meaning)
- `LIST_PEERS` == `PEERS_LIST` (same semantic meaning)

Implementation MUST use only the canonical names above on the wire.

## B) Node State (Data Model)
Each node MUST keep the following in-memory state.

### B.1 Identity and Address
- `node_id: string` (UUID)
- `self_addr: string` (`ip:port`)

### B.2 Peer List
- `peer_list: map[addr]PeerRecord`, capped by `peer_limit`
- `PeerRecord` fields:
  - `addr: string` (required)
  - `node_id: string | null` (null until known)
  - `last_seen_ms: int64` (last valid message from peer)
  - `last_ping_sent_ms: int64 | null`
  - `consecutive_ping_failures: int`
  - `pending_ping_id: string | null`
  - `pending_ping_seq: int | null`
  - `rtt_ms: int | null`
  - `is_verified_hello: bool` (true after accepted HELLO)
  - `source: "bootstrap" | "peers_list" | "hello"`

### B.3 Seen Set
- `seen_set: set[string]` containing all processed `msg_id` values for `GOSSIP`.
- Project-scope eviction policy: unbounded for correctness simplicity.
- Implementations MAY add bounded eviction (LRU/time-window), but MUST preserve at-most-once processing within the active window.

### B.4 Known Message Store (for Hybrid Pull)
- `known_messages: map[msg_id]StoredGossip`
- `StoredGossip` fields:
  - `msg_id: string`
  - `topic: string`
  - `data: JSON value`
  - `origin_id: string`
  - `origin_timestamp_ms: int64`
  - `first_seen_ms: int64`

### B.5 Runtime Config
- `fanout: int`
- `ttl: int`
- `peer_limit: int`
- `ping_interval: int` (seconds)
- `peer_timeout: int` (seconds)
- `pull_interval: int` (seconds)
- `ids_max_ihave: int`
- `k_pow: int`
- `seed: int`

## C) Message Envelope (JSON over UDP)

### C.1 Canonical Envelope
All protocol messages MUST use this envelope:

```json
{
  "version": 1,
  "msg_id": "uuid-or-hash",
  "msg_type": "HELLO|GET_PEERS|PEERS_LIST|GOSSIP|PING|PONG|IHAVE|IWANT",
  "sender_id": "node-uuid",
  "sender_addr": "127.0.0.1:8000",
  "timestamp_ms": 1730,
  "ttl": 8,
  "payload": {}
}
```

### C.2 Field Requirements
- `version` MUST be integer `1`.
- `msg_id` MUST be non-empty string and globally unique per logical message.
- `msg_type` MUST be one of canonical types.
- `sender_id` MUST be a UUID string.
- `sender_addr` MUST be `ip:port` string.
- `timestamp_ms` MUST be sender wall-clock epoch milliseconds.
- `payload` MUST be a JSON object.
- `ttl` rules:
  - For `GOSSIP`, `ttl` MUST be present and MUST be integer `>= 0`.
  - For control messages (`HELLO`, `GET_PEERS`, `PEERS_LIST`, `PING`, `PONG`, `IHAVE`, `IWANT`), `ttl` SHOULD be omitted or `null` and MUST be ignored by receivers.

### C.3 Validation and Error Handling
- Invalid JSON, missing required fields, invalid field types, unsupported `version`, or unknown `msg_type` MUST NOT crash the node.
- Such messages MUST be logged as dropped/invalid and ignored.
- Control-plane parse/validation failures SHOULD be rate-limited in logs.

### C.4 UDP Size Guidance
To avoid fragmentation, a serialized UDP datagram SHOULD target <= 1200 bytes. Large `data` payloads SHOULD be truncated/rejected at sender side with an error log.

## D) Message Types (Normative)

## D.1 HELLO
### Purpose
Announce node presence and request admission into receiver peer set.

### Payload
```json
{
  "capabilities": ["udp", "json"],
  "pow": {
    "hash_alg": "sha256",
    "difficulty_k": 4,
    "nonce": 9138472,
    "digest_hex": "0000ab12..."
  }
}
```

### Required Payload Fields
- `capabilities: string[]` MUST include `"udp"` and `"json"`.
- `pow` object:
  - MUST be present if `k_pow > 0`.
  - MAY be omitted if `k_pow == 0`.

### PoW Validation Rules
If receiver `k_pow > 0`, receiver MUST verify all of:
1. `hash_alg == "sha256"`
2. `difficulty_k == k_pow` (exact match)
3. `digest_hex == SHA256_HEX(UTF8(str(nonce) + sender_id))`
4. `digest_hex` starts with at least `k_pow` leading hex `0` characters.

### Receive Behavior
- If HELLO is valid, receiver MUST upsert sender in `peer_list` (subject to `peer_limit` policy in Section E.2), set `is_verified_hello=true`, and update `last_seen_ms`.
- If PoW is required and invalid, receiver MUST reject admission (no peer add/update except optional metrics counter).
- Rejection behavior: receiver MUST silently drop at protocol level (no explicit error response), but MUST log rejection reason.

### Logging
- MUST log HELLO accept/reject, sender, and reason (`pow_invalid`, `pow_missing`, etc.).

## D.2 GET_PEERS
### Purpose
Request known neighbors for discovery/bootstrap.

### Payload
```json
{ "max_peers": 20 }
```

### Required Payload Fields
- `max_peers` is optional; if provided MUST be integer >= 1.

### Receive Behavior
- Receiver MUST respond with `PEERS_LIST`.
- Number of returned peers MUST be `min(request.max_peers or peer_limit, peer_limit, known_peer_count)`.
- Receiver SHOULD exclude requester address and SHOULD exclude duplicates.

### Logging
- MUST log request and number of peers returned.

## D.3 PEERS_LIST
### Purpose
Provide discovered peers in response to `GET_PEERS`.

### Payload
```json
{
  "peers": [
    {"node_id": "...", "addr": "127.0.0.1:8001"},
    {"node_id": "...", "addr": "127.0.0.1:8002"}
  ]
}
```

### Required Payload Fields
- `peers` MUST be an array.
- Each item MUST contain:
  - `node_id: string`
  - `addr: string` (`ip:port`)

### Receive Behavior
- Receiver MUST attempt to merge entries into `peer_list` using Section E.2 policy.
- Receiver MUST ignore entries equal to `self_addr`.
- Receiver SHOULD ignore malformed peer entries individually (not fail whole message).

### Logging
- MUST log count received, count admitted, count dropped, and reason for drops.

## D.4 PING
### Purpose
Liveness probe for neighbor management.

### Payload
```json
{
  "ping_id": "uuid-or-counter",
  "seq": 17
}
```

### Required Payload Fields
- `ping_id: string` (unique for outstanding probe)
- `seq: int` (monotonic per sender-peer relation)

### Receive Behavior
- Receiver MUST respond with `PONG` echoing `ping_id` and `seq`.
- Receiver SHOULD update sender `last_seen_ms`.

### Logging
- MUST log ping send and ping receive events.

## D.5 PONG
### Purpose
Acknowledge a `PING` and confirm liveness.

### Payload
```json
{
  "ping_id": "uuid-or-counter",
  "seq": 17
}
```

### Receive Behavior
- Receiver MUST match `ping_id` to pending ping state.
- On match, receiver MUST:
  - set peer `last_seen_ms = now`
  - reset `consecutive_ping_failures = 0`
  - compute/store `rtt_ms`
  - clear pending ping marker
- Unmatched `PONG` MUST be ignored with debug log.

### Logging
- MUST log pong match success/failure.

## D.6 GOSSIP
### Purpose
Disseminate application information.

### Payload
```json
{
  "topic": "news",
  "data": "Hello network!",
  "origin_id": "node-uuid",
  "origin_timestamp_ms": 1730
}
```

### Required Payload Fields
- `topic: string`
- `data: JSON value`
- `origin_id: string`
- `origin_timestamp_ms: int64`

### Dedup and Processing
1. Validate envelope + payload + `ttl`.
2. If `msg_id` in `seen_set`, receiver MUST ignore (no forward).
3. Else receiver MUST:
   - add `msg_id` to `seen_set`
   - store in `known_messages`
   - log receive event for metrics

### TTL Rule (Corrected Interpretation)
Project text contains a likely typo (`ttl < 0`). This specification defines the correct stopping rule:
- On receive of a new gossip: `ttl_next = ttl_in - 1`
- Forwarding is allowed ONLY if `ttl_next > 0`
- If `ttl_next <= 0`, node MUST NOT forward

This rule MUST be used to guarantee termination and prevent infinite dissemination.

### Fanout Forwarding
- Candidate neighbors = active peers excluding immediate sender address.
- Node MUST choose `k = min(fanout, candidate_count)` distinct neighbors (no repetition).
- Selection MUST be random without replacement.
- RNG MUST be initialized from `--seed` for reproducibility.
- Forwarded message MUST keep the same `msg_id` and payload, set `ttl=ttl_next`, and update sender fields (`sender_id`, `sender_addr`, `timestamp_ms`).
- If candidate_count < `fanout`, node forwards to all candidates.

### Logging
- MUST log every new gossip receive with `msg_id`, `node_id`, receive timestamp.
- MUST log every gossip send with destination, `msg_id`, bytes, timestamp.

## D.7 IHAVE (Hybrid Push-Pull)
### Purpose
Advertise known message IDs (not full payloads) periodically.

### Payload
```json
{
  "ids": ["id1", "id2", "id3"],
  "max_ids": 32
}
```

### Required Payload Fields
- `ids: string[]` length `1..ids_max_ihave`
- `max_ids` MAY be included; if present SHOULD equal sender config `ids_max_ihave`.

### Send/Receive Rules
- Every `pull_interval` seconds, node MUST send `IHAVE` to `min(fanout, active_peer_count)` random peers.
- `ids` MUST be capped by `ids_max_ihave`.
- Receiver MUST compare `ids` with `seen_set` and compute missing IDs.
- If missing IDs are non-empty, receiver MUST send `IWANT`.

### Logging
- MUST log IHAVE send/receive counts and number of IDs advertised.

## D.8 IWANT (Hybrid Push-Pull)
### Purpose
Request full gossip messages for missing IDs.

### Payload
```json
{
  "ids": ["id2", "id3"]
}
```

### Required Payload Fields
- `ids: string[]`, non-empty.

### Receive Rules
- Sender receiving `IWANT` MUST look up each ID in `known_messages`.
- For each found ID, sender MUST send full `GOSSIP` message with same `msg_id` and original payload.
- For pull-response delivery, sender MUST set `ttl=1` to deliver to requester without re-flooding.
- Missing IDs MAY be ignored silently with debug log.

### Logging
- MUST log IWANT requested IDs count and fulfilled IDs count.

## E) Algorithms / State Machines

## E.1 Startup and Bootstrap
Pseudocode:

```text
on_start:
  load config, init RNG(seed), bind UDP socket
  init node state (peer_list, seen_set, known_messages)

  if bootstrap addr is provided and bootstrap != self_addr:
    send HELLO to bootstrap
    send GET_PEERS{max_peers=peer_limit} to bootstrap

  event loop starts:
    - receive UDP and handle messages
    - periodic ping task every ping_interval
    - periodic pull task every pull_interval
```

Bootstrap receive path:
1. `HELLO` accepted -> bootstrap upserts newcomer as peer.
2. `GET_PEERS` received -> bootstrap returns `PEERS_LIST`.
3. New node merges peers up to cap.
4. New node MAY proactively send `HELLO` to newly discovered peers.

Seed node is an entry point only; protocol MUST NOT require seed after bootstrap success.

## E.2 Peer List Management and Dead Node Detection
Peer capacity and replacement:
- `peer_list` size MUST NOT exceed `peer_limit`.
- Merge/upsert rules:
  - If peer already exists by `addr`: update metadata.
  - Else if space exists: add peer.
  - Else apply replacement policy below.

Replacement policy (deterministic):
1. Compute each existing peer score tuple:
   - `S1 = consecutive_ping_failures` (higher is worse)
   - `S2 = now_ms - last_seen_ms` (higher is worse)
2. Candidate for eviction = peer with lexicographically maximum `(S1, S2, addr)`.
3. Evict only if either:
   - `S1 >= 3`, or
   - `S2 > peer_timeout * 1000`
4. If no eviction condition is met, reject new candidate.

Liveness task:
- Every `ping_interval`, node SHOULD ping all peers (or at minimum a rotating subset).
- If pending ping exceeds `peer_timeout`, increment `consecutive_ping_failures`.
- After `consecutive_ping_failures >= 3`, peer MUST be removed.

## E.3 Gossip Receive/Forward Pipeline
Pseudocode:

```text
on_receive_gossip(m, from_addr):
  validate m
  if m.msg_id in seen_set:
    log duplicate_drop
    return

  seen_set.add(m.msg_id)
  known_messages[m.msg_id] = payload
  log gossip_receive

  ttl_next = m.ttl - 1
  if ttl_next <= 0:
    log ttl_stop
    return

  candidates = active_peers excluding from_addr
  targets = random_sample(candidates, min(fanout, len(candidates)))
  for p in targets:
    send gossip(msg_id=m.msg_id, payload=m.payload, ttl=ttl_next, dst=p)
```

## F) Configuration Parameters (CLI Contract)
Node executable MUST support:

- `--port <int>`: UDP listen port.
- `--bootstrap <ip:port>`: seed node address.
- `--fanout <int>`: number of peers per gossip forward.
- `--ttl <int>`: initial gossip TTL.
- `--peer-limit <int>`: max peer list size.
- `--ping-interval <seconds>`: ping scheduling interval.
- `--peer-timeout <seconds>`: ping timeout/liveness threshold base.
- `--seed <int>`: random seed for reproducibility.

Later-mandatory, defined now:
- `--pull-interval <seconds>`: interval for periodic `IHAVE`.
- `--ids-max-ihave <int>`: max IDs carried by one `IHAVE`.
- `--k-pow <int>`: PoW difficulty (leading hex zeros required in HELLO hash).

Terminology mapping to project text:
- `limit_peer`/`peer_limit` -> `--peer-limit`
- `interval_ping`/`ping_interval` -> `--ping-interval`
- `timeout_peer`/`peer_timeout` -> `--peer-timeout`

## G) Logging & Metrics Hooks
Nodes MUST emit machine-parseable JSONL logs. Recommended schema:

```json
{
  "ts_ms": 1730123456789,
  "node_id": "...",
  "event": "recv|send|peer_add|peer_remove|ping_timeout|drop_invalid|drop_duplicate",
  "msg_type": "GOSSIP",
  "msg_id": "...",
  "peer_addr": "127.0.0.1:8001",
  "bytes": 412,
  "status": "ok|dropped|rejected",
  "reason": "pow_invalid|ttl_stop|seen_before|parse_error"
}
```

Mandatory logging hooks:
- For each received new `GOSSIP`: log `msg_id`, receive timestamp, local `node_id`.
- For each send event (all message types): log `msg_type`, timestamp, bytes, destination, and `msg_id` when available.
- Log peer add/remove events with reasons.
- Log ping send/timeout/pong success.

These logs MUST be sufficient to compute Phase 3 convergence and message overhead.

## H) Open Decisions & Justifications
1. TTL correction:
- Source document line has likely typo (`forward if ttl < 0`).
- Chosen rule: decrement then forward only if remaining TTL is strictly positive.
- Justification: guarantees bounded propagation and matches standard gossip TTL semantics.

2. Canonical message names:
- Chosen wire names: `GET_PEERS`, `PEERS_LIST`.
- Justification: aligns with the JSON envelope example and avoids dual naming in implementation.

3. Peer replacement policy:
- Chosen deterministic eviction based on highest failures then staleness.
- Justification: favors stable responsive peers and makes behavior reproducible/debuggable.

4. HELLO rejection behavior:
- Chosen silent protocol drop + local log on PoW failure.
- Justification: simpler UDP behavior and avoids creating reflection/amplification responses.

5. IWANT response TTL:
- Chosen `ttl=1` for pull-response GOSSIP.
- Justification: delivers missing data while minimizing re-flood overhead in Hybrid mode.

## Stage 1 Self-Review Checklist
- [x] UDP transport and JSON format are mandated.
- [x] Required configurable params defined: `fanout`, `ttl`, `peer_limit`, `ping_interval`, `peer_timeout`.
- [x] Later mandatory params defined: `pull_interval`, `ids_max_ihave`, `k_pow`.
- [x] Required message types fully specified: `HELLO`, `GET_PEERS`, `PEERS_LIST`, `GOSSIP`, `PING`, `PONG`.
- [x] Hybrid `IHAVE`/`IWANT` specified.
- [x] Set-Seen dedup and TTL stopping rule specified.
- [x] Bootstrap process via seed node specified.
- [x] Neighbor management and dead-node detection specified.
- [x] Naming inconsistencies mapped and resolved.
- [x] Logging schema defined for later metrics.
- [x] No node implementation code added in this stage.
