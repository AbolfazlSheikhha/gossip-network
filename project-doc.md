## Title:

**Computer Networks – Project**


---

## Computer Networks Course – Project (Page 2 of 9)

# Project

## 1) Project Title

Design, Implementation, and Analysis of the Gossip Protocol for Information Dissemination in a Peer-to-Peer Network

Note that this project can be done in teams of one or two people.

## 2) Introduction

In modern distributed systems (such as blockchains and distributed databases), there is a fundamental need: how do nodes discover each other and disseminate information in the network in a scalable and reliable way? Gossip protocols are a common and powerful solution to this problem.

Gossip protocols (rumor spreading) are a family of information dissemination methods in large and decentralized networks whose main idea is inspired by how news spreads in society. In this method, each node, instead of sending a message to all nodes, sends it only to a limited number of its neighbors (usually randomly). Each neighbor, after receiving the message, if it has not seen it before, repeats the same action and sends the message to a few other neighbors. In this way, the message spreads through the network in a few rounds with low cost and in a scalable manner, and with high probability reaches most nodes.

To prevent endless dissemination and repeated processing, usually two mechanisms are used: (1) a unique identifier for each message and maintaining a set of seen messages (Set-Seen), and (2) limiting the number of rounds using TTL (decrease at each forwarding). In some more advanced versions, dissemination is combined as Push (active sending) and/or Pull (requesting new messages from neighbors) to reduce overhead.

## 3) Summary of Key Requirements

* In this project, use UDP.
* Configurable parameters: `fanout`, `ttl`, `limit_peer`, `interval_ping`, and `timeout_peer` must be changeable via command line or configuration file.
* Avoid duplicate processing: each message with `id_msg` must be processed only once (with Set-Seen).
* Statistical analysis: for each network size, experiments must be run at least ۵ times with different `seed`, and mean + standard deviation must be reported.

## 4) Overall Project Structure

The project consists of ۵ phases. Success in each phase is a prerequisite for the next phase.

### 4.1) Phase 1: Protocol Design

The goal of this phase is the precise design of the protocol before starting implementation; such that the network behavior, message structures, and decision rules of each node are completely specified and implementable. In this phase, you must specify on paper details such as the information each node stores, message format and fields, how a new node joins the network (Bootstrap), neighbor management and inactive-node detection, and the Gossip dissemination logic (such as checking duplicate messages, neighbor selection, and the values of Fanout/TTL), so that in later phases, implementation and analysis are done based on a clear and coherent design.

---

## Computer Networks Course – Project (Page 3 of 9)

### 4.1.1) Node Architecture (Architecture Node)

Each node must at least maintain the following state:

* `NodeID`: a unique identifier (e.g., UUID) independent of IP:Port
* `SelfAddr`: address `ip:port`
* `Peer List`: list of neighbors along with information such as last seen/response time
* `Seen Set`: set of seen `id_msg` values to prevent duplicate processing
* `Config`: configurable parameters `fanout`, `ttl`, `peer_limit`, `ping_interval`, `peer_timeout`

### 4.1.2) Message Formats (Formats Message)

Your protocol must at least support the following messages:

* `HELLO`: introducing a new node to another node and starting the connection process (initial registration in Peer List and initial coordination).
* `PEERS_GET`: requesting the list of known neighbors of a node to expand the Peer List and improve network discovery.
* `LIST_PEERS`: response to `PEERS_GET` containing a set of known nodes (address/id) to help with Bootstrap and updating neighbors.
* `GOSSIP`: disseminating a new piece of information/event in the network. Receiving nodes, if it is new, store it and forward it to a number of neighbors.

Also, for neighbor management, `PONG/PING` messages are recommended. To detect whether neighbors are alive and keep the Peer List updated, nodes periodically send `PING`, and if alive, the other side responds with `PONG`. If a node does not respond to several `PING` messages within a specified time interval (`Timeout`), it can be considered inactive and removed from the Peer List.

You may extend your protocol with more messages if needed.

Overall message format (JSON over UDP):

```json
{
  "version": 1,
  "msg_id": "uuid-or-hash",
  "msg_type": "HELLO | GET_PEERS | PEERS_LIST | GOSSIP | PING | PONG",
  "sender_id": "node-uuid",
  "sender_addr": "127.0.0.1:8000",
  "timestamp_ms": 1730,
  "ttl": 8,
  "payload": { ... }
}
```

Sample payloads. Note that you can change the items below or the header according to your needs. But you must explain your changes in the report.

---

## Computer Networks Course – Project (Page 4 of 9)

* `HELLO`:

```json
"payload": {
  "capabilities": ["udp", "json"]
}
```

* `GET_PEERS`:

```json
"payload": { "max_peers": 20 }
```

* `PEERS_LIST`:

```json
"payload": {
  "peers": [
    {"node_id":"...", "addr":"127.0.0.1:8001"},
    {"node_id":"...", "addr":"127.0.0.1:8002"}
  ]
}
```

* `GOSSIP`:

```json
"payload": {
  "topic": "news",
  "data": "Hello network!",
  "origin_id": "node-uuid",
  "origin_timestamp_ms": 1730
}
```

