from __future__ import annotations

from typing import Any, Callable

from .logging_jsonl import JsonlLogger
from .messages import KNOWN_MSG_TYPES


class MessageDispatcher:
    def __init__(
        self,
        logger: JsonlLogger,
        handlers: dict[str, Callable[[dict[str, Any], str], None]] | None = None,
    ) -> None:
        self.logger = logger
        self._handlers: dict[str, Callable[[dict[str, Any], str], None]] = {
            "HELLO": self.handle_hello,
            "GET_PEERS": self.handle_get_peers,
            "PEERS_LIST": self.handle_peers_list,
            "PING": self.handle_ping,
            "PONG": self.handle_pong,
            "GOSSIP": self.handle_gossip,
            "IHAVE": self.handle_ihave,
            "IWANT": self.handle_iwant,
        }
        if handlers:
            self._handlers.update(handlers)

    def dispatch(self, msg: dict[str, Any], peer: str) -> None:
        msg_type = str(msg.get("msg_type", ""))
        handler = self._handlers.get(msg_type)
        if handler is None:
            self.handle_unknown(msg, peer)
            return
        handler(msg, peer)

    def _log_stub(self, handler_name: str, msg: dict[str, Any], peer: str) -> None:
        self.logger.log(
            "handler_stub",
            handler=handler_name,
            peer=peer,
            msg_type=msg.get("msg_type"),
            msg_id=msg.get("msg_id"),
            sender_id=msg.get("sender_id"),
        )

    def handle_unknown(self, msg: dict[str, Any], peer: str) -> None:
        self.logger.log(
            "recv_unknown_type",
            peer=peer,
            msg_type=msg.get("msg_type"),
            msg_id=msg.get("msg_id"),
            known_types="|".join(sorted(KNOWN_MSG_TYPES)),
        )

    def handle_hello(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_hello", msg, peer)

    def handle_get_peers(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_get_peers", msg, peer)

    def handle_peers_list(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_peers_list", msg, peer)

    def handle_ping(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_ping", msg, peer)

    def handle_pong(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_pong", msg, peer)

    def handle_gossip(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_gossip", msg, peer)

    def handle_ihave(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_ihave", msg, peer)

    def handle_iwant(self, msg: dict[str, Any], peer: str) -> None:
        self._log_stub("handle_iwant", msg, peer)
