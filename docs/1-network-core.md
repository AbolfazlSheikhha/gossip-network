# Stage 2 - Node Network Core (UDP + Skeleton Handlers)

## Overview
This stage implements the runtime plumbing only (no full protocol logic yet):
- UDP bind and receive loop using `asyncio` datagram endpoint.
- JSON decode and envelope validation that never crashes on bad input.
- Dispatcher by `msg_type` with stub handlers for all protocol types.
- Structured JSONL logging to `logs/` for later analysis.

## Architecture
- `src/config.py`
  - `Config` dataclass
  - CLI argument parsing/validation for all required project parameters
- `src/logging_jsonl.py`
  - `JsonlLogger` for structured logs (`.jsonl`) with flush-on-write
- `src/messages.py`
  - Message constants and envelope validation
  - `make_message(msg_type, payload, ctx, ttl=None)`
  - `validate_message(message) -> (ok, reason)`
  - `send_message(transport, logger, addr, msg)`
- `src/dispatcher.py`
  - `MessageDispatcher` routes by message type
  - Stub handlers for `HELLO`, `GET_PEERS`, `PEERS_LIST`, `PING`, `PONG`, `GOSSIP`, `IHAVE`, `IWANT`
  - `handle_unknown` for unsupported types
- `src/node.py`
  - `NodeRuntime` and UDP protocol callback implementation
  - Receive path: decode -> parse -> validate -> dispatch
  - Graceful shutdown with logger close
- `src/__main__.py`
  - Entrypoint so `python -m src ...` works
- `scripts/send_test.py`
  - Sends valid/invalid UDP payloads for smoke testing

## CLI Usage
Run node:

```bash
python -m src \
  --port 8000 \
  --bootstrap 127.0.0.1:9000 \
  --fanout 3 \
  --ttl 8 \
  --peer-limit 20 \
  --ping-interval 2 \
  --peer-timeout 6 \
  --seed 42 \
  --pull-interval 2 \
  --ids-max-ihave 32 \
  --k-pow 0
```

Supported arguments:
- Required now: `--port`, `--bootstrap`, `--fanout`, `--ttl`, `--peer-limit`, `--ping-interval`, `--peer-timeout`, `--seed`
- Accepted for later stages: `--pull-interval`, `--ids-max-ihave`, `--k-pow`

## Logging Schema
Logs are written to `logs/node-<port>-<ts>-<node>.jsonl`.

Schema keys (per event):
- `ts_ms`: event time (epoch ms)
- `event`: event name
- `node_id`: local node UUID
- `peer`: peer `ip:port` when applicable
- `msg_type`: protocol type when applicable
- `msg_id`: message ID when applicable
- `bytes`: datagram size when applicable
- `reason`: failure reason when applicable

Key receive/send events:
- `recv_ok`
- `recv_invalid_json`
- `recv_invalid_schema`
- `recv_unknown_type`
- `send_ok`
- `send_error`
- `handler_stub`

## Smoke Test
1. Start node in terminal A:

```bash
python -m src \
  --port 8000 \
  --bootstrap 127.0.0.1:9000 \
  --fanout 3 \
  --ttl 8 \
  --peer-limit 20 \
  --ping-interval 2 \
  --peer-timeout 6 \
  --seed 42
```

2. Send invalid JSON in terminal B:

```bash
python scripts/send_test.py --port 8000 --kind invalid-json
```

3. Send valid envelope with unknown type:

```bash
python scripts/send_test.py --port 8000 --kind unknown --msg-type RANDOM_TYPE
```

4. Send valid known type (`HELLO` stub):

```bash
python scripts/send_test.py --port 8000 --kind known --msg-type HELLO
```

5. Inspect latest log file in `logs/` and verify:
- invalid JSON logged (`recv_invalid_json`)
- unknown type logged (`recv_unknown_type`)
- known type handler stub logged (`handler_stub`)

## Notes
- Canonical message names follow Stage 1 spec (`GET_PEERS`, `PEERS_LIST`).
- Non-canonical message type names are treated as unknown and ignored.
- Full peer discovery and gossip behavior are intentionally deferred to later stages.
