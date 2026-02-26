from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


class JsonlLogger:
    def __init__(self, node_id: str, port: int, log_dir: str = "logs") -> None:
        self.node_id = node_id
        self._closed = False
        self._lock = Lock()

        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"node-{port}-{now_ms()}-{node_id[:8]}.jsonl"
        self.path = directory / filename
        self._fp = self.path.open("a", encoding="utf-8", buffering=1)

    def log(self, event: str, **fields: Any) -> None:
        if self._closed:
            return

        record: dict[str, Any] = {
            "ts_ms": now_ms(),
            "event": event,
            "node_id": self.node_id,
        }
        for key, value in fields.items():
            if value is not None:
                record[key] = value

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
        with self._lock:
            if self._closed:
                return
            self._fp.write(line + "\n")
            self._fp.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._fp.flush()
            self._fp.close()
            self._closed = True

