"""Notifications for MedVerify.

Every notification is recorded in an in-app feed (shown by the web console, which
also raises browser pop-ups) and appended to data/notifications.jsonl. Optional
remote channels deliver the same notification elsewhere:

- ntfy (https://ntfy.sh or a self-hosted server): free phone push with no account.
  The topic name acts like a password: anyone who knows it can read the messages,
  so pick a long random topic. Disabled until a topic is configured.

Remote delivery runs on a background thread, so a slow or offline push service
never delays reminders, device commands, or API responses. Delivery outcomes are
recorded on each notification; a failed push is never reported as delivered.

SAFETY BOUNDARY:
Messages describe what the camera saw ("removed from compartment A"). They never
claim that a dose was swallowed.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests

INFO = "info"
WARNING = "warning"
CRITICAL = "critical"
LEVELS = (INFO, WARNING, CRITICAL)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Notification:
    title: str
    message: str
    level: str = INFO
    kind: str = "GENERAL"
    compartment_id: str | None = None
    session_id: str | None = None
    id: str = field(default_factory=lambda: f"ntf-{uuid.uuid4().hex[:10]}")
    timestamp: str = field(default_factory=utc_now)
    deliveries: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Channel(Protocol):
    name: str

    def send(self, notification: Notification) -> None:
        """Deliver or raise. Called on the notifier's worker thread."""


class NtfyChannel:
    """Phone push via ntfy. https://docs.ntfy.sh/publish/"""

    name = "ntfy"
    PRIORITY = {INFO: "default", WARNING: "high", CRITICAL: "urgent"}
    TAGS = {INFO: "pill", WARNING: "warning", CRITICAL: "rotating_light"}

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None, timeout: float = 5.0):
        if not topic or not topic.strip():
            raise ValueError("ntfy topic is required")
        self.topic = topic.strip()
        self.server = server.rstrip("/")
        self.token = token
        self.timeout = timeout

    def send(self, notification: Notification) -> None:
        headers = {
            # HTTP headers must be latin-1; keep titles ASCII-safe.
            "Title": notification.title.encode("ascii", "replace").decode("ascii"),
            "Priority": self.PRIORITY.get(notification.level, "default"),
            "Tags": self.TAGS.get(notification.level, "pill"),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        resp = requests.post(
            f"{self.server}/{self.topic}",
            data=notification.message.encode("utf-8"),
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()


class Notifier:
    """Records notifications in-app and fans them out to remote channels asynchronously."""

    def __init__(
        self,
        log_file: Path | str | None = None,
        channels: list[Channel] | None = None,
        keep: int = 200,
    ) -> None:
        self.log_path = Path(log_file) if log_file else None
        self.channels: list[Channel] = list(channels or [])
        self.keep = keep
        self._recent: list[Notification] = []
        self._lock = threading.Lock()
        self._queue: queue.Queue[Notification | None] = queue.Queue(maxsize=500)
        self._worker: threading.Thread | None = None

    @property
    def channel_names(self) -> list[str]:
        return ["in_app"] + [c.name for c in self.channels]

    def notify(
        self,
        title: str,
        message: str,
        level: str = INFO,
        kind: str = "GENERAL",
        compartment_id: str | None = None,
        session_id: str | None = None,
    ) -> Notification:
        if level not in LEVELS:
            level = INFO
        n = Notification(
            title=title,
            message=message,
            level=level,
            kind=kind,
            compartment_id=compartment_id,
            session_id=session_id,
        )
        n.deliveries["in_app"] = "delivered"
        with self._lock:
            self._recent.append(n)
            del self._recent[: -self.keep]
        self._append_log(n)
        print(f"[NOTIFY:{level.upper()}] {title} - {message}")

        for channel in self.channels:
            n.deliveries[channel.name] = "pending"
        if self.channels:
            self._ensure_worker()
            try:
                self._queue.put_nowait(n)
            except queue.Full:
                for channel in self.channels:
                    n.deliveries[channel.name] = "dropped: queue full"
        return n

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [n.to_dict() for n in self._recent[-limit:]]

    def _append_log(self, n: Notification) -> None:
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(n.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[NOTIFY] Could not write notification log: {exc}")

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._deliver, name="medverify-notifier", daemon=True)
                self._worker.start()

    def _deliver(self) -> None:
        while True:
            n = self._queue.get()
            try:
                if n is None:
                    return
                for channel in self.channels:
                    try:
                        channel.send(n)
                        n.deliveries[channel.name] = "delivered"
                    except Exception as exc:  # A failing channel must not block the others
                        n.deliveries[channel.name] = f"failed: {type(exc).__name__}: {exc}"[:200]
                        print(f"[NOTIFY] {channel.name} delivery failed: {exc}")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait for queued remote deliveries (used by tests and shutdown)."""
        import time

        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True


def build_notifier(settings: dict[str, Any], log_file: Path | str | None = None) -> Notifier:
    """Create a Notifier from settings["notifications"], with environment overrides.

    MEDVERIFY_NTFY_TOPIC / MEDVERIFY_NTFY_SERVER / MEDVERIFY_NTFY_TOKEN override the file,
    so a private topic need not be written into a config file that may be committed.
    """
    import os

    cfg = (settings.get("notifications") or {}).get("ntfy") or {}
    topic = os.getenv("MEDVERIFY_NTFY_TOPIC") or cfg.get("topic") or ""
    server = os.getenv("MEDVERIFY_NTFY_SERVER") or cfg.get("server") or "https://ntfy.sh"
    token = os.getenv("MEDVERIFY_NTFY_TOKEN") or cfg.get("token") or None
    enabled = bool(os.getenv("MEDVERIFY_NTFY_TOPIC")) or bool(cfg.get("enabled"))

    channels: list[Channel] = []
    if enabled and topic.strip():
        channels.append(NtfyChannel(topic, server, token))
    return Notifier(log_file=log_file, channels=channels)
