from __future__ import annotations

import asyncio
import hashlib
import json
import random
import signal
import uuid
from dataclasses import dataclass
from typing import Any

from .config import Config, parse_config, parse_host_port
from .dispatcher import MessageDispatcher
from .logging_jsonl import JsonlLogger
from .messages import MessageContext, make_message, now_ms, send_message, validate_message


@dataclass
class PeerRecord:
    addr: str
    node_id: str | None
    last_seen_ts_ms: int | None
    consecutive_ping_failures: int = 0
    last_ping_sent_ms: int | None = None
    pending_ping_id: str | None = None
    pending_ping_seq: int | None = None
    rtt_ms: int | None = None
    is_verified_hello: bool = False
    source: str = "peers_list"


class PeerStore:
    def __init__(
        self,
        self_addr: str,
        peer_limit: int,
        peer_timeout_s: int,
        logger: JsonlLogger,
    ) -> None:
        self._self_addr = self_addr
        self._peer_limit = peer_limit
        self._peer_timeout_ms = peer_timeout_s * 1000
        self._logger = logger
        self._peers: dict[str, PeerRecord] = {}

    def upsert(
        self,
        *,
        addr: str,
        node_id: str | None,
        last_seen_ts_ms: int | None,
        source: str,
        mark_hello_verified: bool,
        now_ts_ms: int,
    ) -> dict[str, str | None]:
        if addr == self._self_addr:
            return {"action": "ignored", "reason": "self", "evicted": None}

        existing = self._peers.get(addr)
        if existing is not None:
            if node_id:
                existing.node_id = node_id
            if last_seen_ts_ms is not None:
                existing.last_seen_ts_ms = last_seen_ts_ms
            existing.source = source
            if mark_hello_verified:
                existing.is_verified_hello = True

            self._logger.log(
                "peer_update",
                peer=existing.addr,
                peer_node_id=existing.node_id,
                last_seen_ts_ms=existing.last_seen_ts_ms,
                source=source,
            )
            return {"action": "updated", "reason": "existing", "evicted": None}

        if len(self._peers) >= self._peer_limit:
            evict = self._select_eviction_candidate(now_ts_ms)
            if evict is None:
                return {"action": "ignored", "reason": "peer_limit_reject", "evicted": None}
            del self._peers[evict.addr]
            self._logger.log(
                "peer_evict",
                peer=evict.addr,
                peer_node_id=evict.node_id,
                consecutive_ping_failures=evict.consecutive_ping_failures,
                last_seen_ts_ms=evict.last_seen_ts_ms,
                reason="capacity_replacement",
            )
            evicted_addr: str | None = evict.addr
        else:
            evicted_addr = None

        record = PeerRecord(
            addr=addr,
            node_id=node_id,
            last_seen_ts_ms=last_seen_ts_ms,
            is_verified_hello=mark_hello_verified,
            source=source,
        )
        self._peers[addr] = record
        self._logger.log(
            "peer_add",
            peer=record.addr,
            peer_node_id=record.node_id,
            last_seen_ts_ms=record.last_seen_ts_ms,
            source=source,
        )
        return {"action": "added", "reason": "new", "evicted": evicted_addr}

    def _select_eviction_candidate(self, now_ts_ms: int) -> PeerRecord | None:
        candidate: PeerRecord | None = None
        candidate_score: tuple[int, int, str] | None = None
        for record in self._peers.values():
            last_seen = record.last_seen_ts_ms if record.last_seen_ts_ms is not None else 0
            staleness_ms = max(0, now_ts_ms - last_seen)
            score = (record.consecutive_ping_failures, staleness_ms, record.addr)
            if candidate_score is None or score > candidate_score:
                candidate_score = score
                candidate = record

        if candidate is None or candidate_score is None:
            return None

        failures, staleness_ms, _addr = candidate_score
        if failures >= 3 or staleness_ms > self._peer_timeout_ms:
            return candidate
        return None

    def list_for_peers_list(self, *, limit: int, exclude_addrs: set[str]) -> list[dict[str, str]]:
        peers: list[dict[str, str]] = []
        for addr in sorted(self._peers):
            if addr == self._self_addr or addr in exclude_addrs:
                continue
            record = self._peers[addr]
            if not record.node_id:
                continue
            peers.append({"node_id": record.node_id, "addr": record.addr})
            if len(peers) >= limit:
                break
        return peers


