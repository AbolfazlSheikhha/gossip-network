from __future__ import annotations

import asyncio
import json
import random
import signal
import uuid
from dataclasses import dataclass
from typing import Any

from .config import Config, parse_config
from .dispatcher import MessageDispatcher
from .logging_jsonl import JsonlLogger
from .messages import MessageContext, send_message, validate_message


@dataclass
class NodeState:
    node_id: str
    self_addr: str
    config: Config


class NodeDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, runtime: "NodeRuntime") -> None:
        self.runtime = runtime

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.runtime.on_connection_made(transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.runtime.on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        self.runtime.logger.log("udp_error", reason=str(exc))

    def connection_lost(self, exc: Exception | None) -> None:
        self.runtime.on_connection_lost(exc)


class NodeRuntime:
    def __init__(self, config: Config) -> None:
        random.seed(config.seed)
        node_id = str(uuid.uuid4())
        self.state = NodeState(node_id=node_id, self_addr=config.self_addr, config=config)
        self.message_ctx = MessageContext(
            node_id=node_id,
            self_addr=config.self_addr,
            default_ttl=config.ttl,
        )
        self.logger = JsonlLogger(node_id=node_id, port=config.port)
        self.dispatcher = MessageDispatcher(logger=self.logger)
        self.transport: asyncio.DatagramTransport | None = None
        self._stop_event = asyncio.Event()

    def on_connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport
        sockname = transport.get_extra_info("sockname")
        bind = f"{sockname[0]}:{sockname[1]}" if sockname else self.state.self_addr
        self.logger.log(
            "node_listening",
            peer=bind,
            bootstrap=self.state.config.bootstrap,
            fanout=self.state.config.fanout,
            ttl=self.state.config.ttl,
            peer_limit=self.state.config.peer_limit,
            ping_interval=self.state.config.ping_interval,
            peer_timeout=self.state.config.peer_timeout,
            seed=self.state.config.seed,
            interval_pull=self.state.config.interval_pull,
            ids_max_ihave=self.state.config.ids_max_ihave,
            k_pow=self.state.config.k_pow,
            log_path=str(self.logger.path),
        )

    def on_connection_lost(self, exc: Exception | None) -> None:
        self.logger.log("node_transport_closed", reason=str(exc) if exc else "normal")
        self.stop()

    def on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        try:
            text = data.decode("utf-8")
            message = json.loads(text)
        except UnicodeDecodeError:
            self.logger.log("recv_invalid_json", peer=peer, bytes=len(data), reason="utf8_decode_error")
            return
        except json.JSONDecodeError as exc:
            self.logger.log(
                "recv_invalid_json",
                peer=peer,
                bytes=len(data),
                reason=f"json_decode_error:{exc.msg}",
            )
            return

        ok, reason = validate_message(message)
        if not ok:
            self.logger.log(
                "recv_invalid_schema",
                peer=peer,
                bytes=len(data),
                reason=reason,
                msg_type=message.get("msg_type") if isinstance(message, dict) else None,
                msg_id=message.get("msg_id") if isinstance(message, dict) else None,
            )
            return

        self.logger.log(
            "recv_ok",
            peer=peer,
            bytes=len(data),
            msg_type=message.get("msg_type"),
            msg_id=message.get("msg_id"),
        )

        try:
            self.dispatcher.dispatch(message, peer=peer)
        except Exception as exc:  # pragma: no cover - defensive safety path
            self.logger.log(
                "handler_error",
                peer=peer,
                msg_type=message.get("msg_type"),
                msg_id=message.get("msg_id"),
                reason=str(exc),
            )

    def send_message(self, addr: tuple[str, int], msg: dict[str, Any]) -> bool:
        if self.transport is None:
            self.logger.log(
                "send_error",
                peer=f"{addr[0]}:{addr[1]}",
                msg_type=msg.get("msg_type"),
                msg_id=msg.get("msg_id"),
                reason="transport_unavailable",
            )
            return False
        return send_message(self.transport, self.logger, addr, msg)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: NodeDatagramProtocol(self),
            local_addr=(self.state.config.bind_host, self.state.config.port),
        )
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport
        try:
            await self._stop_event.wait()
        finally:
            if self.transport is not None:
                self.transport.close()
                self.transport = None
            self.logger.log("node_shutdown")

    def stop(self) -> None:
        if not self._stop_event.is_set():
            self.logger.log("node_stop_requested")
            self._stop_event.set()

    def close(self) -> None:
        self.logger.close()


async def run_node(config: Config) -> None:
    runtime = NodeRuntime(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.stop)
        except (NotImplementedError, RuntimeError):
            # Not available in all environments.
            pass

    try:
        await runtime.run()
    finally:
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    config = parse_config(argv)
    asyncio.run(run_node(config))
    return 0

