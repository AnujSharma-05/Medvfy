"""Commands from the server to the camera process (e.g. "recalibrate" after a refill).

The camera has no listening socket; it already sends a heartbeat every few seconds.
Pending commands ride back on the heartbeat response, and the camera reports each
result on a later heartbeat. A command is re-sent until a result arrives (the camera
ignores ids it has already handled) and expires if the camera never answers, so the
caretaker sees "camera did not respond" instead of waiting forever.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

KINDS = {"recalibrate"}
PENDING, DONE, FAILED, EXPIRED = "pending", "done", "failed", "expired"


class CameraCommands:
    def __init__(self, expire_seconds: float = 120.0, keep: int = 50) -> None:
        self.expire = timedelta(seconds=expire_seconds)
        self.keep = keep
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []

    def enqueue(self, kind: str, requested_by: str, now: datetime | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown command {kind}")
        now = now or datetime.now()
        cmd = {"id": f"cmd-{uuid.uuid4().hex[:8]}", "type": kind, "requested_by": requested_by,
               "requested_at": now.isoformat(timespec="seconds"), "status": PENDING,
               "result": None, "finished_at": None}
        with self._lock:
            # One pending command of a kind at a time: pressing twice should not recalibrate twice.
            for item in self._items:
                if item["type"] == kind and item["status"] == PENDING:
                    return dict(item)
            self._items.append(cmd)
            del self._items[: -self.keep]
        return dict(cmd)

    def _expire(self, now: datetime) -> None:
        for item in self._items:
            if item["status"] == PENDING and now - datetime.fromisoformat(item["requested_at"]) > self.expire:
                item.update(status=EXPIRED, result="The camera did not respond. Is the camera window running?",
                            finished_at=now.isoformat(timespec="seconds"))

    def pending(self, now: datetime | None = None) -> list[dict[str, str]]:
        """What to hand the camera on this heartbeat."""
        now = now or datetime.now()
        with self._lock:
            self._expire(now)
            return [{"id": i["id"], "type": i["type"]} for i in self._items if i["status"] == PENDING]

    def complete(self, cmd_id: str, ok: bool, message: str, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or datetime.now()
        with self._lock:
            for item in self._items:
                if item["id"] == cmd_id and item["status"] == PENDING:
                    item.update(status=DONE if ok else FAILED, result=str(message)[:200],
                                finished_at=now.isoformat(timespec="seconds"))
                    return dict(item)
        return None

    def recent(self, now: datetime | None = None, limit: int = 5) -> list[dict[str, Any]]:
        now = now or datetime.now()
        with self._lock:
            self._expire(now)
            return [dict(i) for i in self._items[-limit:]][::-1]
