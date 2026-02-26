from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import parse_host_port
from .logging_jsonl import JsonlLogger


KNOWN_MSG_TYPES = {
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
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True)
class MessageContext:
    node_id: str
    self_addr: str
    default_ttl: int


def make_message(
    msg_type: str,
    payload: dict[str, Any],
    ctx: MessageContext,
    ttl: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a JSON object (dict)")

    message: dict[str, Any] = {
        "version": 1,
        "msg_id": str(uuid.uuid4()),
        "msg_type": msg_type,
        "sender_id": ctx.node_id,
        "sender_addr": ctx.self_addr,
        "timestamp_ms": now_ms(),
        "payload": payload,
    }

    if msg_type == "GOSSIP":
        message["ttl"] = ctx.default_ttl if ttl is None else ttl
    elif ttl is not None:
        message["ttl"] = ttl

    return message


def validate_message(message: Any) -> tuple[bool, str]:
    if not isinstance(message, dict):
        return False, "message_not_object"

    required_fields = (
        "version",
        "msg_id",
        "msg_type",
        "sender_id",
        "sender_addr",
        "timestamp_ms",
        "payload",
    )
    for field in required_fields:
        if field not in message:
            return False, f"missing_{field}"

    if not _is_int(message["version"]) or message["version"] != 1:
        return False, "invalid_version"
    if not _is_non_empty_string(message["msg_id"]):
        return False, "invalid_msg_id"
    if not _is_non_empty_string(message["msg_type"]):
        return False, "invalid_msg_type"
    if not _is_non_empty_string(message["sender_id"]):
        return False, "invalid_sender_id"
    if not _is_non_empty_string(message["sender_addr"]):
        return False, "invalid_sender_addr"

    try:
        parse_host_port(message["sender_addr"], "sender_addr")
    except ValueError:
        return False, "invalid_sender_addr_format"

    if not _is_int(message["timestamp_ms"]):
        return False, "invalid_timestamp_ms"
    if not isinstance(message["payload"], dict):
        return False, "invalid_payload_type"

    if message["msg_type"] == "GOSSIP":
        if "ttl" not in message:
            return False, "missing_ttl"
        ttl_value = message["ttl"]
        if not _is_int(ttl_value) or ttl_value < 0:
            return False, "invalid_ttl"
    elif "ttl" in message and message["ttl"] is not None and not _is_int(message["ttl"]):
        return False, "invalid_ttl_type"

    return True, "ok"


def send_message(
    transport: Any,
    logger: JsonlLogger,
    addr: tuple[str, int],
    msg: dict[str, Any],
) -> bool:
    try:
        encoded = json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.log(
            "send_error",
            peer=f"{addr[0]}:{addr[1]}",
            msg_type=msg.get("msg_type"),
            msg_id=msg.get("msg_id"),
            reason=f"serialize_error:{exc}",
        )
        return False

    try:
        transport.sendto(encoded, addr)
    except OSError as exc:
        logger.log(
            "send_error",
            peer=f"{addr[0]}:{addr[1]}",
            msg_type=msg.get("msg_type"),
            msg_id=msg.get("msg_id"),
            reason=f"socket_error:{exc}",
        )
        return False

    logger.log(
        "send_ok",
        peer=f"{addr[0]}:{addr[1]}",
        msg_type=msg.get("msg_type"),
        msg_id=msg.get("msg_id"),
        bytes=len(encoded),
    )
    return True
