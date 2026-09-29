"""A small disk cache with a time to live, that prefers stale data to none.

The sources update a few times a day at most, and one of them is a 14 MB
download. Several processes share the files (the MCP server, the approval
service, the lineup script), so writes go through a rename.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

# After a failed fetch with nothing cached, do not try again for this long:
# a source that is down must not add its timeout to every tool call.
RETRY_AFTER = 300.0


class DiskCache:
    def __init__(self, root: str | Path, clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.clock = clock
        self._memo: dict[str, tuple[float, Any]] = {}
        self._failed_until: dict[str, float] = {}
        self.status: dict[str, dict[str, Any]] = {}

    def _path(self, key: str) -> Path:
        # Keys can carry text from outside (a venue's city). A file name is
        # made only of characters that cannot leave the cache directory.
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", key).lstrip(".") or "_"
        return self.root / f"{name[:150]}.json"

    def _read(self, key: str) -> tuple[float, Any] | None:
        path = self._path(key)
        try:
            at = path.stat().st_mtime
            memo = self._memo.get(key)
            if memo and memo[0] == at:
                return memo
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        self._memo[key] = (at, value)
        return at, value

    def _write(self, key: str, value: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path(key).with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value, separators=(",", ":")))
        os.replace(tmp, self._path(key))

    def get(self, key: str, ttl: float, load: Callable[[], Any]) -> Any | None:
        """The cached value if fresh, else a new one, else the stale one."""
        now = self.clock()
        hit = self._read(key)
        if hit and now - hit[0] < ttl:
            self.status[key] = {"ok": True, "age_minutes": int((now - hit[0]) / 60)}
            return hit[1]
        if now < self._failed_until.get(key, 0.0):
            return self._fallback(key, hit, now, "recently failed; not retried yet")
        try:
            value = load()
        except Exception as exc:  # noqa: BLE001 - a source must never break a tool
            self._failed_until[key] = now + RETRY_AFTER
            return self._fallback(key, hit, now, str(exc))
        try:
            self._write(key, value)
        except OSError:
            pass
        self._memo.pop(key, None)
        self.status[key] = {"ok": True, "age_minutes": 0}
        return value

    def _fallback(self, key: str, hit: tuple[float, Any] | None, now: float,
                  error: str) -> Any | None:
        if hit:
            self.status[key] = {"ok": True, "stale": True, "error": error,
                                "age_minutes": int((now - hit[0]) / 60)}
            return hit[1]
        self.status[key] = {"ok": False, "error": error}
        return None