* `PING`:

```json
"payload": {
  "ping_id": "uuid-or-counter",
  "seq": 17
}
```

* `PONG`:

```json
"payload": {
  "ping_id": "uuid-or-counter",
  "seq": 17
}
```

### 4.1.3) Protocol Logic (Logic Protocol)

**(A) Bootstrap (joining a new node)**
At the beginning, it is necessary to define a seed node: the seed node is one or more always-available nodes in the network that a new node, when starting, knows its address in advance in order to begin the process of entering the network. The newcomer node, by sending messages such as `HELLO` and `PEERS_GET` to the seed node, receives its first list of neighbors and then can discover other nodes and continue working independently of the seed node. The seed node does not have a permanent central role and is only considered the entry point (Entry Point) for bootstrapping the network.

Suggested process:

1. Send `HELLO` to the seed node.
2. Send `GET_PEERS` and receive `PEERS_LIST`.
3. Fill the Peer List up to the `peer_limit` cap.
4. Start periodic cycles for `PING/PONG` and refreshing neighbors.

---

## Computer Networks Course – Project (Page 5 of 9)

**(B) Neighbor management and dead-node detection**

* Every `interval_ping` seconds, send `PING` to a number of neighbors.
* If no response arrives by `timeout_peer`, the neighbor becomes suspicious and after several times of no response it should be removed.
* The size of the Peer List must be limited and you must have a specific replacement policy (e.g., remove the oldest or the least active).

**(C) Gossip dissemination logic**
When a node receives `GOSSIP`:

1. If `id_msg` is in the Seen Set, ignore it.
2. Otherwise, store it in the Seen Set + log the receive time.
3. Decrease TTL, and if `ttl < ۰`, send the message to `fanout` random neighbors without repetition. (`fanout` is the number of neighbors each node sends the message to.)

---

## 4.2) Phase 2: Implementation

### 4.2.1) Implementation Requirements

1. Build an executable `node` program that runs from the CLI and receives parameters:

```bash
./node --port 8000 --bootstrap 127.0.0.1:9000 \
  --fanout 3 --ttl 8 --peer-limit 20 \
  --ping-interval 2 --peer-timeout 6 \
  --seed 42
```

Explanation of parameters `port`, `bootstrap`, and `seed`:

* `--port`: the port on which the node listens for incoming messages on the same machine. In localhost simulation, each node must have a different port to avoid interference (e.g., ۸۰۰۰, ۸۰۰۱, ۸۰۰۲, ...).
* `--bootstrap <ip:port>`: the seed node address in the form IP:Port. The new node at startup is introduced to the network through this address and receives its first neighbor list with messages like `HELLO` and `PEERS_GET`. In local simulation, usually `127.0.0.1:<port>` is used.
* `--seed`: the initial value for generating pseudo-random numbers (for example, in randomly selecting neighbors when forwarding messages or selecting a neighbor for `PING`). Having a specified seed makes experiments repeatable and allows fair comparison of results. In the analysis phase, for each N, multiple experiments with different seeds must be run.

2. Each node must simultaneously do these tasks (Event loop / Async / Thread are all acceptable):

* Listen to incoming messages
* Send periodic messages (`PING` and manage the Peer List)
* Receive user input to generate a new `GOSSIP`

Note that all operations of a node must be automatic. The user must be able to type a message in the terminal of each node, and that node must start the Gossip process.

3. Important logs must be printed in output (connection, receiving a new message, forwarding to neighbors, removing a dead neighbor, ...).

### 4.2.2) Correctness Requirements

* Each `id_msg` must be processed at most once.
* Corrupted message / invalid JSON must not crash the program.
* TTL must prevent infinite dissemination.
* Peer List must not grow without control (must have a cap and a management policy).

At the end, run a network with ۱۰ nodes on localhost and show that by generating one message, at least ۹ nodes have received it.

---

## Computer Networks Course – Project (Page ۶ of ۹)

## ۴.۳) Phase 3: Simulation and Performance Analysis

### ۴.۳.۱) Simulation Scenario

Write a script that automatically runs networks with the following sizes:

* N ∈ {10, 20, 50}

For each N, run the experiment at least ۵ times with different seeds.

### ۴.۳.۲) Data Collection

Each node must log the receive time of each `id_msg` of `GOSSIP`. The analysis script must compute the metrics from the logs.

### ۴.۳.۳) Evaluation Metrics

1. **Time Convergence**
   If the message is produced at time `t0`, the convergence time is the minimum time such that by that moment ۹۵% of nodes have received the message.

2. **Message Overhead**
   The total number of all messages sent (including `GOSSIP` and control messages such as `PONG/PING/LIST_PEERS/PEERS_GET/HELLO`) in the whole network, from the moment the message is produced until reaching ۹۵% coverage.

---

## Computer Networks Course – Project (Page ۷ of ۹)

### ۴.۳.۴) Presenting Results

Present the results with comparative charts:

* Horizontal axis: N
* Vertical axis: convergence time and message overhead

Also interpret the effect of settings `fanout` and `ttl` and neighbor policy, and analyze results for different parameters.

