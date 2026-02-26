import argparse
import asyncio
import hashlib
import json
import logging
import random
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


LOG_FORMAT = "[%(asctime)s] %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("gossip-node")


VERSION = 1


def _parse_bool(s: str) -> bool:
    s = (s or "").strip().lower()
    return s in ("true", "1", "yes", "y", "on")


@dataclass
class PeerInfo:
    node_id: str
    addr: Tuple[str, int]
    last_seen: float = field(default_factory=lambda: time.time())
    last_ping: float = 0.0
    missed_pongs: int = 0


@dataclass
class NodeConfig:
    port: int
    bootstrap: Optional[Tuple[str, int]]
    fanout: int
    ttl: int
    peer_limit: int
    ping_interval: float
    peer_timeout: float
    seed: int
    # hybrid push-pull
    pull_interval: float = 2.0
    ihave_max_ids: int = 32
    # proof-of-work difficulty (leading zero hex digits)
    pow_k: int = 0
    # stdin gossip loop toggle
    stdin_enabled: bool = True
    # periodic peer discovery via GET_PEERS
    discovery_interval: float = 4.0


class GossipNodeProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: "GossipNode"):
        self.node = node

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.node.transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        logger.info("UDP socket ready on %s:%d", *self.node.self_addr)

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        asyncio.create_task(self.node.handle_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        logger.error("Datagram error: %s", exc)


class GossipNode:
    def __init__(self, cfg: NodeConfig) -> None:
        self.cfg = cfg
        self.node_id = str(uuid.uuid4())
        self.self_addr = ("127.0.0.1", cfg.port)
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.peers: Dict[str, PeerInfo] = {}
        self.seen_msgs: Set[str] = set()
        self.random = random.Random(cfg.seed + cfg.port)
        # cache of recent gossip messages for Hybrid IWANT responses
        self.gossip_cache: Dict[str, Dict] = {}
        self._hello_pow_payload: Optional[Dict] = None
        # Remove peer after repeated missed pong windows.
        self._max_missed_pongs: int = 3

    # networking helpers
    def _base_msg(self, msg_type: str, ttl: Optional[int] = None) -> Dict:
        return {
            "version": VERSION,
            "msg_id": str(uuid.uuid4()),
            "msg_type": msg_type,
            "sender_id": self.node_id,
            "sender_addr": f"{self.self_addr[0]}:{self.self_addr[1]}",
            "timestamp_ms": int(time.time() * 1000),
            "ttl": self.cfg.ttl if ttl is None else ttl,
            "payload": {},
        }

    def _send_raw(self, msg: Dict, addr: Tuple[str, int]) -> None:
        if not self.transport:
            return
        try:
            data = json.dumps(msg).encode("utf-8")
        except Exception as e:
            logger.error("JSON encode error: %s", e)
            return
        self.transport.sendto(data, addr)
        logger.info(
            "SEND type=%s msg_id=%s dest=%s:%d at_ms=%d",
            msg.get("msg_type"),
            msg.get("msg_id"),
            addr[0],
            addr[1],
            int(time.time() * 1000),
        )

    def _broadcast(self, msg: Dict, exclude: Optional[str] = None) -> None:
        if exclude is None:
            peer_items = list(self.peers.items())
        else:
            peer_items = [(pid, p) for pid, p in self.peers.items() if pid != exclude]
        if not peer_items:
            return
        self.random.shuffle(peer_items)
        fanout = min(self.cfg.fanout, len(peer_items))
        selected = peer_items[:fanout]
        for peer_id, p in selected:
            self._send_raw(msg, p.addr)
            logger.info("FORWARD %s to %s", msg["msg_type"], p.addr)

    # message creation
    def _make_hello(self) -> Dict:
        msg = self._base_msg("HELLO")
        payload: Dict = {"capabilities": ["udp", "json"]}
        if self.cfg.pow_k > 0:
            if self._hello_pow_payload is None:
                self._hello_pow_payload = self._compute_pow(self.node_id, self.cfg.pow_k)
            payload["pow"] = self._hello_pow_payload
        msg["payload"] = payload
        return msg

    def _make_get_peers(self, max_peers: int) -> Dict:
        msg = self._base_msg("GET_PEERS")
        msg["payload"] = {"max_peers": max_peers}
        return msg

    def _make_peers_list(self) -> Dict:
        msg = self._base_msg("PEERS_LIST")
        peers_payload = []
        for p in self.peers.values():
            peers_payload.append(
                {"node_id": p.node_id, "addr": f"{p.addr[0]}:{p.addr[1]}"}
            )
        msg["payload"] = {"peers": peers_payload}
        return msg

    def _make_ping(self) -> Dict:
        msg = self._base_msg("PING")
        msg["payload"] = {
            "ping_id": str(uuid.uuid4()),
            "seq": self.random.randint(0, 1_000_000),
        }
        return msg

    def _make_pong(self, ping_payload: Dict) -> Dict:
        msg = self._base_msg("PONG")
        msg["payload"] = {
            "ping_id": ping_payload.get("ping_id"),
            "seq": ping_payload.get("seq"),
        }
        return msg

    def _make_gossip(self, topic: str, data: str, origin_id: Optional[str] = None) -> Dict:
        msg = self._base_msg("GOSSIP")
        origin = origin_id or self.node_id
        msg["payload"] = {
            "topic": topic,
            "data": data,
            "origin_id": origin,
            "origin_timestamp_ms": int(time.time() * 1000),
        }
        return msg

    def _make_ihave(self, ids: List[str]) -> Dict:
        msg = self._base_msg("IHAVE")
        msg["payload"] = {"ids": ids[: self.cfg.ihave_max_ids], "max_ids": self.cfg.ihave_max_ids}
        return msg

    def _make_iwant(self, ids: List[str]) -> Dict:
        msg = self._base_msg("IWANT")
        msg["payload"] = {"ids": ids}
        return msg

    # ----------------- PoW helpers -----------------
    @staticmethod
    def _compute_pow(node_id: str, k: int) -> Dict:
        assert k >= 0
        if k == 0:
            return {
                "hash_alg": "sha256",
                "difficulty_k": 0,
                "nonce": 0,
                "digest_hex": "",
            }
        prefix = "0" * k
        nonce = 0
        while True:
            h = hashlib.sha256(f"{node_id}{nonce}".encode("utf-8")).hexdigest()
            if h.startswith(prefix):
                return {
                    "hash_alg": "sha256",
                    "difficulty_k": k,
                    "nonce": nonce,
                    "digest_hex": h,
                }
            nonce += 1

    @staticmethod
    def _verify_pow(node_id: str, pow_payload: Dict, k: int) -> bool:
        try:
            hash_alg = str(pow_payload.get("hash_alg", "")).lower()
            difficulty_k = int(pow_payload.get("difficulty_k", -1))
            nonce = int(pow_payload.get("nonce", -1))
            digest_hex = str(pow_payload.get("digest_hex", ""))
        except Exception:
            return False
        if hash_alg != "sha256":
            return False
        if difficulty_k != k:
            return False
        if nonce < 0:
            return False
        computed_digest = hashlib.sha256(f"{node_id}{nonce}".encode("utf-8")).hexdigest()
        return computed_digest == digest_hex and computed_digest.startswith("0" * k)

    # peer mgmt
    def _update_peer(self, node_id: str, addr: Tuple[str, int]) -> None:
        now = time.time()
        if node_id in self.peers:
            p = self.peers[node_id]
            p.addr = addr
            p.last_seen = now
        else:
            if len(self.peers) >= self.cfg.peer_limit:
                oldest_id = min(self.peers.items(), key=lambda kv: kv[1].last_seen)[0]
                logger.info("Dropping peer %s due to peer_limit", oldest_id)
                self.peers.pop(oldest_id, None)
            self.peers[node_id] = PeerInfo(node_id=node_id, addr=addr, last_seen=now)
        logger.debug("Peer set size=%d", len(self.peers))

    async def handle_datagram(self, data: bytes, addr: Tuple[str, int]) -> None:
        try:
            text = data.decode("utf-8")
            msg = json.loads(text)
        except Exception:
            logger.warning("Invalid JSON from %s", addr)
            return

        msg_type = msg.get("msg_type")
        sender_id = msg.get("sender_id")
        ttl = msg.get("ttl", 0)
        payload = msg.get("payload", {}) or {}

        if not msg_type or not sender_id:
            logger.warning("Missing fields in message from %s", addr)
            return

        logger.info(
            "RECV type=%s msg_id=%s from=%s:%d",
            msg_type,
            msg.get("msg_id"),
            addr[0],
            addr[1],
        )

        if sender_id != self.node_id and msg_type != "HELLO":
            self._update_peer(sender_id, addr)

        if msg_type == "HELLO":
            await self._on_hello(msg, addr)
        elif msg_type == "GET_PEERS":
            await self._on_get_peers(msg, addr)
        elif msg_type == "PEERS_LIST":
            await self._on_peers_list(msg)
        elif msg_type == "PING":
            await self._on_ping(msg, addr)
        elif msg_type == "PONG":
            await self._on_pong(msg)
        elif msg_type == "GOSSIP":
            await self._on_gossip(msg, ttl, sender_id)
        elif msg_type == "IHAVE":
            await self._on_ihave(msg, sender_id)
        elif msg_type == "IWANT":
            await self._on_iwant(msg, sender_id)
        else:
            logger.warning("Unknown msg_type=%s from %s", msg_type, addr)

    async def _on_hello(self, msg: Dict, addr: Tuple[str, int]) -> None:
        logger.info("HELLO from %s", addr)
        sender_id = msg.get("sender_id")
        payload = msg.get("payload") or {}
        pow_payload = payload.get("pow")
        if self.cfg.pow_k > 0:
            if not isinstance(sender_id, str) or not isinstance(pow_payload, dict):
                logger.warning("HELLO without valid PoW from %s rejected", addr)
                return
            if not self._verify_pow(sender_id, pow_payload, self.cfg.pow_k):
                logger.warning("HELLO with invalid PoW from %s rejected", addr)
                return
        if isinstance(sender_id, str):
            self._update_peer(sender_id, addr)
        reply = self._make_peers_list()
        self._send_raw(reply, addr)

    async def _on_get_peers(self, msg: Dict, addr: Tuple[str, int]) -> None:
        payload = msg.get("payload") or {}
        max_peers = int(payload.get("max_peers", self.cfg.peer_limit))
        logger.info("GET_PEERS from %s (max=%d)", addr, max_peers)

        peers_payload = []
        peers_payload.append({"node_id": self.node_id, "addr": f"{self.self_addr[0]}:{self.self_addr[1]}"})
        for p in self.peers.values():
            peers_payload.append({"node_id": p.node_id, "addr": f"{p.addr[0]}:{p.addr[1]}"})
            if len(peers_payload) >= max_peers:
                break

        reply = self._base_msg("PEERS_LIST")
        reply["payload"] = {"peers": peers_payload}
        self._send_raw(reply, addr)

    async def _on_peers_list(self, msg: Dict) -> None:
        payload = msg.get("payload") or {}
        peers = payload.get("peers") or []
        logger.info("Received PEERS_LIST with %d peers", len(peers))
        discovered_new = 0
        for p in peers:
            try:
                addr_str = p["addr"]
                host, port_str = addr_str.split(":")
                port = int(port_str)
                node_id = p["node_id"]
            except Exception:
                continue
            if node_id == self.node_id:
                continue
            was_known = node_id in self.peers
            self._update_peer(node_id, (host, port))
            if not was_known:
                discovered_new += 1
                self._send_raw(self._make_hello(), (host, port))
        if discovered_new:
            logger.info("Discovered %d new peers from PEERS_LIST", discovered_new)

    async def _on_ping(self, msg: Dict, addr: Tuple[str, int]) -> None:
        logger.debug("PING from %s", addr)
        pong = self._make_pong(msg.get("payload") or {})
        self._send_raw(pong, addr)

    async def _on_pong(self, msg: Dict) -> None:
        logger.debug("PONG from %s", msg.get("sender_addr"))
        sender_id = msg.get("sender_id")
        if isinstance(sender_id, str) and sender_id in self.peers:
            p = self.peers[sender_id]
            p.missed_pongs = 0

    async def _on_gossip(self, msg: Dict, ttl: int, sender_id: str) -> None:
        msg_id = msg.get("msg_id")
        if not msg_id:
            return
        if msg_id in self.seen_msgs:
            return
        self.seen_msgs.add(msg_id)
        now_ms = int(time.time() * 1000)
        payload = msg.get("payload") or {}
        topic = payload.get("topic")
        data = payload.get("data")
        logger.info(
            "GOSSIP_RECEIVED node_id=%s port=%d msg_id=%s topic=%s data=%s from=%s at_ms=%d origin_ts=%s",
            self.node_id,
            self.self_addr[1],
            msg_id,
            topic,
            data,
            msg.get("sender_addr"),
            now_ms,
            payload.get("origin_timestamp_ms"),
        )
        self.gossip_cache[msg_id] = msg
        if len(self.gossip_cache) > 1024:
            first_key = next(iter(self.gossip_cache.keys()))
            self.gossip_cache.pop(first_key, None)
        if ttl <= 0:
            return
        fwd = dict(msg)
        fwd["ttl"] = ttl - 1
        self._broadcast(fwd, exclude=sender_id)

    async def _on_ihave(self, msg: Dict, sender_id: str) -> None:
        payload = msg.get("payload") or {}
        ids = payload.get("ids") or []
        unknown_ids = [mid for mid in ids if mid not in self.seen_msgs]
        if not unknown_ids:
            return
        logger.debug("IHAVE from %s, requesting %d messages", sender_id, len(unknown_ids))
        iwant = self._make_iwant(unknown_ids)
        peer = self.peers.get(sender_id)
        if peer:
            self._send_raw(iwant, peer.addr)

    async def _on_iwant(self, msg: Dict, sender_id: str) -> None:
        payload = msg.get("payload") or {}
        ids = payload.get("ids") or []
        peer = self.peers.get(sender_id)
        if not peer:
            return
        for mid in ids:
            msg_obj = self.gossip_cache.get(mid)
            if msg_obj is None:
                continue
            self._send_raw(msg_obj, peer.addr)
        logger.debug("IWANT from %s, sent %d messages", sender_id, len(ids))

    # periodic tasks 
    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.ping_interval)
            now = time.time()
            dead: List[str] = []
            for pid, p in self.peers.items():
                if p.last_ping > 0 and (now - p.last_ping) > self.cfg.peer_timeout and (now - p.last_seen) > self.cfg.peer_timeout:
                    p.missed_pongs += 1
                    p.last_ping = now
                    logger.debug("Peer %s missed pong (%d/%d)", pid, p.missed_pongs, self._max_missed_pongs)
                    if p.missed_pongs >= self._max_missed_pongs:
                        dead.append(pid)
            for pid in dead:
                info = self.peers.pop(pid, None)
                logger.info("Peer %s removed (timeout) addr=%s", pid, info.addr if info else None)

            # Ping a few peers
            if not self.peers:
                continue
            peer_items = list(self.peers.items())
            self.random.shuffle(peer_items)
            count = min(self.cfg.fanout, len(peer_items))
            ping = self._make_ping()
            for pid, p in peer_items[:count]:
                self._send_raw(ping, p.addr)
                p.last_ping = now
                logger.debug("PING sent to %s", p.addr)

    async def _hybrid_pull_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.pull_interval)
            if self.cfg.pull_interval <= 0:
                continue
            if not self.peers or not self.seen_msgs:
                continue
            peer_items = list(self.peers.items())
            self.random.shuffle(peer_items)
            count = min(self.cfg.fanout, len(peer_items))
            ids_list = list(self.seen_msgs)
            self.random.shuffle(ids_list)
            ihave_msg = self._make_ihave(ids_list)
            for peer_id, p in peer_items[:count]:
                self._send_raw(ihave_msg, p.addr)
            logger.debug("IHAVE broadcast to %d peers", count)

    async def _peer_discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.discovery_interval)
            if self.cfg.discovery_interval <= 0:
                continue
            if not self.peers:
                continue
            peer_items = list(self.peers.items())
            self.random.shuffle(peer_items)
            # Query up to fanout peers to keep the overlay connected over time.
            count = min(self.cfg.fanout, len(peer_items))
            for _, p in peer_items[:count]:
                self._send_raw(self._make_get_peers(self.cfg.peer_limit), p.addr)

    async def _stdin_gossip_loop(self) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)

        try:
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception as e:
            logger.error("stdin gossip loop disabled (failed to attach stdin): %s", e)
            return

        logger.info("Type messages to send GOSSIP. Ctrl-D to quit.")
        while True:
            try:
                line = await reader.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8").strip()
            if not text:
                continue
            msg = self._make_gossip(topic="user", data=text)
            msg_id = msg["msg_id"]
            self.seen_msgs.add(msg_id)
            logger.info(
                "GOSSIP_ORIGINATED node_id=%s port=%d msg_id=%s data=%s at_ms=%d",
                self.node_id,
                self.self_addr[1],
                msg_id,
                text,
                msg["payload"]["origin_timestamp_ms"],
            )
            self._broadcast(msg)

    # startup
    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: GossipNodeProtocol(self),
            local_addr=("0.0.0.0", self.cfg.port),
        )
        self.transport = transport  # type: ignore[assignment]

        if self.cfg.bootstrap is not None:
            bootstrap_addr = self.cfg.bootstrap
            logger.info("Bootstrapping via %s:%d", *bootstrap_addr)
            hello = self._make_hello()
            self._send_raw(hello, bootstrap_addr)
            get_peers = self._make_get_peers(self.cfg.peer_limit)
            self._send_raw(get_peers, bootstrap_addr)

        tasks = [
            asyncio.create_task(self._ping_loop()),
            asyncio.create_task(self._hybrid_pull_loop()),
            asyncio.create_task(self._peer_discovery_loop()),
        ]
        if self.cfg.stdin_enabled:
            tasks.append(asyncio.create_task(self._stdin_gossip_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            transport.close()


def parse_args(argv: Optional[List[str]] = None) -> NodeConfig:
    parser = argparse.ArgumentParser(description="UDP Gossip node")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bootstrap", type=str, default=None, help="bootstrap ip:port")
    parser.add_argument("--fanout", type=int, default=3)
    parser.add_argument("--ttl", type=int, default=8)
    parser.add_argument("--peer-limit", type=int, default=50)
    parser.add_argument("--ping-interval", type=float, default=2.0)
    parser.add_argument("--peer-timeout", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pull-interval", type=float, default=2.0, help="hybrid IHAVE interval seconds")
    parser.add_argument("--ihave-max-ids", type=int, default=32, help="max msg_ids per IHAVE")
    parser.add_argument("--pow-k", type=int, default=0, help="PoW difficulty (leading zero hex digits)")
    parser.add_argument("--stdin", type=str, default="true", help="enable stdin gossip loop (true/false)")
    parser.add_argument("--discovery-interval", type=float, default=4.0, help="periodic GET_PEERS interval seconds")

    args = parser.parse_args(argv)
    bootstrap_tuple: Optional[Tuple[str, int]] = None
    if args.bootstrap:
        try:
            host, port_str = args.bootstrap.split(":")
            bootstrap_tuple = (host, int(port_str))
        except Exception:
            raise SystemExit("--bootstrap must be in form ip:port")

    return NodeConfig(
        port=args.port,
        bootstrap=bootstrap_tuple,
        fanout=args.fanout,
        ttl=args.ttl,
        peer_limit=args.peer_limit,
        ping_interval=args.ping_interval,
        peer_timeout=args.peer_timeout,
        seed=args.seed,
        pull_interval=args.pull_interval,
        ihave_max_ids=args.ihave_max_ids,
        pow_k=max(0, int(args.pow_k)),
        stdin_enabled=_parse_bool(args.stdin),
        discovery_interval=max(0.0, float(args.discovery_interval)),
    )


def main(argv: Optional[List[str]] = None) -> None:
    cfg = parse_args(argv)
    node = GossipNode(cfg)
    try:
        asyncio.run(node.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()