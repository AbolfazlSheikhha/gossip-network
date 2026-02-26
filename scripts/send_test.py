#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import time
import uuid
from typing import Any


KNOWN_TYPES = {
    "HELLO",
    "GET_PEERS",
    "PEERS_LIST",
    "PING",
    "PONG",
    "GOSSIP",
    "IHAVE",
    "IWANT",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def build_message(msg_type: str, sender_addr: str) -> dict[str, Any]:
    payload: dict[str, Any]
    ttl: int | None = None

    if msg_type == "HELLO":
        payload = {"capabilities": ["udp", "json"]}
    elif msg_type == "GET_PEERS":
        payload = {"max_peers": 20}
    elif msg_type == "PEERS_LIST":
        payload = {"peers": [{"node_id": str(uuid.uuid4()), "addr": "127.0.0.1:9001"}]}
    elif msg_type == "PING":
        payload = {"ping_id": str(uuid.uuid4()), "seq": 1}
    elif msg_type == "PONG":
        payload = {"ping_id": str(uuid.uuid4()), "seq": 1}
    elif msg_type == "GOSSIP":
        payload = {
            "topic": "smoke",
            "data": "hello",
            "origin_id": str(uuid.uuid4()),
            "origin_timestamp_ms": now_ms(),
        }
        ttl = 3
    elif msg_type == "IHAVE":
        payload = {"ids": [str(uuid.uuid4())], "max_ids": 32}
    elif msg_type == "IWANT":
        payload = {"ids": [str(uuid.uuid4())]}
    else:
        payload = {"note": "unknown type smoke"}

    msg: dict[str, Any] = {
        "version": 1,
        "msg_id": str(uuid.uuid4()),
        "msg_type": msg_type,
        "sender_id": str(uuid.uuid4()),
        "sender_addr": sender_addr,
        "timestamp_ms": now_ms(),
        "payload": payload,
    }
    if ttl is not None:
        msg["ttl"] = ttl
    return msg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send test UDP datagrams to a node")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--kind",
        choices=("known", "unknown", "invalid-json", "invalid-schema"),
        default="known",
    )
    parser.add_argument("--msg-type", default="HELLO")
    parser.add_argument("--sender-host", default="127.0.0.1")
    parser.add_argument("--sender-port", type=int, default=9999)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sender_addr = f"{args.sender_host}:{args.sender_port}"
    target = (args.host, args.port)

    if args.kind == "invalid-json":
        payload = b"{this is not valid json"
    elif args.kind == "invalid-schema":
        payload = json.dumps({"msg_type": "HELLO"}, separators=(",", ":")).encode("utf-8")
    elif args.kind == "unknown":
        msg = build_message(args.msg_type, sender_addr)
        if msg["msg_type"] in KNOWN_TYPES:
            msg["msg_type"] = "NOT_A_REAL_TYPE"
        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    else:
        if args.msg_type not in KNOWN_TYPES:
            raise SystemExit(f"--msg-type must be one of {sorted(KNOWN_TYPES)} for --kind known")
        msg = build_message(args.msg_type, sender_addr)
        payload = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sent = sock.sendto(payload, target)

    print(f"sent {sent} bytes to {args.host}:{args.port} ({args.kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