## ۴.۴) Phase 4: Complementary Sections (Mandatory)

In this project, in addition to simple Gossip (Push-based), the following two complementary capabilities are also mandatory. The goal of these sections is to increase project realism and compare practical differences between designs in terms of message overhead and convergence speed.

### ۴.۴.۱) Complementary Part 1: Hybrid Push-Pull

In the simple Push method, after receiving a new message each node sends it to a number of neighbors. This method is usually fast but can increase message overhead (especially when most nodes have already seen the message). In the Hybrid method (combining Push and Pull), in addition to Push, a light mechanism is added to request unseen messages to prevent repeated and useless sending.

New parameters (mandatory and configurable):

* `interval_pull`: time interval for sending Hybrid/Pull messages (e.g., every ۲ seconds)
* `ids_max_ihave`: maximum number of identifiers placed in one `IHAVE` message (e.g., ۳۲)

Each node periodically tells neighbors which messages it has (only `id_msg`s, not the full content). If the neighbor does not have a message, it requests it, and the full message is sent only if needed.

Hybrid messages:

* `IHAVE`: announcement of a summary of new messages (a short list of `id_msg`)
* `IWANT`: request for specific messages (list of required `id_msg`s)

Sample `Payload` for `IHAVE`:

```json
"payload": {
  "ids": ["id1", "id2", "id3"],
  "max_ids": 32
}
```

Sample `Payload` for `IWANT`:

```json
"payload": {
  "ids": ["id2", "id3"]
}
```

---

## Computer Networks Course – Project (Page ۸ of ۹)

Hybrid execution rules:

* Each node every `interval_pull` seconds (configurable) sends `IHAVE` to a number of neighbors.
* `IHAVE` must include only message identifiers and its length must be limited by `ids_max_ihave` so the control message does not become large.
* The receiving node compares the list of `ids` with the Seen Set. Any `id_msg` that does not exist must be requested via `IWANT`.
* The sending node, in response to `IWANT`, sends the full `GOSSIP` message corresponding to each `id_msg`.

Finally, in Phase 3 you must run and compare two modes:

* `only-Push`: only the normal dissemination of `GOSSIP` with Fanout
* `Hybrid Push-Pull`: Push + message `IHAVE/IWANT`

And report the effect of Hybrid on Overhead and Convergence (mean and standard deviation).

### ۴.۴.۲) Complementary Part 2: Resistance Against Sybil With Work-of-Proof

In P2P networks, an attacker can disrupt dissemination by creating many fake nodes (Sybil), polluting others’ Peer Lists. In this project, a light defense mechanism based on (PoW) Work-of-Proof is added for accepting `HELLO`, so joining the network becomes costly.

* `k_pow`: number of required leading zeros in the hash (Difficulty). Suggested example: `k_pow = 4` (configurable)

PoW definition: each new node must find a number called `nonce` such that:

* `H(nonce || id_node)` starts with k leading zeros

Where H is a hash function (e.g., SHA-256) and k is `k_pow`.

Sample `Payload` for `HELLO` with PoW:

```json
"payload": {
  "capabilities": ["udp", "json"],
  "pow": {
    "hash_alg": "sha256",
    "difficulty_k": 4,
    "nonce": 9138472,
    "digest_hex": "0000ab12..."
  }
}
```

---

## Computer Networks Course – Project (Page ۹ of ۹)

Neighbor acceptance rules:

* Each node when receiving `HELLO` must validate PoW: (1) compute that `H(nonce||id_node)` truly starts with k zeros, (2) `k_difficulty` equals the configured `k_pow`.
* If PoW is not valid, the request must be rejected and the sender must not be added to the Peer List.
* k must be chosen such that on a typical laptop it takes from a few hundredths of a second to a few seconds (not so hard that it becomes impractical).

Finally, in the final report, clearly analyze the impact of the PoW mechanism and answer the following:

* **Security benefit**: explain how PoW makes mass joining of fake nodes (Sybil) costly and reduces the probability of polluting the Peer List (even if it does not completely eliminate the attack).
* **Cost and side effect**: report the average time needed to find the nonce for the chosen `k_pow` (e.g., mean and approximate range on your own system) and explain what effect this cost has on the joining speed of honest nodes.
* **Difficulty selection**: state the chosen `k_pow` and argue why this value is balanced (not so easy that it is ineffective, not so hard that it becomes impractical for normal use).
* Provide a small table or a simple chart of PoW generation time versus `k_pow` (for ۲ or ۳ different values) so that the cost trend is tangibly clear.

## ۴.۵) Phase 5: Documentation and Presentation

1. Source code
2. PDF report including design, implementation notes, charts, and analysis
3. Short video (maximum ۱۰ minutes):

* Running the network (e.g., ۱۰ nodes) and disseminating one message
* Explaining important design decisions
* Summarizing analysis results

# ۵) Submission Method

Submit all files (code, report, and video link) in one ZIP file.

Important note: using ready-made libraries that have implemented the Gossip protocol itself is not allowed, but using standard networking libraries, JSON, etc. is allowed. Also, for implementation you may use all programming languages.
