"""Durable Event and Observation Logger for MedVerify.

Maintains structured, durable records in CSV and synchronizes with backend API.
Ensures local records are never discarded when remote server is unavailable.

Only confirmed state changes (transitions with is_new_event=True) are logged by
default, so a stable scene that is evaluated repeatedly does not flood the CSV or
the backend with duplicate notifications.

Backend delivery runs on a background worker thread so a slow or unreachable
server never stalls the camera loop. The local CSV row is always written first,
synchronously; the HTTP POST is best-effort and its outcome is counted, not assumed.
"""

from __future__ import annotations

import csv
import queue
import threading
import time
from pathlib import Path
from typing import Any, Sequence
import requests

from vision.state_machine import StateTransition


class EventLogger:
    """Logs compartment observation events to CSV and downstream API."""

    CSV_HEADERS = [
        "timestamp_utc",
        "compartment_id",
        "state",
        "changed_pixel_fraction",
        "interpretation",
        "previous_state",
        "confidence",
        "reason",
    ]

    def __init__(
        self,
        log_file: Path | str,
        server_url: str | None = None,
        max_pending: int = 256,
    ) -> None:
        self.log_path = Path(log_file)
        self.server_url = server_url.rstrip("/") if server_url else None

        # Delivery diagnostics: a failed or dropped POST is visible here, never reported as sent.
        self.delivered_count = 0
        self.failed_count = 0
        self.dropped_count = 0
        self.heartbeat_failures = 0
        self.last_delivery_error: str | None = None

        self._pending: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max(1, max_pending))
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()

    def log_transitions(
        self,
        transitions: Sequence[StateTransition],
        only_new_events: bool = True,
    ) -> None:
        """Write transitions to local CSV and queue them for the server.

        Args:
            transitions: State machine decisions from one evaluation pass.
            only_new_events: When True (default), skip transitions whose state did not
                change, so repeated observations of a stable scene are not re-logged
                or re-sent as fresh notifications.
        """
        if only_new_events:
            transitions = [t for t in transitions if t.is_new_event]
        if not transitions:
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not self.log_path.exists()

        rows = []
        for t in transitions:
            diff = t.metrics.get("composite_diff", t.metrics.get("diff_from_expected", 0.0))
            interpretation = (
                "Visual change only; not proof of dose removal or ingestion."
            )
            rows.append([
                t.timestamp_utc,
                t.slot,
                t.new_state.value,
                round(float(diff), 4),
                interpretation,
                t.previous_state.value,
                round(float(t.confidence), 2),
                t.reason,
            ])

        existing_cols = None
        if not is_new_file:
            try:
                with self.log_path.open("r", encoding="utf-8") as rf:
                    first_line = rf.readline().strip()
                    if first_line:
                        existing_cols = len([c.strip() for c in first_line.split(",")])
            except OSError:
                pass

        formatted_rows = [r[:5] for r in rows] if existing_cols == 5 else rows

        with self.log_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new_file:
                writer.writerow(self.CSV_HEADERS)
            writer.writerows(formatted_rows)

        # Queue each event for background delivery if a server is configured
        if self.server_url:
            for t in transitions:
                diff = t.metrics.get("composite_diff", t.metrics.get("diff_from_expected", 0.0))
                self._enqueue({
                    "slot": t.slot,
                    "state": t.new_state.value,
                    "fraction": diff,
                    "note": t.reason,
                    "previous_state": t.previous_state.value,
                })

    def send_heartbeat(self, payload: dict[str, Any]) -> None:
        """Queue a camera liveness report (non-blocking). Failures are counted separately
        from observations so a stopped backend does not drown real delivery errors."""
        if self.server_url:
            self._enqueue({"heartbeat": payload})

    def _enqueue(self, item: dict[str, Any]) -> None:
        self._ensure_worker()
        try:
            self._pending.put_nowait(item)
        except queue.Full:
            # The CSV row already exists; only the remote copy is lost.
            self.dropped_count += 1
            self.last_delivery_error = "Delivery queue full; observation kept in local CSV only"

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._deliver_pending,
                    name="medverify-event-delivery",
                    daemon=True,
                )
                self._worker.start()

    def _deliver_pending(self) -> None:
        while True:
            item = self._pending.get()
            try:
                if item is None:
                    return
                if "heartbeat" in item:
                    if not self._post("/api/vision/heartbeat", item["heartbeat"]):
                        self.heartbeat_failures += 1
                    continue
                if self.send_to_backend(
                    item["slot"], item["state"], item["fraction"], item["note"], item.get("previous_state")
                ):
                    self.delivered_count += 1
                else:
                    self.failed_count += 1
            except Exception as exc:  # Keep the worker alive for later events
                self.failed_count += 1
                self.last_delivery_error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self._pending.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued deliveries finish. Returns False if the timeout expired."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._pending.all_tasks_done:
            while self._pending.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pending.all_tasks_done.wait(remaining)
        return True

    def close(self, timeout: float = 2.0) -> bool:
        """Give queued deliveries up to `timeout` seconds, then stop the worker.

        Returns True if everything queued was attempted before the deadline.
        """
        drained = self.flush(timeout)
        if self._worker is not None and self._worker.is_alive():
            try:
                self._pending.put_nowait(None)
            except queue.Full:
                pass  # Daemon worker will not block interpreter exit
        return drained

    def send_to_backend(
        self, slot: str, state: str, fraction: float, note: str, previous_state: str | None = None
    ) -> bool:
        """Post observation to Flask API synchronously. Returns True if successful, False otherwise."""
        payload = {
            "compartment_id": slot,
            "state": state,
            "changed_pixel_fraction": round(float(fraction), 4),
            "note": f"{note} (Visual only; not ingestion proof)",
        }
        if previous_state:
            payload["previous_state"] = previous_state
        return self._post("/api/observations", payload)

    def _post(self, path: str, payload: dict[str, Any]) -> bool:
        if not self.server_url:
            return False
        try:
            resp = requests.post(
                f"{self.server_url}{path}",
                json=payload,
                timeout=1.0,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            # Backend unavailability is handled gracefully without losing data
            self.last_delivery_error = f"{type(exc).__name__}: {exc}"[:200]
            return False