@dataclass
class NodeState:
    node_id: str
    self_addr: str
    config: Config
    peer_store: PeerStore


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
        logger = JsonlLogger(node_id=node_id, port=config.port)
        peer_store = PeerStore(
            self_addr=config.self_addr,
            peer_limit=config.peer_limit,
            peer_timeout_s=config.peer_timeout,
            logger=logger,
        )
        self.state = NodeState(
            node_id=node_id,
            self_addr=config.self_addr,
            config=config,
            peer_store=peer_store,
        )
        self.message_ctx = MessageContext(
            node_id=node_id,
            self_addr=config.self_addr,
            default_ttl=config.ttl,
        )
        self.logger = logger
        self.dispatcher = MessageDispatcher(
            logger=self.logger,
            handlers={
                "HELLO": self.handle_hello,
                "GET_PEERS": self.handle_get_peers,
                "PEERS_LIST": self.handle_peers_list,
                "PING": self.handle_ping,
                "PONG": self.handle_pong,
            },
        )
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
            pull_interval=self.state.config.pull_interval,
            ids_max_ihave=self.state.config.ids_max_ihave,
            k_pow=self.state.config.k_pow,
            log_path=str(self.logger.path),
        )
        self._bootstrap_join()

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

    def _bootstrap_join(self) -> None:
        if self.state.config.bootstrap == self.state.self_addr:
            self.logger.log("bootstrap_skipped_self")
            return

        try:
            host, port = parse_host_port(self.state.config.bootstrap, "bootstrap")
        except ValueError as exc:
            self.logger.log("bootstrap_invalid", reason=str(exc))
            return

        bootstrap_addr = (host, port)
        hello_msg = make_message(
            "HELLO",
            self._build_hello_payload(),
            self.message_ctx,
        )
        if self.send_message(bootstrap_addr, hello_msg):
            self.logger.log(
                "bootstrap_hello_sent",
                peer=self.state.config.bootstrap,
                msg_id=hello_msg["msg_id"],
            )

        get_peers_msg = make_message(
            "GET_PEERS",
            {"max_peers": self.state.config.peer_limit},
            self.message_ctx,
        )
        if self.send_message(bootstrap_addr, get_peers_msg):
            self.logger.log(
                "bootstrap_get_peers_sent",
                peer=self.state.config.bootstrap,
                msg_id=get_peers_msg["msg_id"],
                max_peers=self.state.config.peer_limit,
            )

    def _build_hello_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"capabilities": ["udp", "json"]}
        if self.state.config.k_pow > 0:
            payload["pow"] = self._create_pow(self.state.config.k_pow)
        return payload

    def _create_pow(self, difficulty_k: int) -> dict[str, Any]:
        target = "0" * difficulty_k
        while True:
            nonce = random.getrandbits(32)
            digest = hashlib.sha256(f"{nonce}{self.state.node_id}".encode("utf-8")).hexdigest()
            if digest.startswith(target):
                return {
                    "hash_alg": "sha256",
                    "difficulty_k": difficulty_k,
                    "nonce": nonce,
                    "digest_hex": digest,
                }

    def _parse_peer_tuple(self, raw: str) -> tuple[str, int] | None:
        try:
            host, port = parse_host_port(raw, "peer")
        except ValueError:
            return None
        return host, port

    def _touch_sender(self, msg: dict[str, Any], source: str, mark_hello_verified: bool) -> dict[str, str | None]:
        return self.state.peer_store.upsert(
            addr=str(msg.get("sender_addr")),
            node_id=str(msg.get("sender_id")),
            last_seen_ts_ms=now_ms(),
            source=source,
            mark_hello_verified=mark_hello_verified,
            now_ts_ms=now_ms(),
        )

    def _validate_hello(self, msg: dict[str, Any]) -> tuple[bool, str]:
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return False, "payload_not_object"

        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            return False, "missing_or_invalid_capabilities"
        capability_set = {item.lower() for item in capabilities}
        if "udp" not in capability_set or "json" not in capability_set:
            return False, "capabilities_missing_udp_json"

        difficulty = self.state.config.k_pow
        if difficulty <= 0:
            return True, "ok"

        pow_payload = payload.get("pow")
        if not isinstance(pow_payload, dict):
            return False, "pow_missing"
        if pow_payload.get("hash_alg") != "sha256":
            return False, "pow_hash_alg_invalid"

        difficulty_k = pow_payload.get("difficulty_k")
        if not isinstance(difficulty_k, int) or isinstance(difficulty_k, bool):
            return False, "pow_difficulty_invalid_type"
        if difficulty_k != difficulty:
            return False, "pow_difficulty_mismatch"

        nonce = pow_payload.get("nonce")
        if not isinstance(nonce, int) or isinstance(nonce, bool):
            return False, "pow_nonce_invalid_type"

        digest_hex = pow_payload.get("digest_hex")
        if not isinstance(digest_hex, str) or not digest_hex:
            return False, "pow_digest_invalid_type"

        expected = hashlib.sha256(f"{nonce}{msg['sender_id']}".encode("utf-8")).hexdigest()
        if digest_hex != expected:
            return False, "pow_digest_mismatch"
        if not digest_hex.startswith("0" * difficulty):
            return False, "pow_difficulty_not_met"

        return True, "ok"

    def handle_hello(self, msg: dict[str, Any], peer: str) -> None:
        is_valid, reason = self._validate_hello(msg)
        if not is_valid:
            self.logger.log(
                "hello_rejected",
                peer=peer,
                msg_type=msg.get("msg_type"),
                msg_id=msg.get("msg_id"),
                reason=reason,
            )
            return

        result = self._touch_sender(msg, source="hello", mark_hello_verified=True)
        self.logger.log(
            "hello_accepted",
            peer=str(msg.get("sender_addr", peer)),
            msg_id=msg.get("msg_id"),
            action=result["action"],
            reason=result["reason"],
        )

    def handle_get_peers(self, msg: dict[str, Any], peer: str) -> None:
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            self.logger.log("get_peers_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="payload_not_object")
            return

        requested_max: int | None = None
        if "max_peers" in payload:
            max_peers = payload.get("max_peers")
            if not isinstance(max_peers, int) or isinstance(max_peers, bool) or max_peers < 1:
                self.logger.log(
                    "get_peers_invalid",
                    peer=peer,
                    msg_id=msg.get("msg_id"),
                    reason="invalid_max_peers",
                )
                return
            requested_max = max_peers

        self._touch_sender(msg, source="hello", mark_hello_verified=False)

        limit = min(requested_max or self.state.config.peer_limit, self.state.config.peer_limit)
        exclude = {self.state.self_addr, str(msg.get("sender_addr"))}
        peers_payload = self.state.peer_store.list_for_peers_list(limit=limit, exclude_addrs=exclude)

        peer_tuple = self._parse_peer_tuple(peer)
        if peer_tuple is None:
            self.logger.log("send_error", peer=peer, msg_type="PEERS_LIST", reason="invalid_requester_peer")
            return

        peers_msg = make_message("PEERS_LIST", {"peers": peers_payload}, self.message_ctx)
        if self.send_message(peer_tuple, peers_msg):
            self.logger.log(
                "peers_list_sent",
                peer=peer,
                msg_id=peers_msg["msg_id"],
                requested_max=requested_max,
                returned=len(peers_payload),
            )

    def handle_peers_list(self, msg: dict[str, Any], peer: str) -> None:
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            self.logger.log("peers_list_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="payload_not_object")
            return

        peers = payload.get("peers")
        if not isinstance(peers, list):
            self.logger.log("peers_list_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="invalid_peers_field")
            return

        self._touch_sender(msg, source="hello", mark_hello_verified=False)

        added = 0
        updated = 0
        ignored = 0
        evicted = 0
        seen_addrs: set[str] = set()

        for entry in peers:
            if not isinstance(entry, dict):
                ignored += 1
                continue

            addr_value = entry.get("addr")
            node_id_value = entry.get("node_id")

            if not isinstance(addr_value, str) or self._parse_peer_tuple(addr_value) is None:
                ignored += 1
                continue
            if addr_value == self.state.self_addr or addr_value in seen_addrs:
                ignored += 1
                continue
            seen_addrs.add(addr_value)

            node_id: str | None = None
            if node_id_value is not None:
                if not isinstance(node_id_value, str) or not node_id_value.strip():
                    ignored += 1
                    continue
                node_id = node_id_value

            outcome = self.state.peer_store.upsert(
                addr=addr_value,
                node_id=node_id,
                last_seen_ts_ms=None,
                source="peers_list",
                mark_hello_verified=False,
                now_ts_ms=now_ms(),
            )
            action = outcome["action"]
            if action == "added":
                added += 1
                if outcome["evicted"] is not None:
                    evicted += 1
            elif action == "updated":
                updated += 1
            else:
                ignored += 1

        self.logger.log(
            "peers_list_received",
            peer=peer,
            msg_id=msg.get("msg_id"),
            received=len(peers),
            added=added,
            updated=updated,
            ignored=ignored,
            evicted=evicted,
        )

    def handle_ping(self, msg: dict[str, Any], peer: str) -> None:
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            self.logger.log("ping_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="payload_not_object")
            return

        ping_id = payload.get("ping_id")
        seq = payload.get("seq")
        if not isinstance(ping_id, str) or not ping_id:
            self.logger.log("ping_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="invalid_ping_id")
            return
        if not isinstance(seq, int) or isinstance(seq, bool):
            self.logger.log("ping_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="invalid_seq")
            return

        self._touch_sender(msg, source="hello", mark_hello_verified=False)
        peer_tuple = self._parse_peer_tuple(peer)
        if peer_tuple is None:
            self.logger.log("send_error", peer=peer, msg_type="PONG", reason="invalid_ping_sender_peer")
            return

        pong_msg = make_message("PONG", {"ping_id": ping_id, "seq": seq}, self.message_ctx)
        self.send_message(peer_tuple, pong_msg)

    def handle_pong(self, msg: dict[str, Any], peer: str) -> None:
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            self.logger.log("pong_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="payload_not_object")
            return

        ping_id = payload.get("ping_id")
        seq = payload.get("seq")
        if not isinstance(ping_id, str) or not ping_id:
            self.logger.log("pong_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="invalid_ping_id")
            return
        if not isinstance(seq, int) or isinstance(seq, bool):
            self.logger.log("pong_invalid", peer=peer, msg_id=msg.get("msg_id"), reason="invalid_seq")
            return

        self._touch_sender(msg, source="hello", mark_hello_verified=False)

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
