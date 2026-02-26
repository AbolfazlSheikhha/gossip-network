from __future__ import annotations

import argparse
from dataclasses import dataclass


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {raw}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"value must be > 0, got {value}")
    return value


def _non_negative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {raw}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"value must be >= 0, got {value}")
    return value


def parse_host_port(raw: str, field_name: str) -> tuple[str, int]:
    if ":" not in raw:
        raise ValueError(f"{field_name} must be in host:port format")

    host, port_raw = raw.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise ValueError(f"{field_name} host must not be empty")

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} port must be an integer") from exc

    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} port must be between 1 and 65535")

    return host, port


@dataclass(frozen=True)
class Config:
    port: int
    bootstrap: str
    fanout: int
    ttl: int
    peer_limit: int
    ping_interval: int
    peer_timeout: int
    seed: int
    pull_interval: int
    ids_max_ihave: int
    k_pow: int
    bind_host: str = "127.0.0.1"

    @property
    def self_addr(self) -> str:
        return f"{self.bind_host}:{self.port}"

    @property
    def bootstrap_host_port(self) -> tuple[str, int]:
        return parse_host_port(self.bootstrap, "bootstrap")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="node",
        description="UDP gossip node runtime",
    )
    parser.add_argument("--port", type=_positive_int, required=True)
    parser.add_argument("--bootstrap", type=str, required=True)
    parser.add_argument("--fanout", type=_positive_int, required=True)
    parser.add_argument("--ttl", type=_non_negative_int, required=True)
    parser.add_argument("--peer-limit", type=_positive_int, required=True)
    parser.add_argument("--ping-interval", type=_positive_int, required=True)
    parser.add_argument("--peer-timeout", type=_positive_int, required=True)
    parser.add_argument("--seed", type=int, required=True)

    # Mandatory in later stages; accepted now to preserve CLI contract.
    parser.add_argument("--pull-interval", type=_positive_int, default=2)
    parser.add_argument("--ids-max-ihave", type=_positive_int, default=32)
    parser.add_argument("--k-pow", type=_non_negative_int, default=0)
    return parser


def parse_config(argv: list[str] | None = None) -> Config:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        parse_host_port(args.bootstrap, "bootstrap")
    except ValueError as exc:
        parser.error(str(exc))

    if not _is_int(args.seed):
        parser.error("seed must be an integer")

    return Config(
        port=args.port,
        bootstrap=args.bootstrap,
        fanout=args.fanout,
        ttl=args.ttl,
        peer_limit=args.peer_limit,
        ping_interval=args.ping_interval,
        peer_timeout=args.peer_timeout,
        seed=args.seed,
        pull_interval=args.pull_interval,
        ids_max_ihave=args.ids_max_ihave,
        k_pow=args.k_pow,
    )
